import threading
import time
import uuid


def _wait_until(predicate, *, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition was not met before the deadline")


def _request_status(bridge_tcp, operation_id, *, auto_ack=True):
    return bridge_tcp.send(
        "get_request_status",
        {"operation_id": operation_id},
        auto_ack=auto_ack,
    )


def _wait_for_status(bridge_tcp, operation_id, expected, *, timeout=3):
    def poll():
        response = _request_status(bridge_tcp, operation_id)
        if response.get("ok") and response["data"].get("status") == expected:
            return response
        return None

    return _wait_until(poll, timeout=timeout)


def _assert_status_metadata(response, *, operation_id, status):
    assert response["ok"] is True
    data = response["data"]
    assert data["request_id"] == operation_id
    assert data["status"] == status
    assert data["command"] == "execute_code"
    assert "elapsed_ms" in data or any(key.endswith("_at") for key in data)


def test_successful_execute_code_is_recorded_without_changing_its_response_shape(
    bridge_tcp,
):
    operation_id = "1" * 32

    response = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'tracked'"},
        request_id=operation_id,
    )

    assert response["ok"] is True, response
    assert response["request_id"] == operation_id
    assert response["data"]["result"] == "tracked"
    status = _request_status(bridge_tcp, operation_id)
    _assert_status_metadata(status, operation_id=operation_id, status="succeeded")


def test_unknown_request_status_is_an_explicit_error(bridge_tcp):
    operation_id = "2" * 32

    response = _request_status(bridge_tcp, operation_id)

    assert response["ok"] is False
    assert operation_id in str(response)


def test_outcome_unknown_blocks_mutations_until_final_status_is_observed(
    bridge_tcp, tmp_path
):
    operation_id = "3" * 32
    started = tmp_path / "started"
    release = tmp_path / "release"
    finished = tmp_path / "finished"
    timed_out_response = {}
    bridge_tcp.server._HOLDER_TIMEOUT_S = 1.0
    bridge_tcp.pump_enabled.clear()
    code = (
        "import time\n"
        "from pathlib import Path\n"
        f"started = Path({str(started)!r})\n"
        f"release = Path({str(release)!r})\n"
        f"finished = Path({str(finished)!r})\n"
        "started.write_text('started', encoding='utf-8')\n"
        "while not release.exists():\n"
        "    time.sleep(0.01)\n"
        "finished.write_text('finished', encoding='utf-8')\n"
        "result = 'eventually-finished'"
    )

    def execute_slow_operation():
        timed_out_response["value"] = bridge_tcp.send(
            "execute_code",
            {"code": code},
            timeout=2,
            request_id=operation_id,
        )

    worker = threading.Thread(target=execute_slow_operation, daemon=True)
    worker.start()
    try:
        queued = _wait_for_status(bridge_tcp, operation_id, "queued")
        _assert_status_metadata(queued, operation_id=operation_id, status="queued")

        bridge_tcp.pump_enabled.set()
        _wait_until(started.exists)
        running = _request_status(bridge_tcp, operation_id)
        _assert_status_metadata(running, operation_id=operation_id, status="running")
        _wait_until(lambda: "value" in timed_out_response)

        timeout_response = timed_out_response["value"]
        assert timeout_response["ok"] is False
        assert timeout_response["status"] == "outcome_unknown"
        assert timeout_response["request_id"] == operation_id

        in_flight = _request_status(bridge_tcp, operation_id)
        _assert_status_metadata(
            in_flight,
            operation_id=operation_id,
            status="outcome_unknown",
        )

        blocked_id = "4" * 32
        blocked = bridge_tcp.send(
            "execute_code",
            {"code": "result = 'must-not-run'"},
            request_id=blocked_id,
        )
        assert blocked["ok"] is False
        assert operation_id in str(blocked)

        release.write_text("release", encoding="utf-8")
        _wait_until(finished.exists)

        ping = bridge_tcp.send("ping", {})
        assert operation_id not in str(ping)

        final_but_unobserved = bridge_tcp.send(
            "execute_code",
            {"code": "result = 'still-must-not-run'"},
            request_id="5" * 32,
        )
        assert final_but_unobserved["ok"] is False
        assert operation_id in str(final_but_unobserved)

        final = _request_status(bridge_tcp, operation_id)
        _assert_status_metadata(final, operation_id=operation_id, status="succeeded")

        recovered = bridge_tcp.send(
            "execute_code",
            {"code": "result = 'safe-after-observation'"},
            request_id="6" * 32,
        )
        assert recovered["ok"] is True
        assert recovered["data"]["result"] == "safe-after-observation"
    finally:
        bridge_tcp.pump_enabled.set()
        release.write_text("release", encoding="utf-8")
        worker.join(timeout=2)


