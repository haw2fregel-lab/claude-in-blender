"""fork spawn の env — GUI 起動で痩せた PATH を login shell から取り直して注入する。"""
from types import SimpleNamespace

import pytest

from claude_bridge import panel


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    # キャッシュはモジュールグローバル。テストごとに空にして独立させる。
    monkeypatch.setattr(panel, "_LOGIN_SHELL_PATH_CACHE", None)


def test_windows_keeps_current_path_without_spawning_shell(monkeypatch):
    monkeypatch.setattr(panel.os, "name", "nt")
    monkeypatch.setenv("PATH", r"C:\keep\this")

    def boom(*_args, **_kwargs):
        raise AssertionError("shell must not be spawned on Windows")

    monkeypatch.setattr(panel.subprocess, "run", boom)
    assert panel._login_shell_path() == r"C:\keep\this"


def test_posix_takes_path_from_login_shell(monkeypatch):
    monkeypatch.setattr(panel.os, "name", "posix")
    monkeypatch.setenv("PATH", "/thin")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    seen = {}

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(stdout="/home/u/.local/bin:/usr/bin", stderr="")

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    assert panel._login_shell_path() == "/home/u/.local/bin:/usr/bin"
    assert seen["cmd"][0] == "/bin/zsh"
    assert "-l" in seen["cmd"]


def test_posix_falls_back_to_current_path_when_shell_fails(monkeypatch):
    monkeypatch.setattr(panel.os, "name", "posix")
    monkeypatch.setenv("PATH", "/thin")

    def boom(*_args, **_kwargs):
        raise OSError("no shell")

    monkeypatch.setattr(panel.subprocess, "run", boom)
    assert panel._login_shell_path() == "/thin"


def test_second_call_uses_cache_without_spawning_again(monkeypatch):
    monkeypatch.setattr(panel.os, "name", "posix")
    monkeypatch.setenv("PATH", "/thin")
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout="/fat", stderr="")

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    assert panel._login_shell_path() == "/fat"
    assert panel._login_shell_path() == "/fat"
    assert len(calls) == 1


def test_run_claude_passes_env_with_injected_path(monkeypatch, tmp_path):
    (tmp_path / ".mcp.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        panel,
        "_load_bridge",
        lambda: {"cwd": str(tmp_path),
                 "session_id": "11111111-1111-1111-1111-111111111111"},
    )
    monkeypatch.setattr(panel, "_find_claude", lambda _bridge: "claude")
    monkeypatch.setattr(panel, "_login_shell_path", lambda: "/injected/bin")
    seen = {}

    def fake_run(_cmd, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    panel._run_claude("prompt")
    assert seen["env"]["PATH"] == "/injected/bin"
