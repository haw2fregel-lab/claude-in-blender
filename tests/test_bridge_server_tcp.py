import json
import os
import socket
import threading
import time

import pytest


def _read_json_lines(connection, count):
    received = b""
    while received.count(b"\n") < count:
        chunk = connection.recv(8192)
        if not chunk:
            break
        received += chunk
    return [json.loads(line) for line in received.splitlines()]


@pytest.mark.parametrize("invalid_request", [[], None, 7, "request", {"broken": True}])
def test_invalid_json_roots_return_error_without_closing_connection(
    bridge_tcp, invalid_request
):
    token = bridge_tcp.server._session_token
    valid_request = {
        "token": token,
        "command": "execute_code",
        "params": {"code": "result = 'still-connected'"},
        "request_id": "a" * 32,
    }
    payload = (
        json.dumps(invalid_request) + "\n" + json.dumps(valid_request) + "\n"
    ).encode("utf-8")

    with socket.create_connection(("127.0.0.1", bridge_tcp.port), timeout=5) as connection:
        connection.settimeout(5)
        connection.sendall(payload)
        invalid_response, valid_response = _read_json_lines(connection, 2)

    assert invalid_response["ok"] is False
    assert valid_response["ok"] is True
    assert valid_response["data"]["result"] == "still-connected"
    assert valid_response["request_id"] == "a" * 32


def test_malformed_json_returns_error_without_closing_connection(bridge_tcp):
    valid_request = {
        "token": bridge_tcp.server._session_token,
        "command": "execute_code",
        "params": {"code": "result = 'still-connected'"},
        "request_id": "b" * 32,
    }
    payload = b"{ malformed\n" + (json.dumps(valid_request) + "\n").encode("utf-8")

    with socket.create_connection(("127.0.0.1", bridge_tcp.port), timeout=5) as connection:
        connection.settimeout(5)
        connection.sendall(payload)
        invalid_response, valid_response = _read_json_lines(connection, 2)

    assert invalid_response["ok"] is False
    assert "Invalid JSON" in invalid_response["error"]["message"]
    assert valid_response["ok"] is True
    assert valid_response["data"]["result"] == "still-connected"


def test_busy_guard_timeout_recovery_and_system_exit(bridge_tcp):
    server = bridge_tcp.server

    first = bridge_tcp.send("execute_code", {"code": "result = 1 + 1"})
    assert first["ok"] is True
    assert first["data"]["result"] == 2
    second = bridge_tcp.send("execute_code", {"code": "result = 'second'"})
    assert second["ok"] is True
    assert second["data"]["result"] == "second"

    slow_result = {}

    def run_slow_command():
        slow_result["response"] = bridge_tcp.send(
            "execute_code",
            {"code": "import time\ntime.sleep(0.5)\nresult = 'slow-done'"},
        )

    slow_thread = threading.Thread(target=run_slow_command, daemon=True)
    slow_thread.start()
    time.sleep(0.1)
    rejected = bridge_tcp.send("execute_code", {"code": "result = 'intruder'"})
    assert rejected["ok"] is False
    assert "still running" in rejected["error"]["message"]

    get_response = bridge_tcp.send("ping", {})
    assert "still running" not in str(get_response)
    slow_thread.join(timeout=5)
    assert slow_result["response"]["ok"] is True

    server._HOLDER_TIMEOUT_S = 0.2
    bridge_tcp.pump_enabled.clear()
    timed_out_id = "9" * 32
    timed_out = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'stale'"},
        request_id=timed_out_id,
    )
    assert timed_out["ok"] is False
    assert timed_out["status"] == "outcome_unknown"
    assert timed_out["request_id"] == timed_out_id
    assert "Timed out waiting" in timed_out["error"]["message"]
    with server._pending_lock:
        assert server._exec_busy is True

    bridge_tcp.pump_enabled.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with server._pending_lock:
            if not server._exec_busy:
                break
        time.sleep(0.01)
    with server._pending_lock:
        assert server._exec_busy is False

    completed = bridge_tcp.send(
        "get_request_status",
        {"operation_id": timed_out_id},
    )
    assert completed["ok"] is True
    assert completed["data"]["request_id"] == timed_out_id
    assert completed["data"]["status"] in {"succeeded", "failed"}

    recovered = bridge_tcp.send("execute_code", {"code": "result = 'recovered'"})
    assert recovered["ok"] is True
    assert recovered["data"]["result"] == "recovered"

    system_exit = bridge_tcp.send("execute_code", {"code": "raise SystemExit('stop')"})
    assert system_exit["ok"] is False
    assert "SystemExit: stop" in system_exit["error"]["message"]
    after_exit = bridge_tcp.send("execute_code", {"code": "result = 'still alive'"})
    assert after_exit["ok"] is True
    assert after_exit["data"]["result"] == "still alive"


# --- 契約: get_bridge_status がどの Blender かを名乗る（票B2 契約4 / 完了条件 (d)） ---


@pytest.fixture
def fake_blend_file(monkeypatch):
    """`bpy.data` の .blend まわりを実物と同じ型（str / bool）へ寄せる factory。

    conftest の bpy は MagicMock なので、素の `filepath` は mock を返し、応答を JSON に
    できない。保存済みか未保存かは案件ごとに違うので、呼ぶ側が渡す。
    """
    import bpy

    def install(filepath):
        monkeypatch.setattr(bpy.data, "filepath", filepath)
        monkeypatch.setattr(bpy.data, "is_saved", bool(filepath))
        # 絶対 path 化を bpy.path.abspath へ通す実装でも、本物と同じ答えになるように。
        monkeypatch.setattr(bpy.path, "abspath", os.path.abspath)
        # ping 応答の残りの材料も JSON にできる実物型へ寄せる。
        monkeypatch.setattr(bpy.app, "version", (4, 2, 0))
        monkeypatch.setattr(bpy.context.scene, "name", "Scene")

    return install


def test_get_bridge_status_names_the_process_and_the_open_blend_file(
    bridge_tcp, fake_blend_file, tmp_path
):
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"")
    fake_blend_file(str(blend))

    # MCP ツール get_bridge_status の TCP 実体は ping
    response = bridge_tcp.send("ping", {})

    assert response["ok"] is True
    data = response["data"]
    assert isinstance(data["pid"], int)
    assert data["pid"] == os.getpid()
    assert os.path.isabs(data["blend_file"])
    assert os.path.normcase(data["blend_file"]) == os.path.normcase(str(blend))
    # 後方互換。addon_version は .claude/skills が get_bridge_status の返りとして名指す key。
    assert "addon_version" in data, f"既存 key が消えている: {sorted(data)}"


def test_get_bridge_status_reports_no_blend_file_until_the_first_save(
    bridge_tcp, fake_blend_file
):
    fake_blend_file("")  # 未保存の Blender は bpy.data.filepath が空

    response = bridge_tcp.send("ping", {})

    assert response["ok"] is True
    assert response["data"]["blend_file"] is None
    assert response["data"]["pid"] == os.getpid()
