import json
import sys

from tools import bridge_register


def _run_register(monkeypatch, tmp_path, capsys):
    bridge_file = tmp_path / "claude" / "blender-bridge-session.json"
    monkeypatch.setattr(bridge_register, "BRIDGE_FILE", bridge_file)
    monkeypatch.setattr(sys, "argv", ["bridge_register.py", "--cwd", str(tmp_path / "project")])
    bridge_register.main()
    return json.loads(bridge_file.read_text(encoding="utf-8")), capsys.readouterr().out


def test_register_prefers_claude_code_session_environment(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "environment-session-12345678")
    monkeypatch.setattr(
        bridge_register,
        "latest_session",
        lambda _cwd: (_ for _ in ()).throw(AssertionError()),
    )

    data, output = _run_register(monkeypatch, tmp_path, capsys)

    assert data["session_id"] == "environment-session-12345678"
    assert "(env)" in output


def test_register_falls_back_to_latest_session_without_environment(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(bridge_register, "latest_session", lambda _cwd: "fallback-session-87654321")

    data, output = _run_register(monkeypatch, tmp_path, capsys)

    assert data["session_id"] == "fallback-session-87654321"
    assert "照合してね" in output
