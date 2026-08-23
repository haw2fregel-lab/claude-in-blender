import os
import re
import socket
import threading
import time

import bpy
import pytest

from claude_bridge import bridge_server


# エラー応答の敷居（作業票の契約: message + traceback の合計 2048 bytes）。
MAX_ERROR_BYTES = 2048
ENVELOPE_METADATA_KEYS = ("request_id", "ack_required", "status", "blocker_request_id")
# message 側にだけ巨大文を作る。code 自体は短いので、ログ検査が code の写しで通ることはない。
OVERSIZED_ERROR_CODE = "raise RuntimeError('HUGE' + 'HEAD' + 'x' * 100000 + 'TAIL' + 'MARK')"
OVERSIZED_ERROR_BYTES = 100000
LOG_NOTICE_RE = re.compile(
    r" \.\.\. \(truncated; full error in claude_bridge_log, "
    r"original (?P<original>\d{1,3}(?:,\d{3})*) bytes\)$"
)


def _error_bytes(error):
    return sum(len((error.get(key) or "").encode("utf-8")) for key in ("message", "traceback"))


def _envelope_metadata(response):
    return {key: response[key] for key in ENVELOPE_METADATA_KEYS if key in response}


def _bridge_log_writes():
    written = []
    for call in bpy.data.texts.mock_calls:
        written.extend(argument for argument in call.args if isinstance(argument, str))
        written.extend(value for value in call.kwargs.values() if isinstance(value, str))
    return "".join(written)


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


