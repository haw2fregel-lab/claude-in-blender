# -*- coding: utf-8 -*-
"""Blender 内で TCP を待ち受け、外から届いたコマンドを実行する受け口。

プロトコル: 1リクエスト1行の JSON {"token", "command", "params"} を受け、
封筒 {"ok", "data"/"error", "elapsed_ms"} を1行で返す。
ソケットは別スレッドが持ち、bpy を触るのはタイマー経由のメインスレッドだけ。

公開 API: start_server() / stop_server() / is_running() / get_startup_error()
        / clear_exec_log() / current_generation() / set_request_context()
        / clear_request_context()
"""

import errno
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import OrderedDict

import bpy

from .doc_lookup import DocLookupError, lookup_doc

_HOST = "127.0.0.1"
_DEFAULT_PORT = 9877
_LISTEN_BACKLOG = 8
_HOLDER_TIMEOUT_S = 60
_EXEC_TIMEOUT_S = 30
_MAX_OUTPUT_BYTES = 1_000_000
_SCREENSHOT_MAX_SIZE = 768
_MAX_RESPONSE_BYTES = 50_000
# Per-tool caps preserve list schemas before the generic response-size fuse.
_MAX_SCENE_INFO_OBJECTS = 200
_MAX_SELECTED_OBJECTS = 200
_MAX_DOC_BYTES = 8_000
_REQUEST_JOURNAL_LIMIT = 256
_MAX_ERROR_BYTES = 2_048
_FINAL_REQUEST_STATUSES = frozenset({"succeeded", "failed"})


# ping が返す addon_version。blender_manifest.toml の version と合わせる。
def _read_version():
    # blender_manifest.toml が正（バージョンの二重管理を避ける）
    try:
        import tomllib

        with open(
            os.path.join(os.path.dirname(__file__), "blender_manifest.toml"), "rb"
        ) as f:
            return tomllib.load(f)["version"]
    except Exception:  # noqa: BLE001 - 読めなくても ping は返せるように
        return "unknown"


_ADDON_VERSION = _read_version()

_running = False
_ready = False
_pending = []
_pending_lock = threading.Lock()
_server_socket = None
_server_thread = None
_session_token = None
_execute_code_enabled = False
_client_conns = set()
_conns_lock = threading.Lock()
# execute_code が「待ち行列に積まれてから実行完了するまで」立つ。_pending_lock で守る。
# 「実行中か」ではなく「積む時に既に居るか」で判定しないと、timer が長く止まった間の
# 再送が同じ batch に滑り込み、順に二重実行される（積む判定と投入は同じ lock 区間で行う）。
_exec_busy = False
_current_exec_request_id = None
# execute_code の結果追跡。Blender の同一プロセス内だけで保持し、_pending_lock で守る。
_request_journal = OrderedDict()
_blocked_request_id = None
_port = _DEFAULT_PORT
_startup_error = None

_TMP_DIR = os.path.join(tempfile.gettempdir(), "claude-in-blender")
_TOKEN_FILE = os.path.join(_TMP_DIR, "blender-session-token")


# ── Envelope ──────────────────────────────────────────────


def _ok(data, start):
    return {
        "ok": True,
        "data": data,
        "elapsed_ms": round((time.monotonic() - start) * 1000),
    }


def _err(message, tb=None, start=None):
    error = {"message": str(message)}
    if tb:
        error["traceback"] = tb
    elapsed = round((time.monotonic() - start) * 1000) if start else 0
    return {"ok": False, "error": error, "elapsed_ms": elapsed}


def _startup_error_message(error):
    address_in_use = {
        errno.EADDRINUSE,
        getattr(errno, "WSAEADDRINUSE", 10048),
    }
    if isinstance(error, OSError) and error.errno in address_in_use:
        return f"Port {_port} busy"
    return "Bridge failed to start"

# ── Request journal ───────────────────────────────────────


