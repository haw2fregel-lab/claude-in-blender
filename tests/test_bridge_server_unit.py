import os
import threading

from claude_bridge import bridge_server


def test_response_envelopes_have_the_expected_shape():
    ok = bridge_server._ok({"result": 1}, start=0)
    assert ok["ok"] is True
    assert ok["data"] == {"result": 1}
    assert isinstance(ok["elapsed_ms"], int)

    error = bridge_server._err("failed", tb="trace", start=0)
    assert error["ok"] is False
    assert error["error"] == {"message": "failed", "traceback": "trace"}
    assert isinstance(error["elapsed_ms"], int)


def test_large_successful_response_keeps_result_key_when_truncated(bridge_tcp):
    response = bridge_tcp.send(
        "execute_code",
        {"code": f"result = 'x' * {bridge_tcp.server._MAX_RESPONSE_BYTES + 1024}"},
    )

    assert response["ok"] is True
    assert response["data"]["output_truncated"] is True
    assert "result" in response["data"]
    assert response["data"]["original_bytes"] > bridge_tcp.server._MAX_RESPONSE_BYTES


def test_token_publish_failure_stops_the_server(tmp_path, monkeypatch):
    token_file = tmp_path / "bridge-temp" / "blender-session-token"
    monkeypatch.setattr(bridge_server, "_TMP_DIR", str(token_file.parent))
    monkeypatch.setattr(bridge_server, "_TOKEN_FILE", str(token_file))
    real_open = os.open
    publish_attempted = threading.Event()

    def fail_token_publish(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(token_file):
            publish_attempted.set()
            raise OSError("publish denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_token_publish)

    bridge_server.start_server(port=0)
    assert publish_attempted.wait(timeout=2)
    bridge_server._server_thread.join(timeout=2)

    try:
        assert bridge_server._server_thread.is_alive() is False
        assert bridge_server._running is False
        assert bridge_server.is_running() is False
    finally:
        bridge_server.stop_server()


def test_running_status_requires_listener_readiness(monkeypatch):
    monkeypatch.setattr(bridge_server, "_running", True)
    monkeypatch.setattr(bridge_server, "_ready", False)
    assert bridge_server.is_running() is False

    monkeypatch.setattr(bridge_server, "_ready", True)
    assert bridge_server.is_running() is True
