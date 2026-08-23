"""送信開始後に結果を確定できなかった経路の契約テスト。

request journal の目的は「Blender 側で変更済みか不明な時に再送しない」こと。だから
execute_code は、TCP 接続が立って送信が始まった後に応答を確定できなかったどの経路でも、
Claude が get_request_status へ進める request id を必ず受け取れなければならない。

実サーバ (claude_bridge/bridge_server.py) は使わない。異常だけを演じるローカルの TCP
フェイクを立て、`BlenderBridge.send()` が返す envelope だけを見る。タイムアウト待ちへ
入る経路は既存の tests/test_request_journal.py の領分なので、ここでは即座に決着する
異常だけを扱う。
"""

import json
import socket
import struct
import sys
import threading

import pytest


# 契約: 応答が 10MB を超えたら outcome_unknown。
RESPONSE_LIMIT_BYTES = 10 * 1024 * 1024

# フェイクが接続を握り続ける上限。上限超えの応答をクライアントが自力で切り上げるなら
# 待ちは発生しない。切り上げずにフェイクの close を待った時だけ、ここで頭打ちになる。
_HOLD_TIMEOUT_S = 5.0

# RST を出すための SO_LINGER。struct linger のレイアウトが Windows (u_short 2 本) と
# POSIX (int 2 本) で違うので、詰め方を分ける。
_LINGER_NOW = (
    struct.pack("hh", 1, 0) if sys.platform == "win32" else struct.pack("ii", 1, 0)
)


def _read_request_line(connection):
    connection.settimeout(5)
    buffer = b""
    while b"\n" not in buffer:
        chunk = connection.recv(8192)
        if not chunk:
            break
        buffer += chunk
    return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))


class FakeBlender:
    """リクエスト行を 1 本受けてから、渡された異常を演じるだけの TCP サーバ。

    実サーバの応答は要らない。見たいのは「送信は届いたのに結果が確定しない」時に
    クライアントが何を返すかだけなので、受けた行を記録して misbehave へ渡す。
    """

    def __init__(self, misbehave):
        self._misbehave = misbehave
        self.requests = []
        self.release = threading.Event()
        self.hold_ended = threading.Event()
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._listener.settimeout(0.05)
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self.requests.append(_read_request_line(connection))
                self._misbehave(connection, self)
            except OSError:
                pass
            finally:
                connection.close()

    def sent_request_id(self):
        """クライアントが実際に線へ流した request id。

        1 本しか受けていないことも併せて見る。結果が不明な送信を勝手に再送しないのが
        request journal の前提なので、2 本目が来ていたらその前提が崩れている。
        """
        assert len(self.requests) == 1, self.requests
        return self.requests[0]["request_id"]

    def close(self):
        self._stop.set()
        self.release.set()
        self._listener.close()
        self._thread.join(timeout=5)


@pytest.fixture
def fake_blender():
    servers = []

    def start(misbehave):
        server = FakeBlender(misbehave)
        servers.append(server)
        return server

    try:
        yield start
    finally:
        for server in servers:
            server.close()


def _client(port):
    """フェイクへ向けたクライアント。

    トークンファイルは差し替えない。フェイクはトークンを読まないので、実機の
    トークンが読めても読めなくても（読めなければ None が送られる）結果は変わらない。
    """
    import bridge as bridge_client

    return bridge_client.BlenderBridge(host="127.0.0.1", port=port)


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- 送信後に結果が確定しない異常たち ---


def _close_without_responding(connection, server):
    """応答を返さないまま接続を閉じる（EOF）。"""
    connection.close()


def _reset_connection(connection, server):
    """RST で叩き落とす（サーバ側の強制クローズ）。"""
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_NOW)
    connection.close()


def _send_malformed_json(connection, server):
    """改行までは届くが、JSON として閉じていない応答。"""
    connection.sendall(b'{"ok": true, "request_id": "cut", "data": {"result": "cut\n')


def _send_invalid_utf8(connection, server):
    """構造は JSON でも、UTF-8 として decode できない応答。"""
    connection.sendall(b'{"ok": true, "data": {"result": "\xff\xfe"}}\n')


def _send_oversized_response(connection, server):
    """10MB を超える応答。上限で自力で切り上げたか見たいので、送り終えても閉じない。"""
    try:
        connection.sendall(b"x" * (RESPONSE_LIMIT_BYTES + 4096))
    except OSError:
        pass  # 上限に達したクライアントが先に閉じた場合。閉じたのはこちらではない
    server.release.wait(timeout=_HOLD_TIMEOUT_S)
    server.hold_ended.set()


_POST_SEND_FAILURES = [
    pytest.param(_close_without_responding, id="eof"),
    pytest.param(_reset_connection, id="connection-reset"),
    pytest.param(_send_malformed_json, id="malformed-json"),
    pytest.param(_send_invalid_utf8, id="invalid-utf8"),
    pytest.param(_send_oversized_response, id="response-too-large"),
]


def _assert_outcome_unknown(response, *, sent_request_id):
    assert response["ok"] is False, response
    assert response["status"] == "outcome_unknown", response
    assert response["request_id"] == sent_request_id, response
    message = response["error"]["message"]
    assert "Outcome unknown" in message, message
    assert "get_request_status" in message, message
    assert isinstance(response["elapsed_ms"], int), response
    assert response["elapsed_ms"] >= 0, response


def _assert_plain_connection_error(response):
    assert response["ok"] is False, response
    assert response.get("status") != "outcome_unknown", response
    assert "outcome_unknown" not in str(response), response
    assert "get_request_status" not in str(response), response
    assert isinstance(response["error"]["message"], str), response
    assert response["error"]["message"], response


@pytest.mark.parametrize("misbehave", _POST_SEND_FAILURES)
def test_execute_code_returns_outcome_unknown_when_the_outcome_cannot_be_confirmed(
    fake_blender, misbehave
):
    server = fake_blender(misbehave)

    response = _client(server.port).send(
        "execute_code",
        {"code": "result = 'may-or-may-not-have-run'"},
    )

    _assert_outcome_unknown(response, sent_request_id=server.sent_request_id())


def test_execute_code_stops_at_the_response_limit_without_waiting_for_the_server(
    fake_blender,
):
    server = fake_blender(_send_oversized_response)

    response = _client(server.port).send("execute_code", {"code": "result = 'flood'"})

    assert server.hold_ended.is_set() is False, (
        "10MB 超で自力で切り上げず、フェイクが接続を閉じるまで待っている"
    )
    _assert_outcome_unknown(response, sent_request_id=server.sent_request_id())


@pytest.mark.parametrize("misbehave", _POST_SEND_FAILURES)
def test_non_execute_command_keeps_the_plain_connection_error(fake_blender, misbehave):
    server = fake_blender(misbehave)

    response = _client(server.port).send("get_scene_info", {})

    assert server.requests, "リクエストがフェイクへ届いていない（送信前で落ちている）"
    _assert_plain_connection_error(response)


def test_connection_refused_before_anything_is_sent_is_not_outcome_unknown():
    response = _client(_unused_port()).send(
        "execute_code",
        {"code": "result = 'never-sent'"},
    )

    assert response["ok"] is False, response
    assert "status" not in response, response
    assert "request_id" not in response, response
    assert "outcome_unknown" not in str(response), response
    assert "get_request_status" not in str(response), response
