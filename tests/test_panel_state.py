import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from claude_bridge import panel


@pytest.fixture
def bridge_file(tmp_path, monkeypatch):
    path = tmp_path / "claude" / "blender-bridge-session.json"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(panel, "BRIDGE_FILE", path)
    return path


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ('{"cwd": "D:/project", "session_id": null}\n', {
            "cwd": "D:/project", "session_id": None,
        }),
        ("[]\n", None),
        ('"session"\n', None),
        ("42\n", None),
        ("null\n", None),
        ("{ malformed\n", None),
    ],
)
def test_load_bridge_accepts_only_a_json_object(bridge_file, contents, expected):
    bridge_file.parent.mkdir(parents=True)
    bridge_file.write_text(contents, encoding="utf-8")

    assert panel._load_bridge() == expected


def test_load_bridge_returns_none_when_file_is_missing(bridge_file):
    assert not bridge_file.exists()
    assert panel._load_bridge() is None


@pytest.mark.parametrize(
    ("stored_session", "expected_session"),
    [
        (None, None),
        (
            "11111111-1111-1111-1111-111111111111",
            "11111111-1111-1111-1111-111111111111",
        ),
    ],
)
def test_save_session_id_updates_only_when_expected_state_matches(
    bridge_file, stored_session, expected_session
):
    bridge_file.parent.mkdir(parents=True)
    bridge_file.write_text(
        json.dumps({"cwd": "D:/project", "session_id": stored_session}) + "\n",
        encoding="utf-8",
    )

    assert panel._save_session_id(
        "22222222-2222-2222-2222-222222222222",
        expected_cwd="D:/project",
        expected_session_id=expected_session,
    ) is True
    assert json.loads(bridge_file.read_text(encoding="utf-8"))["session_id"] == (
        "22222222-2222-2222-2222-222222222222"
    )


@pytest.mark.parametrize(
    ("expected_cwd", "expected_session"),
    [
        ("D:/other", "11111111-1111-1111-1111-111111111111"),
        ("D:/project", None),
        ("D:/project", "33333333-3333-3333-3333-333333333333"),
    ],
)
def test_save_session_id_rejects_changed_state_without_touching_file(
    bridge_file, expected_cwd, expected_session
):
    bridge_file.parent.mkdir(parents=True)
    original = (
        '{"cwd":"D:/project","session_id":'
        '"11111111-1111-1111-1111-111111111111","custom":"preserve"}\n'
    )
    bridge_file.write_text(original, encoding="utf-8")

    assert panel._save_session_id(
        "22222222-2222-2222-2222-222222222222",
        expected_cwd=expected_cwd,
        expected_session_id=expected_session,
    ) is False
    assert bridge_file.read_text(encoding="utf-8") == original


def test_panel_and_register_tool_share_a_cross_process_bridge_lock(bridge_file):
    bridge_file.parent.mkdir(parents=True)
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from tools.bridge_register import lock_bridge_file\n"
        "print('ready', flush=True)\n"
        "with lock_bridge_file(Path(sys.argv[1])):\n"
        "    print('acquired', flush=True)\n"
    )

    with panel._lock_bridge_file(bridge_file):
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(bridge_file)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=os.environ.copy(),
        )
        assert process.stdout.readline().strip() == "ready"
        time.sleep(0.1)
        assert process.poll() is None

    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert stdout.strip() == "acquired"
