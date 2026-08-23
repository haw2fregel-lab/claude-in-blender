"""契約: capture_after の失敗を、実行の成功を保ったまま見せる（票B2 契約3 / 完了条件 (c)）。

撮影がどの bridge command で飛ぶかは実装側の決めどころなので、command 名では分けない。
「最初の呼びがコードの実行、その後の呼びが撮影」という並びだけを仮定する。
"""

import pytest

from mcp_server import server


CAPTURE_FAILURE_PREFIX = "Execution succeeded, but capture_after failed:"
EXECUTION_RESULT = "scene-ready"
CAPTURE_FAILURE_REASON = "viewport capture refused"


class FakeBridge:
    """1 回目の呼び（実行）と 2 回目以降の呼び（撮影）へ別々に答える bridge。"""

    def __init__(self):
        self.commands = []
        self.execution = {"ok": True, "data": {"result": EXECUTION_RESULT}}
        self.capture = {"ok": False, "error": {"message": CAPTURE_FAILURE_REASON}}

    def respond(self, command):
        self.commands.append(command)
        return self.execution if len(self.commands) == 1 else self.capture


@pytest.fixture
def fake_bridge(monkeypatch, tmp_path):
    import bridge as bridge_client

    token_file = tmp_path / "blender-session-token"
    token_file.write_text("test-token\n", encoding="utf-8")
    monkeypatch.setattr(bridge_client, "_TOKEN_FILE", str(token_file))

    bridge = FakeBridge()

    def fake_send(self, command, params=None, *args, **kwargs):
        return bridge.respond(command)

    monkeypatch.setattr(bridge_client.BlenderBridge, "send", fake_send)
    return bridge


def _text(returned):
    # MCP ツールの返りは list[TextContent | ImageContent]。文字の側だけを繋いで読む。
    return "\n".join(
        item.text for item in returned if getattr(item, "type", None) == "text"
    )


def test_capture_after_failure_is_appended_to_the_successful_result(fake_bridge):
    returned = server.execute_code("result = 1", capture_after=True)

    assert fake_bridge.commands[:1] == ["execute_code"]
    assert len(fake_bridge.commands) > 1, (
        f"capture_after=True でも撮影の呼びが出ていない: {fake_bridge.commands}"
    )
    result_text, marker, reason = _text(returned).partition(CAPTURE_FAILURE_PREFIX)
    assert marker == CAPTURE_FAILURE_PREFIX, _text(returned)
    # 実行結果は頭にそのまま残り、断り書きはその後ろへ付く。
    assert result_text.startswith(EXECUTION_RESULT)
    assert CAPTURE_FAILURE_REASON in reason


def test_the_result_stays_plain_when_no_capture_is_requested(fake_bridge):
    returned = server.execute_code("result = 1")

    assert fake_bridge.commands == ["execute_code"]
    assert _text(returned) == EXECUTION_RESULT


def test_a_failed_execution_never_claims_success(fake_bridge):
    fake_bridge.execution = {"ok": False, "error": {"message": "RuntimeError: boom"}}

    try:
        text = _text(server.execute_code("raise RuntimeError('boom')", capture_after=True))
    except RuntimeError as error:
        # 実行が失敗した時の返し方（例外か本文か）は契約の外。どちらでも文言だけを見る。
        text = str(error)

    assert "Execution succeeded" not in text
    assert CAPTURE_FAILURE_PREFIX not in text
