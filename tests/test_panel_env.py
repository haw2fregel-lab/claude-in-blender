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
    # repo キーを書かない旧形式なので、cwd 自身がアドオンのソースを兼ねる。
    (tmp_path / "mcp_server").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mcp_server" / "server.py").write_text("", encoding="utf-8")
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


# GUI 起動の macOS は PATH が痩せて which が外れる——~/.local/bin の直接参照が本線。
# .exe が先なのは、Windows に Git Bash 用の拡張子なしシムが同居していても
# subprocess で実行できる方を返すため。
@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (("claude.exe",), "claude.exe"),
        (("claude",), "claude"),
        (("claude.exe", "claude"), "claude.exe"),
        ((), None),
    ],
)
def test_find_claude_local_bin_fallback(monkeypatch, tmp_path, names, expected):
    monkeypatch.setattr(panel.shutil, "which", lambda _name: None)
    monkeypatch.setattr(panel.Path, "home", lambda: tmp_path)
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name in names:
        (bin_dir / name).write_text("", encoding="utf-8")

    got = panel._find_claude(None)

    assert got == (str(bin_dir / expected) if expected else None)


# .mcp.json の command は ${CLAUDE_IN_BLENDER_PYTHON:-python}。
# macOS は python コマンドが無いことが多く、fork spawn 時に実体パスを変数で渡す。
class TestPythonForMcp:
    @pytest.fixture(autouse=True)
    def _clean_variable(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_IN_BLENDER_PYTHON", raising=False)

    def test_windows_returns_none_and_leaves_the_default(self, monkeypatch):
        monkeypatch.setattr(panel.os, "name", "nt")

        assert panel._python_for_mcp("C:/bin") is None

    def test_existing_variable_is_respected(self, monkeypatch):
        monkeypatch.setattr(panel.os, "name", "posix")
        monkeypatch.setenv("CLAUDE_IN_BLENDER_PYTHON", "/opt/custom/python")

        assert panel._python_for_mcp("/bin") is None

    def test_posix_prefers_python_over_python3(self, monkeypatch):
        monkeypatch.setattr(panel.os, "name", "posix")
        found = {"python": "/usr/bin/python", "python3": "/usr/bin/python3"}
        monkeypatch.setattr(
            panel.shutil, "which", lambda name, path=None: found.get(name)
        )

        assert panel._python_for_mcp("/bin") == "/usr/bin/python"

    def test_posix_falls_back_to_python3(self, monkeypatch):
        monkeypatch.setattr(panel.os, "name", "posix")
        found = {"python3": "/opt/homebrew/bin/python3"}
        monkeypatch.setattr(
            panel.shutil, "which", lambda name, path=None: found.get(name)
        )

        assert panel._python_for_mcp("/bin") == "/opt/homebrew/bin/python3"

    def test_posix_returns_none_when_no_python_exists(self, monkeypatch):
        monkeypatch.setattr(panel.os, "name", "posix")
        monkeypatch.setattr(panel.shutil, "which", lambda name, path=None: None)

        assert panel._python_for_mcp("/bin") is None

    def test_lookup_uses_the_injected_login_shell_path(self, monkeypatch):
        monkeypatch.setattr(panel.os, "name", "posix")
        seen_paths = []

        def fake_which(name, path=None):
            seen_paths.append(path)
            return None

        monkeypatch.setattr(panel.shutil, "which", fake_which)
        panel._python_for_mcp("/fat/bin:/usr/bin")

        assert set(seen_paths) == {"/fat/bin:/usr/bin"}


# os.name を posix に差し替えたまま _run_claude を通すと Windows の pathlib が
# 壊れる（3.13 は実行時に os.name を見る）ので、統合側は判定関数をモックして
# 配線——返り値が child env に載ること——だけを見る。判定自体は上の単体が担保。
def _spawn_seen_env(monkeypatch, tmp_path, python_for_mcp):
    # repo キーを書かない旧形式なので、cwd 自身がアドオンのソースを兼ねる。
    (tmp_path / "mcp_server").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mcp_server" / "server.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        panel,
        "_load_bridge",
        lambda: {"cwd": str(tmp_path),
                 "session_id": "11111111-1111-1111-1111-111111111111"},
    )
    monkeypatch.setattr(panel, "_find_claude", lambda _bridge: "claude")
    monkeypatch.setattr(panel, "_login_shell_path", lambda: "/injected/bin")
    monkeypatch.setattr(panel, "_python_for_mcp", lambda _path: python_for_mcp)
    seen = {}

    def fake_run(_cmd, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    panel._run_claude("prompt")
    return seen["env"]


def test_run_claude_injects_python_for_mcp(monkeypatch, tmp_path):
    env = _spawn_seen_env(monkeypatch, tmp_path, "/injected/bin/python3")

    assert env["CLAUDE_IN_BLENDER_PYTHON"] == "/injected/bin/python3"


def test_run_claude_leaves_env_alone_without_python_for_mcp(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_IN_BLENDER_PYTHON", raising=False)

    env = _spawn_seen_env(monkeypatch, tmp_path, None)

    assert "CLAUDE_IN_BLENDER_PYTHON" not in env
