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
