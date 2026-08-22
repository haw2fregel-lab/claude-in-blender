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
    ("claude_bridge_ctx_selection", "Selection",
     "Target the current selection (get_selection)."),
    ("claude_bridge_ctx_scene", "Scene info",
     "Check the scene first (get_scene_info / get_object_info)."),
    ("claude_bridge_ctx_doc", "Docs",
     "Look up the API docs first (get_doc)."),
    ("claude_bridge_ctx_screenshot", "Screenshot",
     "Check a viewport screenshot first (get_viewport_screenshot)."),
)

# ワーカースレッド → メインスレッド(timer) の受け渡し箱。
# バックグラウンドスレッドから bpy を触るとクラッシュするため、
# スレッドはこの dict にだけ書き、timer が拾って UI プロパティへ反映する。
_result_box = {"ready": False, "text": "", "error": False}
_worker = None

# 直前の送信で見たファイル世代（None = まだ一度も送っていない）。
# bridge_server が採番し、パネルは「自分が前回見た値」だけを持つ。
_last_sent_generation = None

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
    # "Default" 単体は Blender 本体の翻訳辞書に載っていて「デフォルト」に化ける
    # （enum item は translate=False が届かない）。辞書に無い語を使う。
    ("default", "Claude Code default", "Send with Claude Code's default model"),
    ("fable", "Fable", "Latest Fable (alias)"),
    ("opus", "Opus", "Latest Opus (alias)"),
    ("sonnet", "Sonnet", "Latest Sonnet (alias)"),
    ("haiku", "Haiku", "Latest Haiku (alias)"),
)


def _model_label(bridge):
    """bridge の model 値をパネル表示名にする。未知の値は素通しで見せる。"""
    value = (bridge or {}).get("model")
    if not value:
        return "Claude Code default"
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
    return "(no messages)"


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
    # native installer の既定置き場。.exe を先に見る——Windows に Git Bash 用の
    # 拡張子なしシムが同居していても、subprocess で実行できる方を返すため。
    # macOS は GUI 起動の Blender だと PATH が痩せて which が外れるので、
    # この fallback が ~/.local/bin/claude を拾う本線になる。
    for fallback in (
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude",
    ):
        if fallback.exists():
            return str(fallback)
    return None


_LOGIN_SHELL_PATH_CACHE = None


def _login_shell_path():
    # GUI 起動の Blender は .zshrc / .zprofile を読まないので PATH が痩せる。
    # そのまま fork Claude を spawn すると、fork Claude が起動する MCP server の
    # command（`.mcp.json` の `python` など）が解決できず MCP が使えない。
    # login shell 経由で PATH を取り直して子プロセスの env に注入する。
    global _LOGIN_SHELL_PATH_CACHE
    if _LOGIN_SHELL_PATH_CACHE is not None:
        return _LOGIN_SHELL_PATH_CACHE
    fallback = os.environ.get("PATH", "")
    if os.name == "nt":
        _LOGIN_SHELL_PATH_CACHE = fallback
        return _LOGIN_SHELL_PATH_CACHE
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        r = subprocess.run(
            [shell, "-l", "-c", 'printf %s "$PATH"'],
            capture_output=True, text=True, timeout=5,
        )
        got = (r.stdout or "").strip()
        _LOGIN_SHELL_PATH_CACHE = got or fallback
    except Exception:
        _LOGIN_SHELL_PATH_CACHE = fallback
    return _LOGIN_SHELL_PATH_CACHE