def _is_uuid_string(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        uuid.UUID(value)
    except (AttributeError, ValueError):
        return False
    return True


def _journal_public_entry(entry):
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def _journal_create_locked(request_id, command):
    """Create a bounded journal entry. Caller holds _pending_lock."""
    while len(_request_journal) >= _REQUEST_JOURNAL_LIMIT:
        evict_id = next(
            (
                known_id
                for known_id, entry in _request_journal.items()
                if known_id != _blocked_request_id
                and entry["status"] in _FINAL_REQUEST_STATUSES
            ),
            None,
        )
        if evict_id is None:
            return False
        del _request_journal[evict_id]

    now_wall = time.time()
    _request_journal[request_id] = {
        "request_id": request_id,
        "status": "queued",
        "command": command,
        "created_at": now_wall,
        "updated_at": now_wall,
        "elapsed_ms": 0,
        "_started_monotonic": time.monotonic(),
    }
    return True


def _journal_update_locked(request_id, status, error=None):
    """Update a journal entry. Caller holds _pending_lock."""
    entry = _request_journal.get(request_id)
    if entry is None:
        return
    entry["status"] = status
    entry["updated_at"] = time.time()
    entry["elapsed_ms"] = round((time.monotonic() - entry["_started_monotonic"]) * 1000)
    if error:
        entry["error"] = str(error)
    elif status == "succeeded":
        entry.pop("error", None)


def _request_status_response(operation_id, start):
    """Answer from the client thread so status works while Blender is busy."""
    if not isinstance(operation_id, str) or not operation_id:
        return _err("operation_id must be a non-empty string", start=start)

    with _pending_lock:
        entry = _request_journal.get(operation_id)
        if entry is None:
            resp = _err(f"Unknown request: {operation_id}", start=start)
            resp["request_id"] = operation_id
            return resp
        snapshot = _journal_public_entry(entry)

    return _ok(snapshot, start)


def _ack_request_result_response(operation_id, start):
    """Release the mutation blocker only after a final result was delivered."""
    global _blocked_request_id

    if not isinstance(operation_id, str) or not operation_id:
        return _err("operation_id must be a non-empty string", start=start)

    with _pending_lock:
        entry = _request_journal.get(operation_id)
        if entry is None:
            return _err(f"Unknown request: {operation_id}", start=start)
        if entry["status"] not in _FINAL_REQUEST_STATUSES:
            return _err(
                f"Request {operation_id} is not final (status: {entry['status']})",
                start=start,
            )
        if _blocked_request_id != operation_id:
            blocker = _blocked_request_id or "none"
            return _err(
                f"Request {operation_id} is not the pending acknowledgement "
                f"(current blocker: {blocker})",
                start=start,
            )
        _blocked_request_id = None
        snapshot = _journal_public_entry(entry)

    return _ok(
        {
            "request_id": snapshot["request_id"],
            "status": snapshot["status"],
            "acknowledged": True,
        },
        start,
    )


# ── Exec log ──────────────────────────────────────────────

_LOG_NAME = "claude_bridge_log"
_MAX_LOG_LINES = 5000


def _log_exec(text):
    """execute_code の内容を Text データブロックとシステムコンソールに残す。

    Text は Blender の Text エディタで claude_bridge_log を開くと読める
    （.blend 保存にも含まれる）。ログ失敗で実行は止めない。
    """
    print(f"[Claude Bridge] {text}")
    try:
        log = bpy.data.texts.get(_LOG_NAME) or bpy.data.texts.new(_LOG_NAME)
        log.write(text + "\n")
        if len(log.lines) > _MAX_LOG_LINES:
            retained = log.as_string().splitlines()[-(_MAX_LOG_LINES // 2) :]
            log.clear()
            log.write("# (older log truncated)\n" + "\n".join(retained) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _utf8_prefix(text, max_bytes):
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _traceback_tail(text, max_bytes):
    lines = text.rstrip().splitlines()
    if not lines:
        return ""

    last_file_line = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if lines[index].lstrip().startswith("File ")
        ),
        None,
    )
    if last_file_line is None:
        return _utf8_prefix("\n".join(lines[-2:]), max_bytes)

    tail = "\n".join(lines[last_file_line:])
    if len(tail.encode("utf-8")) <= max_bytes:
        return tail

    location = "\n".join(lines[last_file_line:-1])
    remaining = max_bytes - len(location.encode("utf-8")) - 1
    if remaining > 0:
        return location + "\n" + _utf8_prefix(lines[-1], remaining)
    return _utf8_prefix(location, max_bytes)


def _truncate_large_error(result):
    error = result.get("error") if isinstance(result, dict) else None
    if not isinstance(error, dict):
        return False

    message = str(error.get("message", ""))
    traceback_text = str(error.get("traceback", ""))
    original_bytes = len(message.encode("utf-8")) + len(traceback_text.encode("utf-8"))
    if original_bytes <= _MAX_ERROR_BYTES:
        return False

    try:
        _log_exec(f"# -> ERROR (full): {message}\n{traceback_text}")
    except Exception:  # noqa: BLE001
        pass
    notice = (
        " ... (truncated; full error in claude_bridge_log, original "
        f"{original_bytes:,} bytes)"
    )
    traceback_tail = _traceback_tail(traceback_text, _MAX_ERROR_BYTES // 2)
    message_limit = _MAX_ERROR_BYTES - len(traceback_tail.encode("utf-8"))
    message_head = _utf8_prefix(message, message_limit - len(notice.encode("utf-8")))
    error["message"] = message_head + notice
    if "traceback" in error:
        error["traceback"] = traceback_tail
    return True


def clear_exec_log():
    """実行ログを空にする。ログがなければ何もしない。"""
    log = bpy.data.texts.get(_LOG_NAME)
    if not log:
        return False
    log.clear()
    return True


# ── Exec timeout (sys.settrace) ───────────────────────────


class _ExecTimeoutTracer:
    __slots__ = ("_deadline",)

    def __init__(self, deadline):
        self._deadline = deadline

    def __call__(self, frame, event, arg):
        if time.monotonic() > self._deadline:
            raise TimeoutError(f"Execution exceeded {_EXEC_TIMEOUT_S}s limit")
        return self


# ── File switch detection (main thread) ───────────────────

# WindowManager のカスタムプロパティは、ファイルを開くと default に戻り、保存では
# 戻らない（Blender 5.1.2 で実測）。この「戻り」をモジュール側に残した値とのズレとして
# 読み、別の .blend が開かれたことを世代番号にする。ハンドラ（load_pre）は使わない。
_GENERATION_PROP = "claude_bridge_generation"  # 登録は panel.register() 側
_file_generation = 0  # 0 = 未初期化。stop_server() で戻す
_last_exec_generation = None  # execute_code が前回見た世代（None = まだ見ていない）
# パネルから送った依頼が始まった時の世代。None は desktop 直用の従来経路。
_request_context_generation = None


def current_generation():
    """今の .blend を指す世代番号を返す。1 以上で、ファイルが開かれるたびに 1 増える。

    呼び出しは main thread からのみ（bpy を触る）。socket スレッドから呼ばない。
    呼び手は「前回自分が見た世代」を各自で持つ。ここが配るのは採番だけなので、
    パネルと bridge のどちらが先に呼んでも相手の検知を消費しない。
    window_manager が取れない時は例外を出さず直前の値を返す
    （検知の失敗で送信や実行を止めないため）。
    """
    global _file_generation
    try:
        wm = bpy.context.window_manager
        stored = getattr(wm, _GENERATION_PROP, 0)
        if _file_generation == 0:  # 初回。今開いている .blend を第1世代に据える
            marked = 1
        elif stored != _file_generation:  # default に戻っている = ロードされた
            marked = _file_generation + 1
        else:
            return _file_generation
        setattr(wm, _GENERATION_PROP, marked)
    except Exception:  # noqa: BLE001 - 検知の失敗で送信や実行を止めない
        # 印を書けなかった時は世代を進めない。進めると次回もズレが残り、
        # 呼ばれるたびに「切り替わった」と言い続ける（誤検知が止まらない）。
        return _file_generation or 1
    _file_generation = marked
    return marked


def set_request_context(generation: int):
    """進行中のパネル依頼が始まった時のファイル世代を覚える。"""
    global _request_context_generation
    _request_context_generation = generation


def clear_request_context():
    """進行中のパネル依頼に紐づくファイル世代を捨てる。"""
    global _request_context_generation
    _request_context_generation = None


def _current_generation_for_switch():
    """切替検知に使える世代を返す。検知できなければ None で実行を通す。"""
    try:
        return current_generation()
    except Exception:  # noqa: BLE001 - 検知の失敗で送信や実行を止めない
        return None


def _request_context_file_switched():
    """パネル依頼の開始時から .blend が切り替わっていたかを返す。"""
    if _request_context_generation is None:
        return False
    generation = _current_generation_for_switch()
    return generation is not None and generation != _request_context_generation


def _observe_exec_generation():
    """前回の execute_code から .blend が切り替わっていたかを返し、見た世代を覚え直す。

    通知は切替後の最初の1回だけになる。bridge は「依頼」の切れ目を知らないので、
    「古いファイルのまま続いている」の終わりを判定できないため。
    """
    global _last_exec_generation
    generation = _current_generation_for_switch()
    if generation is None:
        return False
    switched = _last_exec_generation is not None and generation != _last_exec_generation
    _last_exec_generation = generation
    if _request_context_generation is not None:
        return generation != _request_context_generation
    return switched


def _with_file_switch(envelope, switched):
    """切替が挟まった時だけ封筒へ印を載せる。通常はキー自体を出さない。"""
    if switched:
        envelope["file_switched"] = True
    return envelope


# ── Command handlers ──────────────────────────────────────


def _cmd_get_scene_info(_params):
    start = time.monotonic()
    file_switched = _request_context_file_switched()
    scene = bpy.context.scene
    total_objects = len(scene.objects)
    objects = []
    for obj in scene.objects:
        if len(objects) >= _MAX_SCENE_INFO_OBJECTS:
            break
        objects.append(
            {
                "name": obj.name,
                "type": obj.type,
                "location": [round(v, 4) for v in obj.location],
                "rotation": [round(v, 4) for v in obj.rotation_euler],
                "scale": [round(v, 4) for v in obj.scale],
                "visible": obj.visible_get(),
            }
        )
    data = {
        "scene_name": scene.name,
        "objects": objects,
        "total_objects": total_objects,
        "frame_current": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
    }
    if total_objects > _MAX_SCENE_INFO_OBJECTS:
        data["objects_truncated"] = True
    return _with_file_switch(_ok(data, start), file_switched)


def _cmd_execute_code(params):
    start = time.monotonic()
    # request context 中は、入力エラーの封筒にも依頼開始時からの切替を載せる。
    # context がない desktop 直用では、従来どおり実行直前にだけ世代を観測する。
    file_switched = (
        _observe_exec_generation()
        if _request_context_generation is not None
        else None
    )
    if not _execute_code_enabled:
        return _with_file_switch(
            _err(
                "execute_code is disabled (CLAUDE_BRIDGE_EXECUTE=0).",
                start=start,
            ),
            file_switched,
        )

    code = params.get("code") or ""
    filename = params.get("filename") or "<execute_code>"
    if not code.strip():
        return _with_file_switch(_err("Empty code", start=start), file_switched)

    request_id = params.get("request_id")
    request_label = request_id[:8] if isinstance(request_id, str) else "unknown"
    _log_exec(
        f"\n# ==== {time.strftime('%H:%M:%S')} execute_code "
        f"[req {request_label}] ====\n{code.rstrip()}"
    )

    # ファイル切替は弾かない。実行はそのまま通し、応答に印だけ載せて判断を Claude に残す。
    # 成否どちらの封筒にも載せる——切替の直後は「名前が無い」等の失敗こそ手がかりになる。
    if file_switched is None:
        file_switched = _observe_exec_generation()

    namespace = {"bpy": bpy, "__builtins__": __builtins__, "__name__": "__main__"}
    if not filename.startswith("<"):
        namespace["__file__"] = filename
    old_trace = sys.gettrace()
    try:
        sys.settrace(_ExecTimeoutTracer(time.monotonic() + _EXEC_TIMEOUT_S))
        exec(compile(code, filename, "exec"), namespace)
    except TimeoutError as e:
        _log_exec(f"# -> TIMEOUT: {e}")
        return _with_file_switch(_err(str(e), start=start), file_switched)
    except BaseException as e:
        _log_exec(f"# -> ERROR: {e}")
        return _with_file_switch(
            _err(
                f"{type(e).__name__}: {e}",
                tb=traceback.format_exc(),
                start=start,
            ),
            file_switched,
        )
    finally:
        sys.settrace(old_trace)

    result = namespace.get("result")
    try:
        json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        result = str(result)

    _log_exec(f"# -> result: {str(result)[:200]}")
    return _with_file_switch(_ok({"result": result}, start), file_switched)


def _cmd_ping(_params):
    start = time.monotonic()
    blend_file = bpy.data.filepath
    return _ok(
        {
            "blender_version": ".".join(str(v) for v in bpy.app.version),
            "addon_version": _ADDON_VERSION,
            "execute_code_enabled": _execute_code_enabled,
            "scene_name": bpy.context.scene.name,
            "pid": os.getpid(),
            "blend_file": os.path.abspath(blend_file) if blend_file else None,
        },
        start,
    )


def _cmd_get_viewport_screenshot(_params):
    start = time.monotonic()
    file_switched = _request_context_file_switched()
    target_area = None
    target_window = None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                target_area = area
                target_window = window
                break
        if target_area:
            break

    if not target_area:
        return _with_file_switch(
            _err("No 3D Viewport found", start=start), file_switched
        )

    tmp_path = os.path.join(_TMP_DIR, f"viewport-{uuid.uuid4().hex[:8]}.png")
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)

    bpy.context.view_layer.depsgraph.update()
    with bpy.context.temp_override(window=target_window, area=target_area):
        bpy.ops.screen.screenshot_area(filepath=tmp_path)

    width = target_area.width
    height = target_area.height

    if max(width, height) > _SCREENSHOT_MAX_SIZE:
        img = bpy.data.images.load(tmp_path)
        try:
            if width >= height:
                new_width = _SCREENSHOT_MAX_SIZE
                new_height = max(1, round(height * _SCREENSHOT_MAX_SIZE / width))
            else:
                new_height = _SCREENSHOT_MAX_SIZE
                new_width = max(1, round(width * _SCREENSHOT_MAX_SIZE / height))
            img.scale(new_width, new_height)
            img.save()
            width = new_width
            height = new_height
        except Exception:
            pass
        finally:
            bpy.data.images.remove(img)

    return _with_file_switch(
        _ok(
            {
                "screenshot_path": tmp_path,
                "width": width,
                "height": height,
            },
            start,
        ),
        file_switched,
    )


def _cmd_get_selection(_params):
    start = time.monotonic()
    file_switched = _request_context_file_switched()
    obj = bpy.context.active_object
    selected = bpy.context.selected_objects
    total_selected = len(selected)
    selected_objects = []
    for selected_obj in selected:
        if len(selected_objects) >= _MAX_SELECTED_OBJECTS:
            break
        selected_objects.append(selected_obj.name)

    if not obj:
        data = {
            "mode": None,
            "active_object": None,
            "selected_objects": selected_objects,
            "total_selected": total_selected,
        }
        if total_selected > _MAX_SELECTED_OBJECTS:
            data["selection_truncated"] = True
        return _with_file_switch(
            _ok(
                data,
                start,
            ),
            file_switched,
        )

    result = {
        "mode": obj.mode,
        "active_object": obj.name,
        "selected_objects": selected_objects,
        "total_selected": total_selected,
    }
    if total_selected > _MAX_SELECTED_OBJECTS:
        result["selection_truncated"] = True

    if obj.mode == "EDIT" and obj.type == "MESH":
        import bmesh

        bm = bmesh.from_edit_mesh(obj.data)
        result["selected_verts"] = sum(1 for v in bm.verts if v.select)
        result["selected_edges"] = sum(1 for e in bm.edges if e.select)
        result["selected_faces"] = sum(1 for f in bm.faces if f.select)
        result["total_verts"] = len(bm.verts)
        result["total_edges"] = len(bm.edges)
        result["total_faces"] = len(bm.faces)

    return _with_file_switch(_ok(result, start), file_switched)


def _cmd_get_object_info(params):
    start = time.monotonic()
    file_switched = _request_context_file_switched()
    name = params.get("name")
    obj = bpy.data.objects.get(name) if name else bpy.context.active_object
    if not obj:
        return _with_file_switch(_err("No object found", start=start), file_switched)

    info = {
        "name": obj.name,
        "type": obj.type,
        "location": [round(v, 4) for v in obj.location],
        "rotation": [round(v, 4) for v in obj.rotation_euler],
        "scale": [round(v, 4) for v in obj.scale],
        "dimensions": [round(v, 4) for v in obj.dimensions],
        "modifiers": [{"name": m.name, "type": m.type} for m in obj.modifiers],
    }

    if obj.type == "MESH":
        mesh = obj.data
        if obj.mode == "EDIT":
            import bmesh

            bm = bmesh.from_edit_mesh(mesh)
            info["vertices"] = len(bm.verts)
            info["edges"] = len(bm.edges)
            info["polygons"] = len(bm.faces)
        else:
            info["vertices"] = len(mesh.vertices)
            info["edges"] = len(mesh.edges)
            info["polygons"] = len(mesh.polygons)
        info["uv_layers"] = [uv.name for uv in mesh.uv_layers]
        info["has_custom_normals"] = mesh.has_custom_normals
        info["materials"] = [m.name if m else None for m in mesh.materials]

    return _with_file_switch(_ok(info, start), file_switched)


def _cap_doc_text(data):
    doc = data.get("doc")
    if not isinstance(doc, str):
        return data
    if len(doc.encode("utf-8")) <= _MAX_DOC_BYTES:
        return data
    # UTF-8 の文字の途中で切らないため、bytes で切って ignore で戻す
    data["doc"] = doc.encode("utf-8")[:_MAX_DOC_BYTES].decode("utf-8", errors="ignore")
    data["doc_truncated"] = True
    data["original_length"] = len(doc)
    # 案内が無いと、切られた doc は実質取得不可になる——全文は Blender 内に
    # 生きているので、取りにいく道をここで名指しする。
    identifier = data.get("identifier")
    if isinstance(identifier, str) and identifier:
        data["full_doc_hint"] = (
            f"Full text: run execute_code with result = {identifier}.__doc__"
        )
    return data


def _cmd_get_doc(params):
    start = time.monotonic()
    try:
        data = lookup_doc(params.get("identifier"))
    except DocLookupError as e:
        return _err(str(e), start=start)
    return _ok(_cap_doc_text(data), start)


_HANDLERS = {
    "ping": _cmd_ping,
    "get_scene_info": _cmd_get_scene_info,
    "get_viewport_screenshot": _cmd_get_viewport_screenshot,
    "execute_code": _cmd_execute_code,
    "get_selection": _cmd_get_selection,
    "get_object_info": _cmd_get_object_info,
    "get_doc": _cmd_get_doc,
}


def _handle_command(command, params):
    start = time.monotonic()
    handler = _HANDLERS.get(command)
    if not handler:
        return _err(f"Unknown command: {command}", start=start)
    try:
        return handler(params)
    except BaseException as e:
        return _err(str(e), tb=traceback.format_exc(), start=start)


# ── Timer callback (main thread) ─────────────────────────


def _process_pending():
    global _exec_busy, _current_exec_request_id, _blocked_request_id
    if not _running:
        return None

    with _pending_lock:
        batch = list(_pending)
        _pending.clear()

    for raw_json, event, holder in batch:
        command = ""
        request_id = None
        execute_started = False
        try:
            # raw_json は _client_handler が peek 済み（不正 JSON はここへ来ない）
            msg = json.loads(raw_json)
            command = msg.get("command", "")
            params = msg.get("params", {})
            request_id = msg.get("request_id")
            # request_id はプロトコル top-level。ログ用に params へも渡す
            if isinstance(params, dict) and request_id is not None:
                params.setdefault("request_id", request_id)
            if command == "execute_code":
                with _pending_lock:
                    entry = _request_journal.get(request_id)
                    if entry is not None and not holder.get("started", False):
                        holder["started"] = True
                        # outcome_unknown は通信側へ返した観測事実なので、実行開始時に
                        # running へ巻き戻さず、完了時だけ最終状態へ進める。
                        if entry["status"] == "queued":
                            _journal_update_locked(request_id, "running")
                        execute_started = True

                if execute_started:
                    result = _handle_command(command, params)
                    _truncate_large_error(result)
                    if request_id is not None:
                        result["request_id"] = request_id
                    with _pending_lock:
                        final_status = "succeeded" if result.get("ok") else "failed"
                        error = (result.get("error") or {}).get("message")
                        _journal_update_locked(request_id, final_status, error=error)
                        # Accepted execute は、最終結果を client が受け取って ACK
                        # するまで次の mutation を止める。holder timeout の有無に
                        # 依存させないことで、client 側 timeout の race も塞ぐ。
                        _blocked_request_id = request_id
                        # 通信側が既に outcome_unknown を返していたら、それを
                        # 後から上書きせず journal だけ最終状態へ進める。
                        if holder["result"] is None:
                            result["ack_required"] = True
                            holder["result"] = result
            elif holder["result"] is None:
                result = _handle_command(command, params)
                _truncate_large_error(result)
                holder["result"] = result
            if request_id is not None and holder["result"] is not None:
                holder["result"]["request_id"] = request_id
        except json.JSONDecodeError as e:
            if holder["result"] is None:
                holder["result"] = _err(f"Invalid JSON: {e}", start=time.monotonic())
        except BaseException as e:
            if command == "execute_code" and request_id is not None:
                with _pending_lock:
                    _journal_update_locked(request_id, "failed", error=str(e))
                    _blocked_request_id = request_id
            if holder["result"] is None:
                result = _err(
                    str(e), tb=traceback.format_exc(), start=time.monotonic()
                )
                _truncate_large_error(result)
                holder["result"] = result
                if command == "execute_code" and request_id is not None:
                    holder["result"]["request_id"] = request_id
                    holder["result"]["ack_required"] = True
        finally:
            if command == "execute_code":
                with _pending_lock:
                    _exec_busy = False
                    if _current_exec_request_id == request_id:
                        _current_exec_request_id = None
            event.set()

    return 0.05 if batch else 0.2


# ── Client handler (daemon thread) ───────────────────────


def _client_handler(conn, addr):
    global _exec_busy, _blocked_request_id, _current_exec_request_id
    try:
        conn.settimeout(30)
        buffer = b""
        while _running:
            try:
                chunk = conn.recv(8192)
            except TimeoutError:
                break
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > _MAX_OUTPUT_BYTES:
                resp = _err(
                    f"Request too large ({len(buffer)} bytes)",
                    start=time.monotonic(),
                )
                conn.sendall(_encode_response(resp))
                return

            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue

                try:
                    raw_json = line.decode("utf-8")
                except UnicodeDecodeError:
                    resp = _err("Invalid UTF-8", start=time.monotonic())
                    conn.sendall(_encode_response(resp))
                    continue

                try:
                    peek = json.loads(raw_json)
                except json.JSONDecodeError:
                    resp = _err("Invalid JSON", start=time.monotonic())
                    conn.sendall(_encode_response(resp))
                    continue
                if not isinstance(peek, dict):
                    resp = _err(
                        "Request JSON must be an object", start=time.monotonic()
                    )
                    conn.sendall(_encode_response(resp))
                    continue
                if _session_token and peek.get("token") != _session_token:
                    resp = _err("Invalid session token", start=time.monotonic())
                    conn.sendall(_encode_response(resp))
                    continue
                command = peek.get("command")
                request_id = peek.get("request_id")

                if command == "get_request_status":
                    params = peek.get("params")
                    operation_id = (
                        params.get("operation_id") if isinstance(params, dict) else None
                    )
                    resp = _request_status_response(operation_id, time.monotonic())
                    if request_id is not None:
                        resp["request_id"] = request_id
                    conn.sendall(_encode_response(resp))
                    continue

                if command == "ack_request_result":
                    params = peek.get("params")
                    operation_id = (
                        params.get("operation_id") if isinstance(params, dict) else None
                    )
                    resp = _ack_request_result_response(operation_id, time.monotonic())
                    if request_id is not None:
                        resp["request_id"] = request_id
                    conn.sendall(_encode_response(resp))
                    continue

                holder = {"result": None}
                event = threading.Event()

                if command == "execute_code":
                    if not _is_uuid_string(request_id):
                        resp = _err(
                            "execute_code requires a UUID string request_id",
                            start=time.monotonic(),
                        )
                        if request_id is not None:
                            resp["request_id"] = request_id
                        conn.sendall(_encode_response(resp))
                        continue

                    # journal 判定・busy 判定・投入を同じ lock 区間で行う。
                    with _pending_lock:
                        existing = _request_journal.get(request_id)
                        blocker_id = _blocked_request_id
                        busy_id = _current_exec_request_id
                        resp = None
                        if existing is not None:
                            resp = _err(
                                f"Duplicate execute request_id: {request_id}. "
                                "The operation was not executed again; use "
                                "get_request_status.",
                                start=time.monotonic(),
                            )
                            resp["status"] = existing["status"]
                            resp["request_id"] = request_id
                        elif blocker_id is not None:
                            resp = _err(
                                "A previous execute has an unobserved outcome. "
                                f"Call get_request_status for {blocker_id} before "
                                "running another execute.",
                                start=time.monotonic(),
                            )
                            resp["status"] = "blocked"
                            resp["request_id"] = request_id
                            resp["blocker_request_id"] = blocker_id
                        elif _exec_busy:
                            resp = _err(
                                "A previous execute is still running in Blender. "
                                "Wait for it to finish before retrying.",
                                start=time.monotonic(),
                            )
                            resp["status"] = "blocked"
                            resp["request_id"] = request_id
                            if busy_id is not None:
                                resp["blocker_request_id"] = busy_id
                        elif not _journal_create_locked(request_id, command):
                            resp = _err(
                                "Request journal is full; cannot safely track a new execute",
                                start=time.monotonic(),
                            )
                            resp["request_id"] = request_id
                        else:
                            _exec_busy = True
                            _current_exec_request_id = request_id
                            _pending.append((raw_json, event, holder))
                    if resp is not None:
                        conn.sendall(_encode_response(resp))
                        continue
                else:
                    with _pending_lock:
                        _pending.append((raw_json, event, holder))

                if not event.wait(timeout=_HOLDER_TIMEOUT_S):
                    with _pending_lock:
                        # Completion can race the wait boundary. Prefer the real
                        # result if it became available before this lock was taken.
                        if holder["result"] is None:
                            if command == "execute_code":
                                holder["result"] = _err(
                                    f"Timed out waiting for main thread "
                                    f"({_HOLDER_TIMEOUT_S}s). Outcome unknown; call "
                                    f"get_request_status for {request_id}.",
                                    start=time.monotonic(),
                                )
                                holder["result"]["status"] = "outcome_unknown"
                                _journal_update_locked(request_id, "outcome_unknown")
                                _blocked_request_id = request_id
                            else:
                                holder["result"] = _err(
                                    f"Timed out waiting for main thread "
                                    f"({_HOLDER_TIMEOUT_S}s)",
                                    start=time.monotonic(),
                                )
                            if request_id is not None:
                                holder["result"]["request_id"] = request_id

                try:
                    resp_bytes = _encode_response(holder["result"])
                except (TypeError, ValueError):
                    fallback = _err("Response not serializable", start=time.monotonic())
                    # 作り直した封筒でも切替の印は落とさない——切替直後の失敗こそ手がかり
                    if (isinstance(holder["result"], dict)
                            and holder["result"].get("file_switched")):
                        fallback["file_switched"] = True
                    resp_bytes = _encode_response(fallback)

                if len(resp_bytes) > _MAX_RESPONSE_BYTES:
                    result = holder["result"]
                    if result and result.get("ok") and "data" in result:
                        data_str = json.dumps(result["data"], ensure_ascii=False)
                        truncated = data_str.encode("utf-8")[:_MAX_RESPONSE_BYTES]
                        # UTF-8 の途中で切れないよう decode で戻す
                        truncated_str = truncated.decode("utf-8", errors="ignore")
                        result["data"] = {
                            "result": truncated_str,
                            "output_truncated": True,
                            "original_bytes": len(resp_bytes),
                        }
                    else:
                        # 安全弁: それでも巨大な応答は中身だけ差し替える。封筒を
                        # 作り直すと request_id / ack_required が落ち、ACK 経路が詰まる。
                        result["ok"] = False
                        result.pop("data", None)
                        result["error"] = {
                            "message": f"Response too large ({len(resp_bytes)} bytes)"
                        }
                    resp_bytes = _encode_response(holder["result"])

                conn.sendall(resp_bytes)
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
        with _conns_lock:
            _client_conns.discard(conn)


def _remove_own_token_file():
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as f:
            if f.read().strip() == _session_token:
                os.remove(_TOKEN_FILE)
    except OSError:
        pass


def _clean_viewport_screenshots():
    try:
        for name in os.listdir(_TMP_DIR):
            if name.startswith("viewport-") and name.endswith(".png"):
                os.remove(os.path.join(_TMP_DIR, name))
    except OSError:
        pass


def _encode_response(result):
    return (json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8")


# ── Server loop (daemon thread) ──────────────────────────


def _server_loop():
    global _server_socket, _running, _ready, _startup_error
    try:
        _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _server_socket.settimeout(1.0)
        _server_socket.bind((_HOST, _port))
        _server_socket.listen(_LISTEN_BACKLOG)
        try:
            os.makedirs(_TMP_DIR, exist_ok=True)
            if sys.platform != "win32":
                os.chmod(_TMP_DIR, 0o700)
            fd = os.open(_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                token_bytes = _session_token.encode()
                if os.write(fd, token_bytes) != len(token_bytes):
                    raise OSError("Session token was only partially written")
            finally:
                os.close(fd)
        except OSError as e:
            # The token is the authentication boundary. Do not leave a listener
            # running when publishing it failed, even on localhost.
            raise RuntimeError(f"Could not publish session token: {e}") from e
        _ready = True
        _clean_viewport_screenshots()
        print(f"[Claude Bridge] Listening on {_HOST}:{_port}")

        while _running:
            try:
                conn, addr = _server_socket.accept()
                with _conns_lock:
                    _client_conns.add(conn)
                threading.Thread(
                    target=_client_handler, args=(conn, addr), daemon=True
                ).start()
            except TimeoutError:
                continue
            except OSError:
                if _running:
                    raise
                break
    except Exception as e:
        print(f"[Claude Bridge] Server error: {e}")
        if not _ready:
            _startup_error = _startup_error_message(e)
        _ready = False
        _running = False
        if _server_socket:
            try:
                _server_socket.close()
            except OSError:
                pass
            _server_socket = None
        _remove_own_token_file()


# ── Public API ───────────────────────────────────────────


def start_server(port=_DEFAULT_PORT):
    """受け口を起動する。すでに動いていれば何もしない。"""
    global _running, _ready, _server_thread, _session_token, _execute_code_enabled
    global _port, _exec_busy, _current_exec_request_id, _blocked_request_id, _startup_error

    if _running:
        print("[Claude Bridge] Already running")
        return

    if _server_thread and _server_thread.is_alive():
        _server_thread.join(timeout=2)

    _port = port
    _startup_error = None
    _execute_code_enabled = os.environ.get("CLAUDE_BRIDGE_EXECUTE", "1") != "0"
    with _pending_lock:
        _pending.clear()
        _exec_busy = False
        _current_exec_request_id = None
        _blocked_request_id = None
        _request_journal.clear()

    _session_token = secrets.token_hex(16)

    _ready = False
    _running = True
    _server_thread = threading.Thread(target=_server_loop, daemon=True)
    _server_thread.start()
    # persistent=True: 既定ではファイルロードで timer が消える。起動時は
    # register → startup ファイル読込の順なので、これが無いと橋が起動直後に死ぬ
    bpy.app.timers.register(_process_pending, first_interval=0.1, persistent=True)

    status = "execute_code=ON" if _execute_code_enabled else "execute_code=OFF"
    print(f"[Claude Bridge] Bridge started on {_HOST}:{_port} ({status})")


def stop_server():
    """受け口を止め、待ち行列・ソケット・トークンを片付ける。"""
    global _running, _ready, _server_socket, _session_token, _exec_busy
    global _current_exec_request_id, _blocked_request_id
    global _file_generation, _last_exec_generation

    was_running = _running
    _ready = False
    _running = False
    with _conns_lock:
        for conn in _client_conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        _client_conns.clear()

    if bpy.app.timers.is_registered(_process_pending):
        bpy.app.timers.unregister(_process_pending)

    if _server_thread and _server_thread.is_alive():
        _server_thread.join(timeout=2)

    with _pending_lock:
        for raw_json, event, holder in _pending:
            if not event.is_set():
                holder["result"] = _err("Bridge shutting down", start=time.monotonic())
                try:
                    request_id = json.loads(raw_json).get("request_id")
                except (AttributeError, json.JSONDecodeError):
                    request_id = None
                if request_id is not None:
                    holder["result"]["request_id"] = request_id
                    _journal_update_locked(
                        request_id, "failed", error="Bridge shutting down"
                    )
                event.set()
        _pending.clear()
        _exec_busy = False
        _current_exec_request_id = None
        _blocked_request_id = None
        _request_journal.clear()

    # 世代は bridge の起動単位。次の start で 1 から採番し直すので、観測側の記憶も
    # 一緒に捨てる（古い採番と比べると、再起動の1回目を切替と誤って言う）。
    _file_generation = 0
    _last_exec_generation = None
    clear_request_context()

    if _server_socket:
        try:
            _server_socket.close()
        except OSError:
            pass
        _server_socket = None

    _remove_own_token_file()
    _session_token = None

    if was_running:
        print("[Claude Bridge] Bridge stopped")


def is_running():
    """受け口が listen と token 公開まで完了しているか。UI の表示用。"""
    return _running and _ready


def get_startup_error():
    """Return the most recent brief bridge startup failure for the panel."""
    return _startup_error
