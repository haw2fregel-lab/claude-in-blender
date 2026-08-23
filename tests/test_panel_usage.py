"""_parse_stream_json — usage は最終コール単体を使う（result の合算は使わない）。"""
import json
from types import SimpleNamespace

import pytest

from claude_bridge import panel


def _assistant(read, write):
    return json.dumps({
        "type": "assistant",
        "message": {"usage": {
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
            "input_tokens": 2,
            "output_tokens": 5,
        }},
    })


def _result(read, write, text="done"):
    return json.dumps({
        "type": "result", "result": text, "is_error": False,
        "session_id": "00000000-0000-0000-0000-000000000000",
        "usage": {"cache_read_input_tokens": read,
                  "cache_creation_input_tokens": write},
    })


def test_usage_is_last_call_not_result_total():
    # 実測の形: call1 がほぼ全書き込み、call2 が読み直し。result は縦の合算。
    stdout = "\n".join([
        _assistant(2_575, 19_717),
        _assistant(22_292, 119),
        _result(24_867, 19_836),
    ])
    result_event, last_usage = panel._parse_stream_json(stdout)
    assert result_event["result"] == "done"
    assert last_usage["cache_read_input_tokens"] == 22_292
    assert last_usage["cache_creation_input_tokens"] == 119


def test_no_result_event_returns_none():
    result_event, last_usage = panel._parse_stream_json(_assistant(100, 50))
    assert result_event is None
    assert last_usage["cache_read_input_tokens"] == 100


def test_broken_lines_and_noise_are_skipped():
    stdout = "\n".join([
        "",
        "not json at all {{{",
        json.dumps(["array", "not", "dict"]),
        json.dumps({"type": "system", "subtype": "init"}),
        _assistant(300, 7),
        json.dumps({"type": "assistant", "message": {}}),  # usage 無し → 前の値を保つ
        _result(300, 7),
    ])
    result_event, last_usage = panel._parse_stream_json(stdout)
    assert result_event is not None
    assert last_usage["cache_read_input_tokens"] == 300
    assert last_usage["cache_creation_input_tokens"] == 7


def test_empty_stdout():
    result_event, last_usage = panel._parse_stream_json("")
    assert result_event is None
    assert last_usage == {}


def _run_with_stdout(monkeypatch, tmp_path, stdout):
    # repo キーを書かない旧形式なので、cwd 自身がアドオンのソースを兼ねる。
    (tmp_path / "mcp_server").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mcp_server" / "server.py").write_text("", encoding="utf-8")
    session_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(
        panel,
        "_load_bridge",
        lambda: {"cwd": str(tmp_path), "session_id": session_id},
    )
    monkeypatch.setattr(panel, "_find_claude", lambda _bridge: "claude")
    monkeypatch.setattr(
        panel.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=stdout, stderr="", returncode=0
        ),
    )

    panel._run_claude("prompt")
    return panel._result_box


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {
                "cache_read_input_tokens": 10,
                "input_tokens": 2,
                "cache_creation_input_tokens": 5,
            },
            "Cache reused 10 / new 7",
        ),
        (
            {
                "cache_read_input_tokens": 0,
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "Cache reused 0 / new 0",
        ),
        ({"cache_read_input_tokens": 9}, "Cache reused 9 / new 0"),
        ({"input_tokens": 3}, "Cache reused 0 / new 3"),
        (
            {"cache_creation_input_tokens": 4},
            "Cache reused 0 / new 4",
        ),
    ],
)
def test_run_claude_displays_last_usage_with_regular_and_cache_creation_input(
    monkeypatch, tmp_path, usage, expected
):
    stdout = "\n".join(
        [
            json.dumps({"type": "assistant", "message": {"usage": usage}}),
            json.dumps(
                {
                    "type": "result",
                    "result": "done",
                    "is_error": False,
                    "session_id": "11111111-1111-1111-1111-111111111111",
                }
            ),
        ]
    )

    result = _run_with_stdout(monkeypatch, tmp_path, stdout)

    assert result["ready"] is True
    assert result["error"] is False
    assert result["usage"] == expected


def test_run_claude_marks_missing_result_event_as_error(monkeypatch, tmp_path):
    result = _run_with_stdout(monkeypatch, tmp_path, _assistant(100, 50))

    assert result["ready"] is True
    assert result["error"] is True
    assert "no result event" in result["text"]