def _python_for_mcp(path_value):
    # .mcp.json の command は ${CLAUDE_IN_BLENDER_PYTHON:-python}。macOS は
    # python コマンドが無いことが多く（Homebrew / python.org とも python3 のみ）、
    # fork spawn 時にこの変数で実体パスを渡して MCP server を立てられるようにする。
    # Windows は default の python で従来通り動くため触らない。
    if os.name == "nt" or os.environ.get("CLAUDE_IN_BLENDER_PYTHON"):
        return None
    for name in ("python", "python3"):
        p = shutil.which(name, path=path_value)
        if p:
            return p
    return None


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
                       "text": "Not set up: run /blender-setup in Claude Code"}
        return
    cwd = bridge.get("cwd")
    if not cwd:
        _result_box = {
            "ready": True,
            "error": True,
            "text": ".mcp.json not found (setup directory not registered). "
                    "If the repo moved, run /blender-setup again in Claude Code",
        }
        return
    mcp_config = Path(cwd) / ".mcp.json"
    if not mcp_config.exists():
        _result_box = {
            "ready": True,
            "error": True,
            "text": f".mcp.json not found ({mcp_config}). "
                    "If the repo moved, run /blender-setup again in Claude Code",
        }
        return
    claude = _find_claude(bridge)
    if not claude:
        _result_box = {"ready": True, "error": True, "text": "claude command not found"}
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
        child_env = {**os.environ, "PATH": _login_shell_path()}
        mcp_python = _python_for_mcp(child_env["PATH"])
        if mcp_python:
            child_env["CLAUDE_IN_BLENDER_PYTHON"] = mcp_python
        p = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=bridge.get("cwd") or str(Path.home()),
            timeout=300,
            creationflags=flags,
            env=child_env,
        )
        d, last_usage = _parse_stream_json(p.stdout)
        if d is not None:
            text = str(d.get("result") or d.get("error") or p.stdout[:500])
            err = bool(d.get("is_error")) or p.returncode != 0
            usage = ""
            if last_usage:
                # キャッシュ再利用=ほぼ無料で読めた分 / 新規=今回課金枠を食った分。
                # 新規は通常入力とキャッシュ作成入力の両方である。
                usage = "Cache reused {:,} / new {:,}".format(
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
                        text += "\n(connection changed elsewhere; this conversation's session was not saved)"
            if err and p.stderr:
                text += "\n" + p.stderr[:300]
        else:
            # result イベントが来ていない = stream-json として読めない応答
            text = "Protocol error: no result event\n" + (
                p.stdout or p.stderr or "empty response")[:500]
            err = True
            usage = ""
        _result_box = {"ready": True, "text": text, "error": err, "usage": usage}
    except subprocess.TimeoutExpired:
        _result_box = {"ready": True, "text": "Timeout (300s)", "error": True}
    except Exception as e:  # noqa: BLE001 - 試作: 何が起きても箱に入れて UI に見せる
        _result_box = {"ready": True, "text": f"Execution error: {e}", "error": True}


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
    if _worker and _worker.is_alive() and wm.claude_bridge_status != "WORKING":
        # ファイルロードで WM プロパティは default("IDLE") に戻るが、_worker は
        # プロセス内グローバルなので生き残る。表示だけ「送れる顔」に戻ると、押した先の
        # execute() で弾かれる。実態へ戻すのは timer 側（draw の中で RNA を書かない）。
        wm.claude_bridge_status = "WORKING"
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
_SEND_LABEL = "[Sent from Blender]"


def _build_prompt(prompt, directives=()):
    """送信文 = 出所ラベル + チェック済みトグルの指示 + ユーザーの文。それだけ。"""
    head = _SEND_LABEL
    if directives:
        head += "\n" + "\n".join("- " + d for d in directives)
    return head + "\n\n" + prompt


class CLAUDE_OT_send(bpy.types.Operator):
    bl_idname = "claude.send_request"
    bl_label = "Send to Claude"
    bl_description = ("Send the request to your Claude Code session. "
                      "Only the source label \"[Sent from Blender]\" and "
                      "checked context directives are added — nothing hidden")

    def invoke(self, context, event):
        # 前回の送信から別の .blend が開かれていたら、送る前に一度だけ確かめる。
        # 弾かない——「この依頼を今のファイルへ送っていいか」の判断は人に残す。
        if (_last_sent_generation is not None
                and bridge_server.current_generation() != _last_sent_generation):
            return context.window_manager.invoke_props_dialog(self, width=420)
        return self.execute(context)

    def draw(self, context):
        # 確認ダイアログの中身（invoke が invoke_props_dialog を開いた時だけ描かれる）。
        # OK なら execute へ、キャンセル・Esc なら送らずに終わる。
        layout = self.layout
        layout.label(text="The .blend file was switched since your last request.",
                     translate=False)
        layout.label(text="Send this request against the current file?",
                     translate=False)

    def execute(self, context):
        global _worker, _last_sent_generation
        wm = context.window_manager
        if not bridge_server.is_running():
            self.report({"ERROR"}, "Cannot send: bridge is not running")
            return {"CANCELLED"}
        prompt = wm.claude_bridge_prompt.strip()
        if not prompt:
            self.report({"WARNING"}, "Request is empty")
            return {"CANCELLED"}
        if _worker and _worker.is_alive():
            self.report({"WARNING"}, "Still processing the previous request")
            return {"CANCELLED"}
        # プロンプト組み立てはメインスレッドで済ませ、スレッドへは完成文字列だけ渡す
        directives = [text for prop, _label, text in _CTX_TOGGLES if getattr(wm, prop)]
        full_prompt = _build_prompt(prompt, directives)
        wm.claude_bridge_status = "WORKING"
        wm.claude_bridge_reply = ""
        # この送信が見た世代を覚える。次の確認は、この後に切り替わった時だけ出る
        _last_sent_generation = bridge_server.current_generation()
        _worker = threading.Thread(target=_run_claude, args=(full_prompt,), daemon=True)
        _worker.start()
        # persistent=True: 応答待ち中にファイルを開いても poll を生かす
        bpy.app.timers.register(_poll_result, first_interval=0.5, persistent=True)
        return {"FINISHED"}


class CLAUDE_OT_copy_reply(bpy.types.Operator):
    bl_idname = "claude.copy_reply"
    bl_label = "Copy Full Reply"
    bl_description = "Copy the full reply to the clipboard"

    def execute(self, context):
        context.window_manager.clipboard = context.window_manager.claude_bridge_reply
        self.report({"INFO"}, "Copied")
        return {"FINISHED"}


class CLAUDE_OT_clear_log(bpy.types.Operator):
    bl_idname = "claude.clear_log"
    bl_label = "Clear Execution Log"
    bl_description = "Clear claude_bridge_log (the record of code Claude ran)"

    def execute(self, context):
        if bridge_server.clear_exec_log():
            self.report({"INFO"}, "Execution log cleared")
        else:
            self.report({"INFO"}, "No execution log")
        return {"FINISHED"}


class CLAUDE_OT_refresh_sessions(bpy.types.Operator):
    bl_idname = "claude.refresh_sessions"
    bl_label = "Refresh List"
    bl_description = "Reload this project's recent sessions"

    def execute(self, context):
        bridge = _load_bridge() or {}
        cwd = bridge.get("cwd")
        if not cwd:
            self.report({"WARNING"}, "cwd not registered")
            return {"CANCELLED"}
        _session_cache["cwd"] = cwd
        _session_cache["items"] = _load_sessions(cwd)
        _session_cache["loaded"] = True
        return {"FINISHED"}


class CLAUDE_OT_pick_session(bpy.types.Operator):
    bl_idname = "claude.pick_session"
    bl_label = "Start from a Fork of This Session"
    bl_description = ("Grow a fork of the selected session as the panel's own. "
                      "Nothing is appended to the original conversation")

    session_id: bpy.props.StringProperty()

    def execute(self, context):
        # 継続 (-r) 直結は作らない: デスクトップ育ちの履歴へパネル構成で
        # 書き込むと、会話が混線しキャッシュも毎回プレフィックス不一致になる。
        if _save_fork_from(self.session_id):
            self.report({"INFO"}, "Fork source: ..." + self.session_id[-8:])
        else:
            self.report({"ERROR"}, "Could not write the bridge file")
        return {"FINISHED"}


class CLAUDE_OT_pick_model(bpy.types.Operator):
    bl_idname = "claude.pick_model"
    bl_label = "Select Model"
    bl_description = "Model for new sessions (applies to new sends and a fork's first send)"

    model: bpy.props.EnumProperty(items=_MODEL_ITEMS)

    def execute(self, context):
        if _save_model(self.model):
            self.report({"INFO"}, "Model: " + _model_label(_load_bridge()))
        else:
            self.report({"ERROR"}, "Could not write the bridge file")
        return {"FINISHED"}


class CLAUDE_OT_disconnect_session(bpy.types.Operator):
    bl_idname = "claude.disconnect_session"
    bl_label = "Disconnect"
    bl_description = "Disconnect the session (cwd stays registered; pick another or send as new)"

    def execute(self, context):
        if _save_session_id(None, clear_fork_from=True):
            self.report({"INFO"}, "Disconnected")
        else:
            self.report({"ERROR"}, "Could not write the bridge file")
        return {"FINISHED"}


class CLAUDE_PT_panel(bpy.types.Panel):
    bl_label = "Claude Bridge"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Claude"

    def draw(self, context):
        # パネルは英語固定。Blender の UI 翻訳は本体辞書にある語（Selection 等）だけを
        # 部分翻訳して英日混在の表示になるため、全表示を translate=False で辞書に通さない。
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
            layout.label(text="Not set up", icon="ERROR", translate=False)
            layout.label(text="Run /blender-setup in Claude Code", translate=False)
            return
        if connection_id:
            row = layout.row(align=True)
            row.enabled = not is_working
            label = "Connected: ..." + connection_id[-8:]
            if fork_from:
                label += " (fork pending)"
            row.label(text=label, icon="LINKED", translate=False)
            row.operator("claude.disconnect_session", text="", icon="X")
            if fork_from:
                # 写しがまだ生まれてない間だけ、写しを受け持つモデルを選べる
                mrow = layout.row()
                mrow.enabled = not is_working
                mrow.operator_menu_enum(
                    "claude.pick_model", "model",
                    text="Model: " + _model_label(bridge), translate=False)
        else:
            # cwd だけある: 既存セッションを選ぶか、新規のまま送るか
            layout.label(text="No session connected", icon="UNLINKED", translate=False)
            box = layout.box()
            box.enabled = not is_working
            box.label(text="Fork from a recent session (last 5):", translate=False)
            cache_ok = _session_cache["loaded"] and _session_cache["cwd"] == cwd
            if cache_ok:
                for pick_id, pick_label in _session_cache["items"]:
                    op = box.operator("claude.pick_session", text=pick_label,
                                      translate=False)
                    op.session_id = pick_id
                if not _session_cache["items"]:
                    box.label(text="(no sessions yet — you can send as new)",
                              translate=False)
            box.operator("claude.refresh_sessions",
                         text="Load list" if not cache_ok else "Refresh",
                         icon="FILE_REFRESH", translate=False)
            # 新規セッションで送る時に受け持つモデル
            mrow = layout.row()
            mrow.enabled = not is_working
            mrow.operator_menu_enum(
                "claude.pick_model", "model",
                text="Model: " + _model_label(bridge), translate=False)
        if bridge_server.is_running():
            layout.label(text="Bridge: running", icon="CHECKMARK", translate=False)
        else:
            layout.label(text="Bridge: stopped", icon="X", translate=False)
        layout.prop(wm, "claude_bridge_prompt", text="")
        col = layout.column(align=True)
        col.label(text="Context:", translate=False)
        grid = col.grid_flow(row_major=True, columns=2, align=True)
        for prop, label, _text in _CTX_TOGGLES:
            grid.prop(wm, prop, text=label, translate=False)
        row = layout.row()
        row.enabled = not is_working and bridge_server.is_running()
        if connection_id:
            row.operator("claude.send_request", text="Send to Claude",
                         icon="PLAY", translate=False)
        else:
            row.operator("claude.send_request", text="Send as New Session",
                         icon="PLAY", translate=False)
        if status == "WORKING":
            layout.label(text="Processing... (may take tens of seconds)",
                         icon="SORTTIME", translate=False)
        elif status == "DONE":
            layout.label(text="Done", icon="CHECKMARK", translate=False)
        elif status == "ERROR":
            layout.label(text="Error", icon="ERROR", translate=False)
        if status in ("DONE", "ERROR") and wm.claude_bridge_usage:
            layout.label(text=wm.claude_bridge_usage, translate=False)
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
                box.label(text=ln, translate=False)
            if collapsed and len(lines) > 8:
                box.label(text=f"... {len(lines) - 8} more lines (uncheck Collapse for all)",
                          translate=False)
            row = layout.row(align=True)
            row.prop(wm, "claude_bridge_collapsed", text="Collapse", toggle=True,
                     translate=False)
            row.operator("claude.copy_reply", text="Copy Full Reply",
                         icon="COPYDOWN", translate=False)
        if bpy.data.texts.get("claude_bridge_log"):
            layout.operator("claude.clear_log", text="Clear Execution Log",
                            icon="TRASH", translate=False)


classes = (CLAUDE_OT_send, CLAUDE_OT_copy_reply, CLAUDE_OT_clear_log,
           CLAUDE_OT_refresh_sessions, CLAUDE_OT_pick_session,
           CLAUDE_OT_pick_model, CLAUDE_OT_disconnect_session, CLAUDE_PT_panel)

_WM_PROPS = ("claude_bridge_prompt", "claude_bridge_status",
             "claude_bridge_reply", "claude_bridge_usage",
             "claude_bridge_collapsed", "claude_bridge_generation") + tuple(
    prop for prop, _label, _text in _CTX_TOGGLES)


def register():
    bpy.types.WindowManager.claude_bridge_prompt = bpy.props.StringProperty(
        name="Request", description="Request for Claude", default="")
    bpy.types.WindowManager.claude_bridge_status = bpy.props.StringProperty(default="IDLE")
    bpy.types.WindowManager.claude_bridge_reply = bpy.props.StringProperty(default="")
    bpy.types.WindowManager.claude_bridge_usage = bpy.props.StringProperty(default="")
    bpy.types.WindowManager.claude_bridge_collapsed = bpy.props.BoolProperty(
        name="Collapse", description="Collapse the reply to the first 8 lines", default=False)
    # ファイル切替の印。WM プロパティはファイルを開くと default に戻り、保存では戻らない。
    # そのズレを bridge_server.current_generation() が世代へ変える（UI には出さない）
    bpy.types.WindowManager.claude_bridge_generation = bpy.props.IntProperty(default=0)
    for prop, label, text in _CTX_TOGGLES:
        setattr(bpy.types.WindowManager, prop, bpy.props.BoolProperty(
            name=label, description=text, default=False))
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    global _last_sent_generation

    if bpy.app.timers.is_registered(_poll_result):
        bpy.app.timers.unregister(_poll_result)
    # 世代は bridge の起動単位で 1 から振り直される。パネルが古い採番を覚えたまま
    # 再有効化されると、次の1回を「切り替わった」と誤って言う。
    _last_sent_generation = None
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    for prop in _WM_PROPS:
        try:
            delattr(bpy.types.WindowManager, prop)
        except AttributeError:
            pass
