import json

import pytest

from mcp_server import server


@pytest.fixture
def execute_code_returning(monkeypatch, tmp_path):
    import bridge as bridge_client

    token_file = tmp_path / "blender-session-token"
    token_file.write_text("test-token\n", encoding="utf-8")
    monkeypatch.setattr(bridge_client, "_TOKEN_FILE", str(token_file))

    payload = {}
    calls = []

    def fake_send(self, command, params=None, *args, **kwargs):
        calls.append(command)
        return {"ok": True, "data": payload["data"]}

    monkeypatch.setattr(bridge_client.BlenderBridge, "send", fake_send)

    def run(data):
        payload["data"] = data
        calls.clear()
        returned = server.execute_code("result = 1")
        assert calls, "execute_code が bridge を呼んでいない"
        # MCP ツールの返りは list[TextContent]。契約の「返り文字列」は中の text
        assert len(returned) == 1 and returned[0].type == "text"
        return returned[0].text

    return run


def test_truncated_output_gets_a_leading_warning_line(execute_code_returning):
    returned = execute_code_returning(
        {"result": "x" * 100, "output_truncated": True, "original_bytes": 1234567}
    )

    first_line, newline, body = returned.partition("\n")
    assert first_line == "[output truncated: showing first ~50 KB of 1,234,567 bytes]"
    assert newline == "\n"
    assert body == "x" * 100


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"result": None}, "OK"),
        ({"result": "plain body"}, "plain body"),
        (
            {"result": "plain body", "output_truncated": False, "original_bytes": 12},
            "plain body",
        ),
    ],
)
def test_output_without_truncation_keeps_its_previous_shape(
    execute_code_returning, data, expected
):
    assert execute_code_returning(data) == expected


def test_structured_result_without_truncation_is_still_json(execute_code_returning):
    returned = execute_code_returning({"result": {"objects": 3, "name": "Cube"}})

    assert json.loads(returned) == {"objects": 3, "name": "Cube"}