def test_duplicate_execute_request_id_does_not_execute_twice(bridge_tcp, tmp_path):
    operation_id = "7" * 32
    marker = tmp_path / "duplicate-marker"
    first_code = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('first', encoding='utf-8')\n"
        "result = 'first'"
    )
    duplicate_code = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('duplicate', encoding='utf-8')\n"
        "result = 'duplicate'"
    )

    first = bridge_tcp.send(
        "execute_code",
        {"code": first_code},
        request_id=operation_id,
    )
    duplicate = bridge_tcp.send(
        "execute_code",
        {"code": duplicate_code},
        request_id=operation_id,
    )

    assert first["ok"] is True
    assert duplicate["request_id"] == operation_id
    assert marker.read_text(encoding="utf-8") == "first"


def test_request_journal_is_reset_when_server_restarts(bridge_tcp):
    operation_id = "8" * 32
    response = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'before-restart'"},
        request_id=operation_id,
    )
    assert response["ok"] is True
    assert _request_status(bridge_tcp, operation_id)["ok"] is True

    bridge_tcp.server.stop_server()
    bridge_tcp.server.start_server(port=bridge_tcp.port)
    _wait_until(bridge_tcp.server.is_running)

    after_restart = _request_status(bridge_tcp, operation_id)
    assert after_restart["ok"] is False
    assert operation_id in str(after_restart)


def test_delivered_final_status_does_not_unblock_without_explicit_ack(bridge_tcp):
    operation_id = "a" * 32
    first = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'awaiting-ack'"},
        request_id=operation_id,
        auto_ack=False,
    )
    assert first["ok"] is True
    assert first["ack_required"] is True

    delivered = _request_status(bridge_tcp, operation_id, auto_ack=False)
    _assert_status_metadata(delivered, operation_id=operation_id, status="succeeded")
    assert "ack_required" not in delivered

    blocked = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'must-remain-blocked'"},
        request_id="b" * 32,
        auto_ack=False,
    )
    assert blocked["ok"] is False
    assert operation_id in str(blocked)

    bridge_tcp.send(
        "ack_request_result",
        {"operation_id": "c" * 32},
        auto_ack=False,
    )
    still_blocked = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'mismatch-must-not-unblock'"},
        request_id="d" * 32,
        auto_ack=False,
    )
    assert still_blocked["ok"] is False
    assert operation_id in str(still_blocked)

    acknowledged = bridge_tcp.send(
        "ack_request_result",
        {"operation_id": operation_id},
        auto_ack=False,
    )
    assert acknowledged["ok"] is True

    recovered_id = "e" * 32
    recovered = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'after-explicit-ack'"},
        request_id=recovered_id,
        auto_ack=False,
    )
    assert recovered["ok"] is True
    bridge_tcp.send(
        "ack_request_result",
        {"operation_id": recovered_id},
        auto_ack=False,
    )


def test_ack_while_request_is_non_final_does_not_remove_final_ack_barrier(
    bridge_tcp,
):
    operation_id = "f" * 32
    response_box = {}
    bridge_tcp.pump_enabled.clear()

    def execute():
        response_box["value"] = bridge_tcp.send(
            "execute_code",
            {"code": "result = 'final-after-early-ack'"},
            request_id=operation_id,
            auto_ack=False,
        )

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    try:
        queued = _wait_for_status(bridge_tcp, operation_id, "queued")
        _assert_status_metadata(queued, operation_id=operation_id, status="queued")

        bridge_tcp.send(
            "ack_request_result",
            {"operation_id": operation_id},
            auto_ack=False,
        )
        bridge_tcp.pump_enabled.set()
        _wait_until(lambda: "value" in response_box)
        assert response_box["value"]["ok"] is True

        blocked = bridge_tcp.send(
            "execute_code",
            {"code": "result = 'early-ack-must-not-count'"},
            request_id="0" * 32,
            auto_ack=False,
        )
        assert blocked["ok"] is False
        assert operation_id in str(blocked)

        bridge_tcp.send(
            "ack_request_result",
            {"operation_id": operation_id},
            auto_ack=False,
        )
    finally:
        bridge_tcp.pump_enabled.set()
        worker.join(timeout=2)


def test_blender_bridge_acks_received_execute_and_status_results(
    bridge_tcp, monkeypatch
):
    import bridge as bridge_client

    monkeypatch.setattr(bridge_client, "_TOKEN_FILE", bridge_tcp.server._TOKEN_FILE)
    client = bridge_client.BlenderBridge(port=bridge_tcp.port)

    executed = client.send("execute_code", {"code": "result = 'client-ack'"})
    assert executed["ok"] is True
    assert "ack_required" not in executed

    raw_id = "1a" * 16
    raw = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'raw-awaiting-status-ack'"},
        request_id=raw_id,
        auto_ack=False,
    )
    assert raw["ok"] is True

    status = client.send("get_request_status", {"operation_id": raw_id})
    _assert_status_metadata(status, operation_id=raw_id, status="succeeded")
    assert "ack_required" not in status

    after_status_ack = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'status-was-acked'"},
        request_id="1b" * 16,
    )
    assert after_status_ack["ok"] is True


