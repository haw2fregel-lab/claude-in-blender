import threading
import time


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
    timed_out = bridge_tcp.send("execute_code", {"code": "result = 'stale'"})
    assert timed_out["ok"] is False
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

    recovered = bridge_tcp.send("execute_code", {"code": "result = 'recovered'"})
    assert recovered["ok"] is True
    assert recovered["data"]["result"] == "recovered"

    system_exit = bridge_tcp.send("execute_code", {"code": "raise SystemExit('stop')"})
    assert system_exit["ok"] is False
    assert "SystemExit: stop" in system_exit["error"]["message"]
    after_exit = bridge_tcp.send("execute_code", {"code": "result = 'still alive'"})
    assert after_exit["ok"] is True
    assert after_exit["data"]["result"] == "still alive"
