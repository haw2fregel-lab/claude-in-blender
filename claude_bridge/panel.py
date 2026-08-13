# -*- coding: utf-8 -*-
"""N パネル — 依頼欄・コンテキストトグル・送信と応答表示。

送信は `claude -r <session_id> -p "<依頼>"` を裏で実行する形。
session_id は ~/.claude/blender-bridge-session.json (Claude Code 側が登録) から読む。
応答側の Claude は、このアドオンに内蔵の受け口 (bridge_server) 経由で Blender を操作する。
"""
import json
import re
import shutil
import subprocess
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

import blf
import bpy

from . import bridge_server

BRIDGE_FILE = Path.home() / ".claude" / "blender-bridge-session.json"

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


def _load_bridge():
    if not BRIDGE_FILE.exists():
        return None
    try:
        return json.loads(BRIDGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_session_id(session_id):
    """bridge ファイルの session_id だけ書き換える（cwd 等の設定は保持）。"""
    data = _load_bridge() or {}
    data["session_id"] = session_id
    data["registered_by"] = "claude_bridge panel"
    try:
        BRIDGE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
    proj = Path.home() / ".claude" / "projects" / _project_slug(cwd)
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


def _run_claude(full_prompt):
    """ワーカースレッド本体。ここでは bpy を一切触らない。

    プロンプトはメインスレッド側で組み立て済みの完成文字列を受け取る
    （トグル = bpy プロパティをスレッドから読まないため）。
    """
    global _result_box
    bridge = _load_bridge()
    if not bridge or not (bridge.get("session_id") or bridge.get("cwd")):
        _result_box = {"ready": True, "error": True,
                       "text": "未セットアップ: Claude Code で /blender-setup を実行してね"}
        return
    claude = _find_claude(bridge)
    if not claude:
        _result_box = {"ready": True, "error": True, "text": "claude コマンドが見つからない"}
        return
    # session_id があれば継続 (-r)、なければ新規セッションとして実行。
    # 新規の場合は応答の session_id を保存して、次の送信から続きになる。
    cmd = [claude]
    if bridge.get("session_id"):
        cmd += ["-r", bridge["session_id"]]
    # MCP はこのリポの1台だけに絞る。グローバル設定の MCP まで毎回 spawn すると
    # 起動が数秒重くなる（実測で 6.1s → 4.2s）。
    if bridge.get("cwd"):
        mcp_config = Path(bridge["cwd"]) / ".mcp.json"
        if mcp_config.exists():
            cmd += ["--strict-mcp-config", "--mcp-config", str(mcp_config)]
    cmd += [
        "-p", full_prompt,
        "--allowedTools", "mcp__claude-in-blender__*",
        "--output-format", "json",
    ]
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=bridge.get("cwd") or str(Path.home()),
            timeout=300,
            creationflags=flags,
        )
        try:
            d = json.loads(p.stdout)
            text = str(d.get("result") or d.get("error") or p.stdout[:500])
            err = bool(d.get("is_error"))
            new_sid = d.get("session_id")
            if new_sid and new_sid != bridge.get("session_id"):
                _save_session_id(new_sid)
        except json.JSONDecodeError:
            text = (p.stdout or p.stderr or "空応答")[:500]
            err = p.returncode != 0
        _result_box = {"ready": True, "text": text, "error": err}
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


