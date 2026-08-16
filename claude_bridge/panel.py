# -*- coding: utf-8 -*-
"""N パネル — 依頼欄・コンテキストトグル・送信と応答表示。

送信は `claude -r <session_id> -p "<依頼>"` を裏で実行する形。
fork_from が登録されている間は初回のみ `--fork-session` で写しを作り、
生まれた専用セッションを session_id に保存して以後そちらを継続する。
接続情報は ~/.claude/blender-bridge-session.json (Claude Code 側が登録) から読む。
応答側の Claude は、このアドオンに内蔵の受け口 (bridge_server) 経由で Blender を操作する。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import blf
import bpy

from . import bridge_server

BRIDGE_FILE = Path.home() / ".claude" / "blender-bridge-session.json"
_bridge_file_lock = threading.Lock()
_EXPECTED_SESSION_UNSET = object()

# コンテキストトグル: (WindowManager プロパティ名, UI ラベル, 送信文に乗る指示)
# チェックした分だけ「これを使ってから作業して」が依頼の頭に乗る。
# description に指示文そのものを入れてあるので、ホバーで何が送られるか見える。
_CTX_TOGGLES = (
    ("claude_bridge_ctx_selection", "選択中を対象",
     "まず get_selection で選択中のものを確認し、それを作業対象にしてください。"),
    ("claude_bridge_ctx_scene", "シーン情報",
     "作業前に get_scene_info / get_object_info でシーンの実データを確認してください。"),
    ("claude_bridge_ctx_doc", "ドキュメント",
     "作業前に get_doc で使う API のドキュメントを確認してください。"),
    ("claude_bridge_ctx_screenshot", "スクショ",
     "作業前に get_viewport_screenshot でビューポートの見た目を確認してください。"),
)

# ワーカースレッド → メインスレッド(timer) の受け渡し箱。
# バックグラウンドスレッドから bpy を触るとクラッシュするため、
# スレッドはこの dict にだけ書き、timer が拾って UI プロパティへ反映する。
_result_box = {"ready": False, "text": "", "error": False}
_worker = None

# 折り返し結果のキャッシュ（draw は redraw のたびに走るため、直近1件だけ持つ）
_wrap_cache = {"key": None, "lines": ()}


def _wrap_text_px(text, max_px, context):
    """UI フォントの実測幅（blf）で max_px に収まるよう折り返した行リストを返す。

    label は入り切らないテキストを黙って「…」に切り詰める（文字が消える）ため、
    目分量の文字数換算ではなく実測で手前に折り返す。
    1文字ずつ測って足し込む（カーニング誤差は max_px 側の余白で吸収）。
    """
    font_id = 0
    max_px *= 0.96  # カーニング・丸め誤差ぶん、少し手前で折る
    try:
        # view.ui_scale はユーザー設定の倍率のみ。OS のディスプレイスケーリング込みの
        # 実効値は system.ui_scale — label の実描画サイズはこちらに従う
        ui_scale = context.preferences.system.ui_scale
        points = context.preferences.ui_styles[0].widget.points
        blf.size(font_id, points * ui_scale)
        measure = lambda ch: blf.dimensions(font_id, ch)[0]  # noqa: E731
    except Exception:  # noqa: BLE001 - 実測できない環境は全角2/半角1の概算に落とす
        px = 8.0
        measure = lambda ch: px * (2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1)  # noqa: E731
    lines = []
    for src in text.splitlines():
        if not src:
            lines.append("")
            continue
        cur, cur_w = "", 0.0
        for ch in src:
            w = measure(ch)
            if cur and cur_w + w > max_px:
                lines.append(cur)
                cur, cur_w = ch, w
            else:
                cur += ch
                cur_w += w
        lines.append(cur)
    return lines


def _claude_config_root():
    """Claude Code の設定ルート。明示設定がなければ従来の ~/.claude を使う。"""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(config_dir) if config_dir else Path.home() / ".claude"


def _bridge_file():
    """現在の Claude 設定ルートに対応する bridge ファイルを返す。"""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return _claude_config_root() / "blender-bridge-session.json"
    return BRIDGE_FILE


@contextmanager
def _lock_bridge_file(bridge_file):
    """panel と登録ツールの read-modify-write を同じ OS lock で直列化する。"""
    lock_file = bridge_file.with_suffix(bridge_file.suffix + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_bridge():
    """bridge 設定を dict だけとして読む。壊れた root は未設定扱いにする。"""
    bridge_file = _bridge_file()
    if not bridge_file.exists():
        return None
    try:
        data = json.loads(bridge_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _matches_bridge_preconditions(data, expected_cwd, expected_session_id,
                                  expected_fork_from=_EXPECTED_SESSION_UNSET):
    """CAS 保存の前提が現在の bridge 設定にも残っているかを返す。"""
    if data is None:
        return False
    if expected_cwd is not None and data.get("cwd") != expected_cwd:
        return False
    if (expected_session_id is not _EXPECTED_SESSION_UNSET
            and data.get("session_id") != expected_session_id):
        return False
    return (expected_fork_from is _EXPECTED_SESSION_UNSET
            or data.get("fork_from") == expected_fork_from)


def _save_session_id(session_id, expected_cwd=None,
                     expected_session_id=_EXPECTED_SESSION_UNSET,
                     expected_fork_from=_EXPECTED_SESSION_UNSET,
                     clear_fork_from=False):
    """前提が一致する場合だけ bridge の session_id を原子的に置き換える。

    expected_cwd / expected_session_id / expected_fork_from は worker 開始時の
    接続先を CAS 前提にする。
    既存の手動選択・切断呼び出しは前提を渡さず、従来どおり保存できる。
    """
    bridge_file = _bridge_file()
    try:
        # thread lock は panel 内、OS lock は bridge_register との競合を閉じる。
        with _bridge_file_lock, _lock_bridge_file(bridge_file):
            data = _load_bridge()
            if data is None:
                # 従来どおり、手動操作はまだ存在しない登録ファイルを作成できる。
                # ただし worker の CAS 保存や壊れた JSON root は未設定として拒否する。
                if (bridge_file.exists() or expected_cwd is not None
                        or expected_session_id is not _EXPECTED_SESSION_UNSET
                        or expected_fork_from is not _EXPECTED_SESSION_UNSET):
                    return False
                data = {}
            if not _matches_bridge_preconditions(
                    data, expected_cwd, expected_session_id, expected_fork_from):
                return False
            data = dict(data)
            data["session_id"] = session_id
            if clear_fork_from:
                data.pop("fork_from", None)
            data["registered_by"] = "claude_bridge panel"
            temp_file = bridge_file.with_suffix(".json.tmp")
            temp_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp_file, bridge_file)
            return True
    except OSError:
        return False


# --model に渡すのは世代で腐らないエイリアスだけ。フル名を使いたい時は
# bridge ファイルの "model" を直接編集すれば、表示も送信もそのまま通る。
_MODEL_ITEMS = (
    ("default", "既定", "Claude Code の既定モデルで送る"),
    ("fable", "Fable", "最新の Fable（エイリアス指定）"),
    ("opus", "Opus", "最新の Opus（エイリアス指定）"),
    ("sonnet", "Sonnet", "最新の Sonnet（エイリアス指定）"),
    ("haiku", "Haiku", "最新の Haiku（エイリアス指定）"),
)


def _model_label(bridge):
    """bridge の model 値をパネル表示名にする。未知の値は素通しで見せる。"""
    value = (bridge or {}).get("model")
    if not value:
        return "既定"
    for ident, label, _desc in _MODEL_ITEMS:
        if ident == value:
            return label
    return value


def _save_model(model):
    """bridge の model を置き換える。default / 空は「指定なし」としてキーを消す。"""
    bridge_file = _bridge_file()
    try:
        with _bridge_file_lock, _lock_bridge_file(bridge_file):
            data = _load_bridge()
            if data is None:
                # 壊れた root は拒否。未登録ファイルの新規作成は手動操作として許す
                # （_save_session_id と同じ線引き）。
                if bridge_file.exists():
                    return False
                data = {}
            data = dict(data)
            if model and model != "default":
                data["model"] = model
            else:
                data.pop("model", None)
            temp_file = bridge_file.with_suffix(".json.tmp")
            temp_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp_file, bridge_file)
            return True
    except OSError:
        return False


def _save_fork_from(fork_source):
    """bridge を「この ID の写しを作る」状態にする。元セッションには書き込まない。

    bridge_register.py と同じ対で書く: fork_from を立て、session_id を空にする。
    """
    bridge_file = _bridge_file()
    try:
        with _bridge_file_lock, _lock_bridge_file(bridge_file):
            data = _load_bridge()
            if data is None:
                if bridge_file.exists():
                    return False
                data = {}
            data = dict(data)
            data["fork_from"] = fork_source
            data["session_id"] = None
            data["registered_by"] = "claude_bridge panel"
            temp_file = bridge_file.with_suffix(".json.tmp")
            temp_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp_file, bridge_file)
            return True
    except OSError:
        return False


def _project_slug(cwd):
    """Claude Code の projects ディレクトリ名（英数字以外を - に置換）。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


