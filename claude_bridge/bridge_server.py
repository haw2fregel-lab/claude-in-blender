# -*- coding: utf-8 -*-
"""Blender 内で TCP を待ち受け、外から届いたコマンドを実行する受け口。

プロトコル: 1リクエスト1行の JSON {"token", "command", "params"} を受け、
封筒 {"ok", "data"/"error", "elapsed_ms"} を1行で返す。
ソケットは別スレッドが持ち、bpy を触るのはタイマー経由のメインスレッドだけ。

公開 API: start_server() / stop_server() / is_running() / clear_exec_log()
"""

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

# ping が返す addon_version。blender_manifest.toml の version と合わせる。
def _read_version():
    # blender_manifest.toml が正（バージョンの二重管理を避ける）
    try:
        import tomllib
        with open(os.path.join(os.path.dirname(__file__), "blender_manifest.toml"), "rb") as f:
            return tomllib.load(f)["version"]
    except Exception:  # noqa: BLE001 - 読めなくても ping は返せるように
        return "unknown"


_ADDON_VERSION = _read_version()

_running = False
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
_port = _DEFAULT_PORT

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
            retained = log.as_string().splitlines()[-(_MAX_LOG_LINES // 2):]
            log.clear()
            log.write("# (older log truncated)\n" + "\n".join(retained) + "\n")
    except Exception:  # noqa: BLE001
        pass


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


# ── Command handlers ──────────────────────────────────────


def _cmd_get_scene_info(_params):
    start = time.monotonic()
    scene = bpy.context.scene
    objects = []
    for obj in scene.objects:
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
    return _ok(
        {
            "scene_name": scene.name,
            "objects": objects,
            "frame_current": scene.frame_current,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
        },
        start,
    )


def _cmd_execute_code(params):
    start = time.monotonic()
    if not _execute_code_enabled:
        return _err(
            "execute_code is disabled (CLAUDE_BRIDGE_EXECUTE=0).",
            start=start,
        )

    code = params.get("code") or ""
    filename = params.get("filename") or "<execute_code>"
    if not code.strip():
        return _err("Empty code", start=start)

    request_id = params.get("request_id")
    request_label = request_id[:8] if isinstance(request_id, str) else "unknown"
    _log_exec(
        f"\n# ==== {time.strftime('%H:%M:%S')} execute_code "
        f"[req {request_label}] ====\n{code.rstrip()}"
    )

    namespace = {"bpy": bpy, "__builtins__": __builtins__, "__name__": "__main__"}
    if not filename.startswith("<"):
        namespace["__file__"] = filename
    old_trace = sys.gettrace()
    try:
        sys.settrace(_ExecTimeoutTracer(time.monotonic() + _EXEC_TIMEOUT_S))
        exec(compile(code, filename, "exec"), namespace)
    except TimeoutError as e:
        _log_exec(f"# -> TIMEOUT: {e}")
        return _err(str(e), start=start)
    except BaseException as e:
        _log_exec(f"# -> ERROR: {e}")
        return _err(
            f"{type(e).__name__}: {e}",
            tb=traceback.format_exc(),
            start=start,
        )
    finally:
        sys.settrace(old_trace)

    result = namespace.get("result")
    try:
        json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        result = str(result)

    _log_exec(f"# -> result: {str(result)[:200]}")
    return _ok({"result": result}, start)


def _cmd_ping(_params):
    start = time.monotonic()
    return _ok(
        {
            "blender_version": ".".join(str(v) for v in bpy.app.version),
            "addon_version": _ADDON_VERSION,
            "execute_code_enabled": _execute_code_enabled,
            "scene_name": bpy.context.scene.name,
        },
        start,
    )


def _cmd_get_viewport_screenshot(_params):
    start = time.monotonic()
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
        return _err("No 3D Viewport found", start=start)

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

    return _ok(
        {
            "screenshot_path": tmp_path,
            "width": width,
            "height": height,
        },
        start,
    )


def _cmd_get_selection(_params):
    start = time.monotonic()
    obj = bpy.context.active_object
    selected_objects = [o.name for o in bpy.context.selected_objects]

    if not obj:
        return _ok(
            {"mode": None, "active_object": None, "selected_objects": selected_objects},
            start,
        )

    result = {
        "mode": obj.mode,
        "active_object": obj.name,
        "selected_objects": selected_objects,
    }

    if obj.mode == "EDIT" and obj.type == "MESH":
        import bmesh

        bm = bmesh.from_edit_mesh(obj.data)
        result["selected_verts"] = sum(1 for v in bm.verts if v.select)
        result["selected_edges"] = sum(1 for e in bm.edges if e.select)
        result["selected_faces"] = sum(1 for f in bm.faces if f.select)
        result["total_verts"] = len(bm.verts)
        result["total_edges"] = len(bm.edges)
        result["total_faces"] = len(bm.faces)

    return _ok(result, start)


def _cmd_get_object_info(params):
    start = time.monotonic()
    name = params.get("name")
    obj = bpy.data.objects.get(name) if name else bpy.context.active_object
    if not obj:
        return _err("No object found", start=start)

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

    return _ok(info, start)


def _cmd_get_doc(params):
    start = time.monotonic()
    try:
        data = lookup_doc(params.get("identifier"))
    except DocLookupError as e:
        return _err(str(e), start=start)
    return _ok(data, start)


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
    global _exec_busy
    if not _running:
        return None

    with _pending_lock:
        batch = list(_pending)
        _pending.clear()

    for raw_json, event, holder in batch:
        try:
            # raw_json は _client_handler が peek 済み（不正 JSON はここへ来ない）
            msg = json.loads(raw_json)
            command = msg.get("command", "")
            params = msg.get("params", {})
            # request_id はプロトコル top-level。ログ用に params へも渡す
            if isinstance(params, dict) and msg.get("request_id") is not None:
                params.setdefault("request_id", msg["request_id"])
            if command == "execute_code":
                # 実行してもしなくても busy は戻す——holder timeout 済みで
                # スキップされた場合に立ちっぱなしになると、以後の execute が
                # 全部 reject され続ける
                try:
                    if holder["result"] is None:
                        holder["result"] = _handle_command(command, params)
                finally:
                    with _pending_lock:
                        _exec_busy = False
            elif holder["result"] is None:
                holder["result"] = _handle_command(command, params)
            request_id = msg.get("request_id")
            if request_id is not None and holder["result"] is not None:
                holder["result"]["request_id"] = request_id
        except json.JSONDecodeError as e:
            if holder["result"] is None:
                holder["result"] = _err(f"Invalid JSON: {e}", start=time.monotonic())
        except BaseException as e:
            if holder["result"] is None:
                holder["result"] = _err(
                    str(e), tb=traceback.format_exc(), start=time.monotonic()
                )
        finally:
            event.set()

    return 0.05 if batch else 0.2


# ── Client handler (daemon thread) ───────────────────────


def _client_handler(conn, addr):
    global _exec_busy
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
                if _session_token and peek.get("token") != _session_token:
                    resp = _err("Invalid session token", start=time.monotonic())
                    conn.sendall(_encode_response(resp))
                    continue
                request_id = peek.get("request_id")
                holder = {"result": None}
                event = threading.Event()

                if peek.get("command") == "execute_code":
                    # 判定と投入を同じ lock 区間で行う（積まれ待ちの重複も弾く）
                    with _pending_lock:
                        rejected = _exec_busy
                        if not rejected:
                            _exec_busy = True
                            _pending.append((raw_json, event, holder))
                    if rejected:
                        resp = _err(
                            "A previous execute is still running in Blender — outcome "
                            "unknown. Check scene state (get_scene_info / get_object_info) "
                            "before retrying.",
                            start=time.monotonic(),
                        )
                        if request_id is not None:
                            resp["request_id"] = request_id
                        conn.sendall(_encode_response(resp))
                        continue
                else:
                    with _pending_lock:
                        _pending.append((raw_json, event, holder))

                if not event.wait(timeout=_HOLDER_TIMEOUT_S):
                    holder["result"] = _err(
                        f"Timed out waiting for main thread ({_HOLDER_TIMEOUT_S}s)",
                        start=time.monotonic(),
                    )
                    if request_id is not None:
                        holder["result"]["request_id"] = request_id

                try:
                    resp_bytes = _encode_response(holder["result"])
                except (TypeError, ValueError):
                    resp_bytes = _encode_response(
                        _err("Response not serializable", start=time.monotonic())
                    )

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
                        # エラーレスポンスが巨大な場合（まず起きないが安全弁）
                        result = _err(
                            f"Response too large ({len(resp_bytes)} bytes)",
                            start=time.monotonic(),
                        )
                        holder["result"] = result
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
    global _server_socket, _running
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
                os.write(fd, _session_token.encode())
            finally:
                os.close(fd)
        except OSError as e:
            print(f"[Claude Bridge] Warning: could not write session token: {e}")
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
    global _running, _server_thread, _session_token, _execute_code_enabled, _port, _exec_busy

    if _running:
        print("[Claude Bridge] Already running")
        return

    if _server_thread and _server_thread.is_alive():
        _server_thread.join(timeout=2)

    _port = port
    _execute_code_enabled = os.environ.get("CLAUDE_BRIDGE_EXECUTE", "1") != "0"
    with _pending_lock:
        _exec_busy = False

    _session_token = secrets.token_hex(16)

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
    global _running, _server_socket, _session_token, _exec_busy

    was_running = _running
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
        for _raw, event, holder in _pending:
            if not event.is_set():
                holder["result"] = _err("Bridge shutting down", start=time.monotonic())
                event.set()
        _pending.clear()
        _exec_busy = False

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
    """受け口が動いているか。UI の表示用。"""
    return _running