class CLAUDE_OT_send(bpy.types.Operator):
    bl_idname = "claude.send_request"
    bl_label = "Claude に送る"
    bl_description = "依頼を Claude Code のセッションに送る"

    def execute(self, context):
        global _worker
        wm = context.window_manager
        prompt = wm.claude_bridge_prompt.strip()
        if not prompt:
            self.report({"WARNING"}, "依頼が空だよ")
            return {"CANCELLED"}
        if _worker and _worker.is_alive():
            self.report({"WARNING"}, "まだ前の依頼を処理中")
            return {"CANCELLED"}
        # プロンプト組み立てはメインスレッドで済ませ、スレッドへは完成文字列だけ渡す
        directives = [text for prop, _label, text in _CTX_TOGGLES if getattr(wm, prop)]
        head = (
            "Blender 側のパネルからの依頼です。claude-in-blender の MCP ツールを使って"
            "Blender を直接操作して実行してください。完了したら結果を3行以内で報告してください。"
        )
        if directives:
            head += "\n" + "\n".join("- " + d for d in directives)
        full_prompt = head + "\n\n依頼: " + prompt
        wm.claude_bridge_status = "WORKING"
        wm.claude_bridge_reply = ""
        _worker = threading.Thread(target=_run_claude, args=(full_prompt,), daemon=True)
        _worker.start()
        bpy.app.timers.register(_poll_result, first_interval=0.5)
        return {"FINISHED"}


class CLAUDE_OT_copy_reply(bpy.types.Operator):
    bl_idname = "claude.copy_reply"
    bl_label = "返事を全文コピー"

    def execute(self, context):
        context.window_manager.clipboard = context.window_manager.claude_bridge_reply
        self.report({"INFO"}, "コピーした")
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
    bl_label = "このセッションに繋ぐ"
    bl_description = "選んだセッションを接続先として登録する"

    session_id: bpy.props.StringProperty()

    def execute(self, context):
        if _save_session_id(self.session_id):
            self.report({"INFO"}, "接続先: ..." + self.session_id[-8:])
        else:
            self.report({"ERROR"}, "登録ファイルを書けなかった")
        return {"FINISHED"}


class CLAUDE_OT_disconnect_session(bpy.types.Operator):
    bl_idname = "claude.disconnect_session"
    bl_label = "接続を解除"
    bl_description = "セッションの接続を解除する（登録した cwd は残るので、選び直しや新規送信はできる）"

    def execute(self, context):
        if _save_session_id(None):
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
        cwd = (bridge or {}).get("cwd")
        if not sid and not cwd:
            # 完全初期状態: 案内だけ出して終わり
            layout.label(text="未セットアップ", icon="ERROR")
            layout.label(text="Claude Code で /blender-setup を実行してね")
            return
        if sid:
            row = layout.row(align=True)
            row.label(text="接続先: ..." + sid[-8:], icon="LINKED")
            row.operator("claude.disconnect_session", text="", icon="X")
        else:
            # cwd だけある: 既存セッションを選ぶか、新規のまま送るか
            layout.label(text="セッション未接続", icon="UNLINKED")
            box = layout.box()
            box.label(text="接続先を選ぶ (直近5件):")
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
        row.enabled = wm.claude_bridge_status != "WORKING"
        if sid:
            row.operator("claude.send_request", icon="PLAY")
        else:
            row.operator("claude.send_request", text="新規セッションで送る", icon="PLAY")
        status = wm.claude_bridge_status
        if status == "WORKING":
            layout.label(text="処理中... (数十秒かかるよ)", icon="SORTTIME")
        elif status == "DONE":
            layout.label(text="完了", icon="CHECKMARK")
        elif status == "ERROR":
            layout.label(text="エラー", icon="ERROR")
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


classes = (CLAUDE_OT_send, CLAUDE_OT_copy_reply,
           CLAUDE_OT_refresh_sessions, CLAUDE_OT_pick_session,
           CLAUDE_OT_disconnect_session, CLAUDE_PT_panel)

_WM_PROPS = ("claude_bridge_prompt", "claude_bridge_status",
             "claude_bridge_reply", "claude_bridge_collapsed") + tuple(
    prop for prop, _label, _text in _CTX_TOGGLES)


def register():
    bpy.types.WindowManager.claude_bridge_prompt = bpy.props.StringProperty(
        name="依頼", description="Claude への依頼", default="")
    bpy.types.WindowManager.claude_bridge_status = bpy.props.StringProperty(default="IDLE")
    bpy.types.WindowManager.claude_bridge_reply = bpy.props.StringProperty(default="")
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
