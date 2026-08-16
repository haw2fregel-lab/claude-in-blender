import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools import bridge_register


def _run_register(monkeypatch, tmp_path, capsys, extra_args=()):
    bridge_file = tmp_path / "claude" / "blender-bridge-session.json"
    monkeypatch.setattr(bridge_register, "BRIDGE_FILE", bridge_file)
    monkeypatch.setattr(
        sys,
        "argv",
        ["bridge_register.py", "--cwd", str(tmp_path / "project"), *extra_args],
    )
    bridge_register.main()
    return json.loads(bridge_file.read_text(encoding="utf-8")), capsys.readouterr().out


# 外から登録された ID は掴み直す相手ではなく fork 元。
# どの経路で来ても fork_from に入り、session_id は null のまま残る。
def test_register_prefers_claude_code_session_environment(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "environment-session-12345678")
    monkeypatch.setattr(
        bridge_register,
        "latest_session",
        lambda _cwd: (_ for _ in ()).throw(AssertionError()),
    )

    data, output = _run_register(monkeypatch, tmp_path, capsys)

    assert data["fork_from"] == "environment-session-12345678"
    assert data["session_id"] is None
    assert "(env)" in output


def test_register_falls_back_to_latest_session_without_environment(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(bridge_register, "latest_session", lambda _cwd: "fallback-session-87654321")

    data, output = _run_register(monkeypatch, tmp_path, capsys)

    assert data["fork_from"] == "fallback-session-87654321"
    assert data["session_id"] is None
    assert "照合してね" in output


def test_register_explicit_session_id_becomes_the_fork_source(monkeypatch, tmp_path, capsys):
    bridge_file = tmp_path / "claude" / "blender-bridge-session.json"
    bridge_file.parent.mkdir(parents=True)
    bridge_file.write_text(
        json.dumps(
            {
                "cwd": "D:/old-project",
                "session_id": "11111111-1111-1111-1111-111111111111",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        bridge_register,
        "latest_session",
        lambda _cwd: (_ for _ in ()).throw(AssertionError()),
    )

    data, _output = _run_register(
        monkeypatch,
        tmp_path,
        capsys,
        extra_args=["--session-id", "22222222-2222-2222-2222-222222222222"],
    )

    assert data["fork_from"] == "22222222-2222-2222-2222-222222222222"
    assert data["session_id"] is None


def test_register_cwd_only_touches_neither_session_id_nor_fork_from(monkeypatch, tmp_path):
    bridge_file = tmp_path / "claude" / "blender-bridge-session.json"
    bridge_file.parent.mkdir(parents=True)
    bridge_file.write_text(
        json.dumps(
            {
                "cwd": "D:/old-project",
                "session_id": "11111111-1111-1111-1111-111111111111",
                "fork_from": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "environment-session-12345678")
    monkeypatch.setattr(bridge_register, "BRIDGE_FILE", bridge_file)
    monkeypatch.setattr(bridge_register, "find_claude", lambda: "claude")
    monkeypatch.setattr(
        bridge_register,
        "latest_session",
        lambda _cwd: (_ for _ in ()).throw(AssertionError()),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["bridge_register.py", "--cwd", str(tmp_path), "--cwd-only"],
    )

    bridge_register.main()

    data = json.loads(bridge_file.read_text(encoding="utf-8"))
    assert data["session_id"] == "11111111-1111-1111-1111-111111111111"
    assert data["fork_from"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert data["cwd"] == str(tmp_path.resolve()).replace("\\", "/")


@pytest.mark.parametrize("invalid_root", [[], None, "session", 42])
def test_register_replaces_non_object_bridge_config(
    monkeypatch, tmp_path, capsys, invalid_root
):
    bridge_file = tmp_path / "claude" / "blender-bridge-session.json"
    bridge_file.parent.mkdir(parents=True)
    bridge_file.write_text(json.dumps(invalid_root) + "\n", encoding="utf-8")
    monkeypatch.setattr(bridge_register, "BRIDGE_FILE", bridge_file)
    monkeypatch.setattr(bridge_register, "find_claude", lambda: "claude")
    monkeypatch.setattr(
        sys,
        "argv",
        ["bridge_register.py", "--cwd", str(tmp_path), "--cwd-only"],
    )

    bridge_register.main()

    data = json.loads(bridge_file.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["cwd"] == str(tmp_path.resolve()).replace("\\", "/")
    # キー欠落と null は等価（panel は get で読む）。--cwd-only は接続情報に触らない。
    assert data.get("session_id") is None
    assert data.get("fork_from") is None
    assert "registered:" in capsys.readouterr().out


@pytest.mark.parametrize("configured_value", [None, ""])
def test_register_paths_fall_back_to_home_claude_when_config_is_unset_or_empty(
    tmp_path, configured_value
):
    env = os.environ.copy()
    if configured_value is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = configured_value
    env["TEST_CLAUDE_HOME"] = str(tmp_path / "home")
    code = (
        "import json, os\n"
        "from pathlib import Path\n"
        "Path.home = classmethod(lambda cls: Path(os.environ['TEST_CLAUDE_HOME']))\n"
        "from tools import bridge_register\n"
        "print(json.dumps([str(bridge_register.CLAUDE_CONFIG_DIR), "
        "str(bridge_register.BRIDGE_FILE)]))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    config_root, bridge_file = map(Path, json.loads(completed.stdout))

    assert config_root == tmp_path / "home" / ".claude"
    assert bridge_file == config_root / "blender-bridge-session.json"


def test_register_paths_and_latest_session_use_nonempty_claude_config_dir(tmp_path):
    env = os.environ.copy()
    config_root = tmp_path / "custom-claude"
    cwd = tmp_path / "project"
    cwd.mkdir()
    env["CLAUDE_CONFIG_DIR"] = str(config_root)
    code = (
        "import json, sys\n"
        "from tools import bridge_register\n"
        "cwd = sys.argv[1]\n"
        "project = bridge_register.CLAUDE_CONFIG_DIR / 'projects' / "
        "bridge_register.project_slug(cwd)\n"
        "project.mkdir(parents=True)\n"
        "(project / 'custom-session.jsonl').write_text('{}\\n', encoding='utf-8')\n"
        "print(json.dumps([str(bridge_register.BRIDGE_FILE), "
        "bridge_register.latest_session(cwd)]))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code, str(cwd)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    bridge_file, session = json.loads(completed.stdout)

    assert Path(bridge_file) == config_root / "blender-bridge-session.json"
    assert session == "custom-session"