# セッション一覧のキャッシュ。draw は redraw のたびに走るので、
# jsonl を読むのは「一覧を読み込む/更新」オペレータの時だけにする。
_session_cache = {"cwd": None, "items": (), "loaded": False}


def _first_user_message(path, max_lines=30):
    """transcript 先頭から最初のユーザー発言を抜く（一覧の見出し用）。

    transcript は非公開フォーマットなので、読めなかったら黙って諦める。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "queue-operation" and d.get("operation") == "enqueue":
                    c = d.get("content")
                    if isinstance(c, str) and c.strip():
                        return c.strip()
                if d.get("type") == "user":
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, str) and c.strip():
                        return c.strip()
                    if isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                                return b["text"].strip()
    except OSError:
        pass
    return "(発言なし)"


def _load_sessions(cwd, limit=5):
    """cwd のプロジェクトの直近セッションを (id, 表示ラベル) で返す。"""
    proj = _claude_config_root() / "projects" / _project_slug(cwd)
    try:
        files = sorted(proj.glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    items = []
    for f in files[:limit]:
        stamp = datetime.fromtimestamp(f.stat().st_mtime).strftime("%m/%d %H:%M")
        head = _first_user_message(f)
        items.append((f.stem, f"{stamp}  {head[:24]}"))
    return tuple(items)


def _find_claude(bridge):
    exe = (bridge or {}).get("claude_exe")
    if exe and Path(exe).exists():
        return exe
    for name in ("claude", "claude.exe", "claude.cmd"):
        p = shutil.which(name)
        if p:
            return p
    fallback = Path.home() / ".local" / "bin" / "claude.exe"
    return str(fallback) if fallback.exists() else None


def _parse_stream_json(stdout):
    """stream-json 出力（1行1イベント）から (result イベント, 最終コールの usage) を返す。

    usage に result イベントのものを使わないこと——result の usage は全 API コールの
    合算で、「この送信のコスト」として誤読される（ツールを使う依頼はコールが複数回
    走るため、線のコンテキスト量を超えた数字が出る。実測: 27万の線で再利用55万）。
    パネルに出すのは最終コール単体 = 1コールの請求 ≒ この線の今のコンテキスト量。
    """
    result_event = None
    last_usage = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            usage = (event.get("message") or {}).get("usage")
            if usage:
                last_usage = usage
        elif event.get("type") == "result":
            result_event = event
    return result_event, last_usage


def _run_claude(full_prompt):
    """ワーカースレッド本体。ここでは bpy を一切触らない。

    プロンプトはメインスレッド側で組み立て済みの完成文字列を受け取る
    （トグル = bpy プロパティをスレッドから読まないため）。
    """
    global _result_box
    bridge = _load_bridge()
    if not bridge:
        _result_box = {"ready": True, "error": True,
                       "text": "未セットアップ: Claude Code で /blender-setup を実行してね"}
        return
    cwd = bridge.get("cwd")
    if not cwd:
        _result_box = {
            "ready": True,
            "error": True,
            "text": ".mcp.json が見つからない（セットアップ作業ディレクトリが未設定）。"
                    "リポを移動した場合は Claude Code で /blender-setup をやり直してね",
        }
        return
    mcp_config = Path(cwd) / ".mcp.json"
    if not mcp_config.exists():
        _result_box = {
            "ready": True,
            "error": True,
            "text": f".mcp.json が見つからない（{mcp_config}）。"
                    "リポを移動した場合は Claude Code で /blender-setup をやり直してね",
        }
        return
    claude = _find_claude(bridge)
    if not claude:
        _result_box = {"ready": True, "error": True, "text": "claude コマンドが見つからない"}
        return
    # fork_from があれば写しを作り、session_id のみなら継続 (-r) する。
    # 新規または fork の場合は応答の session_id を保存して、次の送信から続きになる。
    # --model が付くのは新しいセッションが生まれる送信（新規・fork 初回）だけ。
    # 写しは文脈を引き継ぐがモデルは選び直せる。継続は育ったセッションに従う。
    cmd = [claude]
    session_id = bridge.get("session_id")
    fork_from = bridge.get("fork_from")
    model = bridge.get("model")
    if fork_from:
        cmd += ["-r", fork_from, "--fork-session"]
        if model:
            cmd += ["--model", model]
    elif session_id:
        cmd += ["-r", session_id]
    elif model:
        cmd += ["--model", model]
    # MCP はこのリポの1台だけに絞り、組み込みツールは無効化する。
    # グローバル設定の MCP まで毎回 spawn すると起動が数秒重くなる
    # （実測で 6.1s → 4.2s）。scratch の書き込み・差分編集は MCP 側で担う。
    cmd += [
        "--strict-mcp-config", "--mcp-config", str(mcp_config),
        "--tools", "",
        # 依頼文はプロセス一覧に出さないため stdin で渡す。
        "-p",
        "--allowedTools", "mcp__claude-in-blender__*",
        # stream-json はコールごとの usage が取れる唯一の形式（-p では --verbose 必須）。
        # json 一発だと usage が全コール合算になり、送信コストとして読めない。
        "--output-format", "stream-json", "--verbose",
    ]
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=bridge.get("cwd") or str(Path.home()),
            timeout=300,
            creationflags=flags,
        )
        d, last_usage = _parse_stream_json(p.stdout)
        if d is not None:
            text = str(d.get("result") or d.get("error") or p.stdout[:500])
            err = bool(d.get("is_error")) or p.returncode != 0
            usage = ""
            if last_usage:
                # キャッシュ再利用=ほぼ無料で読めた分 / 新規=今回課金枠を食った分。
                # 新規は通常入力とキャッシュ作成入力の両方である。
                usage = "キャッシュ再利用 {:,} / 新規 {:,}".format(
                    last_usage.get("cache_read_input_tokens") or 0,
                    ((last_usage.get("input_tokens") or 0)
                     + (last_usage.get("cache_creation_input_tokens") or 0)))
            new_sid = d.get("session_id")
            is_valid_session_id = (
                isinstance(new_sid, str)
                and re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    new_sid.lower(),
                )
            )
            if not err and is_valid_session_id and new_sid != session_id:
                saved = _save_session_id(
                    new_sid,
                    expected_cwd=cwd,
                    expected_session_id=session_id,
                    expected_fork_from=fork_from,
                    clear_fork_from=bool(fork_from),
                )
                if not saved:
                    current_bridge = _load_bridge()
                    if (not current_bridge or current_bridge.get("cwd") != cwd
                            or current_bridge.get("session_id") != session_id
                            or current_bridge.get("fork_from") != fork_from):
                        text += "\n(接続先が切り替わったため、この会話の接続先は保存しなかった)"
            if err and p.stderr:
                text += "\n" + p.stderr[:300]
        else:
            # result イベントが来ていない = stream-json として読めない応答
            text = "プロトコルエラー: result イベントがありません\n" + (
                p.stdout or p.stderr or "空応答")[:500]
            err = True
            usage = ""
        _result_box = {"ready": True, "text": text, "error": err, "usage": usage}
    except subprocess.TimeoutExpired:
        _result_box = {"ready": True, "text": "タイムアウト (300秒)", "error": True}
    except Exception as e:  # noqa: BLE001 - 試作: 何が起きても箱に入れて UI に見せる
        _result_box = {"ready": True, "text": f"実行エラー: {e}", "error": True}


def _poll_result():
    """メインスレッドで回る timer。結果が来ていたら UI プロパティへ移す。"""
    global _result_box
    wm = bpy.context.window_manager
    if _result_box.get("ready"):
        wm.claude_bridge_status = "ERROR" if _result_box.get("error") else "DONE"
        wm.claude_bridge_reply = _result_box.get("text", "")
        wm.claude_bridge_usage = _result_box.get("usage", "")
        _result_box = {"ready": False, "text": "", "error": False}
        _tag_redraw()
        return None  # timer 終了
    _tag_redraw()
    return 0.5


def _tag_redraw():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# 送信文の先頭に付く出所ラベル。役目はこの一つだけで、ユーザーの依頼文へ
# 見えない指示を追記しない（作法系の指示は MCP サーバーの instructions が持ち、
# あちらはリポで公開された仕様）。何が送られるかはユーザーから全部見える。
_SEND_LABEL = "[Blender から送信]"


def _build_prompt(prompt, directives=()):
    """送信文 = 出所ラベル + チェック済みトグルの指示 + ユーザーの文。それだけ。"""
    head = _SEND_LABEL
    if directives:
        head += "\n" + "\n".join("- " + d for d in directives)
    return head + "\n\n" + prompt


class CLAUDE_OT_send(bpy.types.Operator):
    bl_idname = "claude.send_request"
    bl_label = "Claude に送る"
    bl_description = ("依頼を Claude Code のセッションに送る。"
                      "送信文に付くのは出所ラベル「[Blender から送信]」と"
                      "チェックしたコンテキスト指示だけ（見えない追記はしない）")

    def execute(self, context):
        global _worker
        wm = context.window_manager
        if not bridge_server.is_running():
            self.report({"ERROR"}, "受け口が停止中のため送信できない")
            return {"CANCELLED"}
        prompt = wm.claude_bridge_prompt.strip()
        if not prompt:
            self.report({"WARNING"}, "依頼が空だよ")
            return {"CANCELLED"}
        if _worker and _worker.is_alive():
            self.report({"WARNING"}, "まだ前の依頼を処理中")
            return {"CANCELLED"}
        # プロンプト組み立てはメインスレッドで済ませ、スレッドへは完成文字列だけ渡す
        directives = [text for prop, _label, text in _CTX_TOGGLES if getattr(wm, prop)]
        full_prompt = _build_prompt(prompt, directives)
        wm.claude_bridge_status = "WORKING"
        wm.claude_bridge_reply = ""
        _worker = threading.Thread(target=_run_claude, args=(full_prompt,), daemon=True)
        _worker.start()
        # persistent=True: 応答待ち中にファイルを開いても poll を生かす
        bpy.app.timers.register(_poll_result, first_interval=0.5, persistent=True)
        return {"FINISHED"}


class CLAUDE_OT_copy_reply(bpy.types.Operator):
    bl_idname = "claude.copy_reply"
    bl_label = "返事を全文コピー"

    def execute(self, context):
        context.window_manager.clipboard = context.window_manager.claude_bridge_reply
        self.report({"INFO"}, "コピーした")
        return {"FINISHED"}


class CLAUDE_OT_clear_log(bpy.types.Operator):
    bl_idname = "claude.clear_log"
    bl_label = "実行ログをクリア"

    def execute(self, context):
        if bridge_server.clear_exec_log():
            self.report({"INFO"}, "実行ログをクリアした")
        else:
            self.report({"INFO"}, "実行ログはない")
        return {"FINISHED"}


class CLAUDE_OT_refresh_sessions(bpy.types.Operator):
    bl_idname = "claude.refresh_sessions"
    bl_label = "一覧を更新"
    bl_description = "このプロジェクトの直近セッションを読み直す"

    def execute(self, context):
        bridge = _load_bridge() or {}
        cwd = bridge.get("cwd")
        if not cwd:
            self.report({"WARNING"}, "cwd 未登録")
            return {"CANCELLED"}
        _session_cache["cwd"] = cwd
        _session_cache["items"] = _load_sessions(cwd)
        _session_cache["loaded"] = True
        return {"FINISHED"}


class CLAUDE_OT_pick_session(bpy.types.Operator):
    bl_idname = "claude.pick_session"
    bl_label = "このセッションの写しから始める"
    bl_description = ("選んだセッションの写し (fork) をパネル専用セッションとして育てる。"
                      "元の会話には書き込まない")

    session_id: bpy.props.StringProperty()

    def execute(self, context):
        # 継続 (-r) 直結は作らない: デスクトップ育ちの履歴へパネル構成で
        # 書き込むと、会話が混線しキャッシュも毎回プレフィックス不一致になる。
        if _save_fork_from(self.session_id):
            self.report({"INFO"}, "写し元: ..." + self.session_id[-8:])
        else:
            self.report({"ERROR"}, "登録ファイルを書けなかった")
        return {"FINISHED"}


class CLAUDE_OT_pick_model(bpy.types.Operator):
    bl_idname = "claude.pick_model"
    bl_label = "モデルを選ぶ"
    bl_description = "新しいセッションを受け持つモデル（新規送信・写しの初回送信で効く）"

    model: bpy.props.EnumProperty(items=_MODEL_ITEMS)

    def execute(self, context):
        if _save_model(self.model):
            self.report({"INFO"}, "モデル: " + _model_label(_load_bridge()))
        else:
            self.report({"ERROR"}, "登録ファイルを書けなかった")
        return {"FINISHED"}


class CLAUDE_OT_disconnect_session(bpy.types.Operator):
    bl_idname = "claude.disconnect_session"
    bl_label = "接続を解除"
    bl_description = "セッションの接続を解除する（登録した cwd は残るので、選び直しや新規送信はできる）"

    def execute(self, context):
        if _save_session_id(None, clear_fork_from=True):
            self.report({"INFO"}, "接続を解除した")
        else:
            self.report({"ERROR"}, "登録ファイルを書けなかった")
        return {"FINISHED"}


class CLAUDE_PT_panel(bpy.types.Panel):
    bl_label = "Claude Bridge"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Claude"

    def draw(self, context):
        wm = context.window_manager
        layout = self.layout
        bridge = _load_bridge()
        sid = (bridge or {}).get("session_id")
        fork_from = (bridge or {}).get("fork_from")
        connection_id = fork_from or sid
        cwd = (bridge or {}).get("cwd")
        status = wm.claude_bridge_status
        is_working = status == "WORKING"
        if not connection_id and not cwd:
            # 完全初期状態: 案内だけ出して終わり
            layout.label(text="未セットアップ", icon="ERROR")
            layout.label(text="Claude Code で /blender-setup を実行してね")
            return
        if connection_id:
            row = layout.row(align=True)
            row.enabled = not is_working
            label = "接続先: ..." + connection_id[-8:]
            if fork_from:
                label += "（写し待ち）"
            row.label(text=label, icon="LINKED")
            row.operator("claude.disconnect_session", text="", icon="X")
            if fork_from:
                # 写しがまだ生まれてない間だけ、写しを受け持つモデルを選べる
                mrow = layout.row()
                mrow.enabled = not is_working
                mrow.operator_menu_enum(
                    "claude.pick_model", "model",
                    text="モデル: " + _model_label(bridge))
        else:
            # cwd だけある: 既存セッションを選ぶか、新規のまま送るか
            layout.label(text="セッション未接続", icon="UNLINKED")
            box = layout.box()
            box.enabled = not is_working
            box.label(text="写しの元を選ぶ (直近5件):")
            cache_ok = _session_cache["loaded"] and _session_cache["cwd"] == cwd
            if cache_ok:
                for pick_id, pick_label in _session_cache["items"]:
                    op = box.operator("claude.pick_session", text=pick_label)
                    op.session_id = pick_id
                if not _session_cache["items"]:
                    box.label(text="(セッションが無い — 新規で送れるよ)")
            box.operator("claude.refresh_sessions",
                         text="一覧を読み込む" if not cache_ok else "更新",
                         icon="FILE_REFRESH")
            # 新規セッションで送る時に受け持つモデル
            mrow = layout.row()
            mrow.enabled = not is_working
            mrow.operator_menu_enum(
                "claude.pick_model", "model",
                text="モデル: " + _model_label(bridge))
        if bridge_server.is_running():
            layout.label(text="受け口: 稼働中", icon="CHECKMARK")
        else:
            layout.label(text="受け口: 停止", icon="X")
        layout.prop(wm, "claude_bridge_prompt", text="")
        col = layout.column(align=True)
        col.label(text="コンテキスト:")
        grid = col.grid_flow(row_major=True, columns=2, align=True)
        for prop, _label, _text in _CTX_TOGGLES:
            grid.prop(wm, prop)
        row = layout.row()
        row.enabled = not is_working and bridge_server.is_running()
        if connection_id:
            row.operator("claude.send_request", icon="PLAY")
        else:
            row.operator("claude.send_request", text="新規セッションで送る", icon="PLAY")
        if status == "WORKING":
            layout.label(text="処理中... (数十秒かかるよ)", icon="SORTTIME")
        elif status == "DONE":
            layout.label(text="完了", icon="CHECKMARK")
        elif status == "ERROR":
            layout.label(text="エラー", icon="ERROR")
        if status in ("DONE", "ERROR") and wm.claude_bridge_usage:
            layout.label(text=wm.claude_bridge_usage)
        reply = wm.claude_bridge_reply
        if reply:
            # パネル実幅から box の余白・スクロールバーぶんを引いた幅に、実測で収める
            ui_scale = context.preferences.system.ui_scale
            max_px = max(80.0, context.region.width - 55 * ui_scale)
            key = (reply, round(max_px))
            if _wrap_cache["key"] != key:
                _wrap_cache["key"] = key
                _wrap_cache["lines"] = _wrap_text_px(reply, max_px, context)
            lines = _wrap_cache["lines"]
            collapsed = wm.claude_bridge_collapsed
            box = layout.box()
            shown_lines = lines[:8] if collapsed and len(lines) > 8 else lines
            for ln in shown_lines:
                box.label(text=ln)
            if collapsed and len(lines) > 8:
                box.label(text=f"... 残り {len(lines) - 8} 行（畳むを解除で全文）")
            row = layout.row(align=True)
            row.prop(wm, "claude_bridge_collapsed", text="畳む", toggle=True)
            row.operator("claude.copy_reply", icon="COPYDOWN")
        if bpy.data.texts.get("claude_bridge_log"):
            layout.operator("claude.clear_log", icon="TRASH")


classes = (CLAUDE_OT_send, CLAUDE_OT_copy_reply, CLAUDE_OT_clear_log,
           CLAUDE_OT_refresh_sessions, CLAUDE_OT_pick_session,
           CLAUDE_OT_pick_model, CLAUDE_OT_disconnect_session, CLAUDE_PT_panel)

_WM_PROPS = ("claude_bridge_prompt", "claude_bridge_status",
             "claude_bridge_reply", "claude_bridge_usage",
             "claude_bridge_collapsed") + tuple(
    prop for prop, _label, _text in _CTX_TOGGLES)


def register():
    bpy.types.WindowManager.claude_bridge_prompt = bpy.props.StringProperty(
        name="依頼", description="Claude への依頼", default="")
    bpy.types.WindowManager.claude_bridge_status = bpy.props.StringProperty(default="IDLE")
    bpy.types.WindowManager.claude_bridge_reply = bpy.props.StringProperty(default="")
    bpy.types.WindowManager.claude_bridge_usage = bpy.props.StringProperty(default="")
    bpy.types.WindowManager.claude_bridge_collapsed = bpy.props.BoolProperty(
        name="畳む", description="返事の表示を先頭8行に畳む", default=False)
    for prop, label, text in _CTX_TOGGLES:
        setattr(bpy.types.WindowManager, prop, bpy.props.BoolProperty(
            name=label, description=text, default=False))
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    if bpy.app.timers.is_registered(_poll_result):
        bpy.app.timers.unregister(_poll_result)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    for prop in _WM_PROPS:
        try:
            delattr(bpy.types.WindowManager, prop)
        except AttributeError:
            pass