def test_blender_bridge_does_not_ack_duplicate_execute_response(
    bridge_tcp, monkeypatch
):
    import bridge as bridge_client

    operation_id = "2a" * 16
    monkeypatch.setattr(bridge_client, "_TOKEN_FILE", bridge_tcp.server._TOKEN_FILE)
    first = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'original'"},
        request_id=operation_id,
        auto_ack=False,
    )
    assert first["ok"] is True

    fixed_uuid = uuid.UUID(hex=operation_id)
    monkeypatch.setattr(bridge_client.uuid, "uuid4", lambda: fixed_uuid)
    duplicate = bridge_client.BlenderBridge(port=bridge_tcp.port).send(
        "execute_code",
        {"code": "result = 'duplicate-must-not-run'"},
    )
    assert duplicate["request_id"] == operation_id
    assert "ack_required" not in duplicate

    still_blocked = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'still-blocked-after-duplicate'"},
        request_id="2b" * 16,
        auto_ack=False,
    )
    assert still_blocked["ok"] is False
    assert operation_id in str(still_blocked)

    bridge_tcp.send(
        "ack_request_result",
        {"operation_id": operation_id},
        auto_ack=False,
    )


def test_connection_timeout_before_socket_exists_is_not_outcome_unknown(
    bridge_tcp, monkeypatch
):
    import bridge as bridge_client

    monkeypatch.setattr(bridge_client, "_TOKEN_FILE", bridge_tcp.server._TOKEN_FILE)

    def fail_before_connect(*args, **kwargs):
        raise TimeoutError("simulated connection timeout")

    monkeypatch.setattr(bridge_client.socket, "create_connection", fail_before_connect)
    response = bridge_client.BlenderBridge(port=bridge_tcp.port).send(
        "execute_code",
        {"code": "result = 'never-sent'"},
    )

    assert response["ok"] is False
    assert "status" not in response
    assert "request_id" not in response
    assert "outcome_unknown" not in str(response)
    assert "get_request_status" not in str(response)


def test_client_timeout_leaves_final_result_blocked_until_status_ack(
    bridge_tcp, monkeypatch
):
    import bridge as bridge_client

    operation_id = "3a" * 16
    monkeypatch.setattr(bridge_client, "_TOKEN_FILE", bridge_tcp.server._TOKEN_FILE)
    monkeypatch.setattr(
        bridge_client.uuid,
        "uuid4",
        lambda: uuid.UUID(hex=operation_id),
    )
    real_create_connection = bridge_client.socket.create_connection

    class TimeoutAfterSend:
        def __init__(self, connection):
            self._connection = connection

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self._connection.close()

        def settimeout(self, timeout):
            self._connection.settimeout(timeout)

        def sendall(self, payload):
            self._connection.sendall(payload)

        def recv(self, size):
            raise TimeoutError("simulated client receive timeout")

    def connect_then_timeout(*args, **kwargs):
        return TimeoutAfterSend(real_create_connection(*args, **kwargs))

    monkeypatch.setattr(bridge_client.socket, "create_connection", connect_then_timeout)
    client = bridge_client.BlenderBridge(port=bridge_tcp.port)
    timed_out = client.send("execute_code", {"code": "result = 'server-finished'"})
    monkeypatch.setattr(bridge_client.socket, "create_connection", real_create_connection)

    assert timed_out["ok"] is False
    assert timed_out["status"] == "outcome_unknown"
    assert timed_out["request_id"] == operation_id

    final = _wait_until(
        lambda: (
            response
            if (response := _request_status(bridge_tcp, operation_id, auto_ack=False)).get(
                "ok"
            )
            and response["data"].get("status") == "succeeded"
            else None
        )
    )
    _assert_status_metadata(final, operation_id=operation_id, status="succeeded")

    blocked = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'must-wait-for-ack'"},
        request_id="3b" * 16,
        auto_ack=False,
    )
    assert blocked["ok"] is False
    assert operation_id in str(blocked)

    status = client.send("get_request_status", {"operation_id": operation_id})
    _assert_status_metadata(status, operation_id=operation_id, status="succeeded")

    recovered = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'after-status-ack'"},
        request_id="3c" * 16,
    )
    assert recovered["ok"] is True


def test_non_execute_holder_timeout_is_not_reported_as_outcome_unknown(bridge_tcp):
    bridge_tcp.server._HOLDER_TIMEOUT_S = 0.1
    bridge_tcp.pump_enabled.clear()
    try:
        response = bridge_tcp.send("ping", {}, timeout=2, auto_ack=False)
    finally:
        bridge_tcp.pump_enabled.set()

    assert response["ok"] is False
    assert "Timed out waiting" in response["error"]["message"]
    assert "outcome_unknown" not in str(response)
    assert "get_request_status" not in str(response)