def test_large_successful_response_keeps_envelope_metadata_when_truncated(bridge_tcp):
    small_id = "5a" * 16
    truncated_id = "5b" * 16

    small = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'small'"},
        request_id=small_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": small_id}, auto_ack=False)
    truncated = bridge_tcp.send(
        "execute_code",
        {"code": f"result = 'x' * {bridge_tcp.server._MAX_RESPONSE_BYTES + 1024}"},
        request_id=truncated_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": truncated_id}, auto_ack=False)

    assert truncated["ok"] is True
    assert truncated["data"]["output_truncated"] is True
    assert _envelope_metadata(truncated) == dict(
        _envelope_metadata(small), request_id=truncated_id
    )
    # 切り詰め形式は従来のまま——data 全体を json.dumps した文字列の頭が入る
    # （JSON の包み紙が混ざる形。是正するなら封筒契約の変更で、このテストが対象）。
    # 警告行を足すのは MCP 側の仕事で、封筒には混ぜない。
    assert truncated["data"]["result"].startswith('{"result": "x')
    assert "[output truncated" not in truncated["data"]["result"]
    assert len(truncated["data"]["result"]) < truncated["data"]["original_bytes"]


def test_error_within_the_size_limit_is_returned_unchanged(bridge_tcp):
    operation_id = "6a" * 16

    response = bridge_tcp.send(
        "execute_code",
        {"code": "raise RuntimeError('boom')"},
        request_id=operation_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": operation_id}, auto_ack=False)

    assert response["ok"] is False
    assert set(response["error"]) == {"message", "traceback"}
    assert "RuntimeError: boom" in response["error"]["message"]
    assert "RuntimeError" in response["error"]["traceback"]
    assert _error_bytes(response["error"]) <= MAX_ERROR_BYTES
    assert "truncated" not in response["error"]["message"]
    assert "claude_bridge_log" not in response["error"]["message"]
    assert response["request_id"] == operation_id


def test_oversized_error_is_capped_and_points_at_the_bridge_log(bridge_tcp):
    operation_id = "6b" * 16

    response = bridge_tcp.send(
        "execute_code",
        {"code": OVERSIZED_ERROR_CODE},
        request_id=operation_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": operation_id}, auto_ack=False)

    assert response["ok"] is False
    assert _error_bytes(response["error"]) <= MAX_ERROR_BYTES
    message = response["error"]["message"]
    # エラー型と message の頭は残る。
    assert "RuntimeError" in message
    assert "HUGEHEAD" in message
    notice = LOG_NOTICE_RE.search(message)
    assert notice is not None, message[-200:]
    assert int(notice.group("original").replace(",", "")) >= OVERSIZED_ERROR_BYTES


def test_oversized_error_keeps_the_full_text_in_the_bridge_log(bridge_tcp):
    operation_id = "6c" * 16
    bpy.data.texts.reset_mock()

    response = bridge_tcp.send(
        "execute_code",
        {"code": OVERSIZED_ERROR_CODE},
        request_id=operation_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": operation_id}, auto_ack=False)

    assert response["ok"] is False
    logged = _bridge_log_writes()
    assert "claude_bridge_log" in logged
    assert "HUGEHEAD" in logged
    assert "TAILMARK" in logged
    assert len(logged) >= OVERSIZED_ERROR_BYTES


def test_oversized_error_keeps_the_metadata_a_small_error_carries(bridge_tcp):
    small_id = "6d" * 16
    oversized_id = "6e" * 16

    small = bridge_tcp.send(
        "execute_code",
        {"code": "raise RuntimeError('boom')"},
        request_id=small_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": small_id}, auto_ack=False)
    oversized = bridge_tcp.send(
        "execute_code",
        {"code": OVERSIZED_ERROR_CODE},
        request_id=oversized_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": oversized_id}, auto_ack=False)

    assert small["ok"] is False
    assert oversized["ok"] is False
    assert oversized["request_id"] == oversized_id
    assert _envelope_metadata(oversized) == dict(
        _envelope_metadata(small), request_id=oversized_id
    )


def test_next_mutation_runs_after_acking_an_oversized_error(bridge_tcp):
    oversized_id = "6f" * 16
    recovered_id = "70" * 16

    oversized = bridge_tcp.send(
        "execute_code",
        {"code": OVERSIZED_ERROR_CODE},
        request_id=oversized_id,
        auto_ack=False,
    )
    assert oversized["ok"] is False
    assert oversized["request_id"] == oversized_id

    acknowledged = bridge_tcp.send(
        "ack_request_result",
        {"operation_id": oversized["request_id"]},
        auto_ack=False,
    )
    assert acknowledged["ok"] is True

    recovered = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'after-oversized-error'"},
        request_id=recovered_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": recovered_id}, auto_ack=False)

    assert recovered["ok"] is True
    assert recovered["data"]["result"] == "after-oversized-error"


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


# --- 契約: 起動が失敗した理由を外から読める（票B2 契約5 / 完了条件 (e)） ---
# 起動に失敗した理由を module が保ち、panel が読める公開関数で取れること。
# 名前は実装側の決めどころ。別名になったら、この 2 行を直せば残りは通る。
STARTUP_ERROR_GETTER = "get_startup_error"
STARTUP_ERROR_ATTR = "_startup_error"


def _startup_error():
    getter = getattr(bridge_server, STARTUP_ERROR_GETTER, None)
    assert callable(getter), (
        f"起動失敗の理由を返す公開関数が無い: bridge_server.{STARTUP_ERROR_GETTER}"
    )
    return getter()


@pytest.fixture
def startup_error_stays_here(monkeypatch):
    """記録された理由を、この test の外へ持ち出さない。

    monkeypatch は今の値を控えて teardown で戻すので、名前が合っていればテスト中に
    付いた理由は片付く（別名なら掃除が空振りするだけで、契約の検証は変わらない）。
    """
    if hasattr(bridge_server, STARTUP_ERROR_ATTR):
        monkeypatch.setattr(
            bridge_server, STARTUP_ERROR_ATTR, getattr(bridge_server, STARTUP_ERROR_ATTR)
        )


def test_a_busy_port_leaves_a_readable_startup_reason(
    tmp_path, monkeypatch, startup_error_stays_here
):
    token_file = tmp_path / "bridge-temp" / "blender-session-token"
    monkeypatch.setattr(bridge_server, "_TMP_DIR", str(token_file.parent))
    monkeypatch.setattr(bridge_server, "_TOKEN_FILE", str(token_file))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        # Windows は SO_REUSEADDR を付けた bind に port を横取りさせる。
        # 「使用中」を確実に作るため、排他で押さえてから渡す。
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]

        try:
            bridge_server.start_server(port=busy_port)
        except OSError:
            # bind の例外をそのまま投げる実装でも、理由が取れることが契約。
            pass
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _startup_error() is None:
                time.sleep(0.01)

            reason = _startup_error()
            assert isinstance(reason, str) and reason.strip(), (
                f"port {busy_port} を塞いだまま起動したのに理由が取れない: {reason!r}"
            )
            assert bridge_server.is_running() is False
        finally:
            bridge_server.stop_server()


def test_a_healthy_bridge_reports_no_startup_reason(bridge_tcp):
    # 起動が通っている間は None。前の失敗が残り続けると、パネルは動いている橋に
    # 停止理由を出してしまう。
    assert _startup_error() is None


