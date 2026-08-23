import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

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


# --- fork 契約 ---
# 外部登録された ID は掴み直す相手ではなく fork 元。パネルの送信はその写しから始め、
# 返ってきた新 ID をパネルの session_id として引き継ぐ。
# 検証は外形のみ: claude へ渡る cmd 列と、bridge ファイルの遷移。

_FORK_SOURCE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_OTHER_FORK = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_STORED_SESSION = "11111111-1111-1111-1111-111111111111"
_NEW_SESSION = "22222222-2222-2222-2222-222222222222"
_MISSING = object()


def _project_cwd(tmp_path):
    # repo キーを書かない旧形式の登録を通すので、cwd 自身がアドオンのソースを兼ねる。
    # 送信前の存在チェックが見るのは mcp_server/server.py。
    cwd = tmp_path / "project"
    (cwd / "mcp_server").mkdir(parents=True, exist_ok=True)
    (cwd / "mcp_server" / "server.py").write_text("", encoding="utf-8")
    return str(cwd).replace("\\", "/")


def _write_bridge(bridge_file, cwd, **fields):
    bridge_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cwd": cwd}
    payload.update(fields)
    text = json.dumps(payload) + "\n"
    bridge_file.write_text(text, encoding="utf-8")
    return text


def _result_line(session_id=_MISSING, is_error=False):
    event = {"type": "result", "result": "done", "is_error": is_error}
    if session_id is not _MISSING:
        event["session_id"] = session_id
    return json.dumps(event)


def _stub_claude_run(monkeypatch, stdout, returncode=0, on_call=None):
    """claude 実行を差し替え、渡された cmd 列を順に記録する。"""
    calls = []

    def fake_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        calls.append(list(cmd))
        if on_call is not None:
            on_call()
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    monkeypatch.setattr(panel, "_find_claude", lambda _bridge: "claude")
    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    return calls


def _resume_target(cmd):
    return cmd[cmd.index("-r") + 1] if "-r" in cmd else None


@pytest.mark.parametrize(
    ("stored", "expected_resume", "expects_fork_flag", "forbidden"),
    [
        pytest.param(
            {"session_id": None, "fork_from": _FORK_SOURCE},
            _FORK_SOURCE,
            True,
            None,
            id="fork_from-only",
        ),
        pytest.param(
            {"session_id": _STORED_SESSION, "fork_from": None},
            _STORED_SESSION,
            False,
            None,
            id="session_id-only",
        ),
        pytest.param(
            {"session_id": _STORED_SESSION, "fork_from": _FORK_SOURCE},
            _FORK_SOURCE,
            True,
            _STORED_SESSION,
            id="fork_from-wins-over-session_id",
        ),
        pytest.param(
            {"session_id": None, "fork_from": None},
            None,
            False,
            None,
            id="both-null-starts-fresh",
        ),
        pytest.param({}, None, False, None, id="both-keys-absent-starts-fresh"),
    ],
)
def test_fork_from_decides_resume_target_and_fork_session_flag(
    bridge_file,
    tmp_path,
    monkeypatch,
    stored,
    expected_resume,
    expects_fork_flag,
    forbidden,
):
    cwd = _project_cwd(tmp_path)
    _write_bridge(bridge_file, cwd, **stored)
    calls = _stub_claude_run(monkeypatch, _result_line(_NEW_SESSION))

    panel._run_claude("塔を生やして")

    cmd = calls[-1]
    assert _resume_target(cmd) == expected_resume
    assert ("--fork-session" in cmd) is expects_fork_flag
    if forbidden is not None:
        assert forbidden not in cmd


def test_successful_fork_stores_the_new_session_and_drops_fork_from(
    bridge_file, tmp_path, monkeypatch
):
    cwd = _project_cwd(tmp_path)
    _write_bridge(bridge_file, cwd, session_id=None, fork_from=_FORK_SOURCE)
    calls = _stub_claude_run(monkeypatch, _result_line(_NEW_SESSION))

    panel._run_claude("塔を生やして")

    data = json.loads(bridge_file.read_text(encoding="utf-8"))
    assert data["session_id"] == _NEW_SESSION
    assert data.get("fork_from") is None
    assert data["cwd"] == cwd

    # fork は一度きり: 次の送信はただの resume に戻る。
    panel._run_claude("屋根も足して")

    assert _resume_target(calls[-1]) == _NEW_SESSION
    assert "--fork-session" not in calls[-1]


@pytest.mark.parametrize(
    ("stored", "expects_model"),
    [
        pytest.param({"model": "sonnet"}, True, id="fresh-session-gets-model"),
        pytest.param(
            {"model": "sonnet", "fork_from": _FORK_SOURCE},
            True,
            id="fork-gets-model",
        ),
        pytest.param(
            {"model": "sonnet", "session_id": _STORED_SESSION},
            False,
            id="resume-never-switches-model",
        ),
        pytest.param({}, False, id="no-model-no-flag"),
    ],
)
def test_model_flag_only_on_session_creating_sends(
    bridge_file, tmp_path, monkeypatch, stored, expects_model
):
    cwd = _project_cwd(tmp_path)
    _write_bridge(bridge_file, cwd, **stored)
    calls = _stub_claude_run(monkeypatch, _result_line(_NEW_SESSION))

    panel._run_claude("塔を生やして")

    cmd = calls[-1]
    if expects_model:
        assert cmd[cmd.index("--model") + 1] == "sonnet"
    else:
        assert "--model" not in cmd


def test_save_model_sets_and_default_clears_the_key(bridge_file, tmp_path):
    cwd = _project_cwd(tmp_path)
    _write_bridge(bridge_file, cwd, session_id=_STORED_SESSION)

    assert panel._save_model("sonnet") is True
    data = json.loads(bridge_file.read_text(encoding="utf-8"))
    assert data["model"] == "sonnet"
    assert data["session_id"] == _STORED_SESSION

    assert panel._save_model("default") is True
    data = json.loads(bridge_file.read_text(encoding="utf-8"))
    assert "model" not in data


def test_save_model_rejects_broken_bridge_root(bridge_file):
    bridge_file.parent.mkdir(parents=True)
    bridge_file.write_text("[]\n", encoding="utf-8")

    assert panel._save_model("sonnet") is False
    assert bridge_file.read_text(encoding="utf-8") == "[]\n"


def test_save_fork_from_arms_a_fork_and_clears_the_session(bridge_file, tmp_path):
    cwd = _project_cwd(tmp_path)
    _write_bridge(bridge_file, cwd, session_id=_STORED_SESSION, model="haiku")

    assert panel._save_fork_from(_FORK_SOURCE) is True
    data = json.loads(bridge_file.read_text(encoding="utf-8"))
    assert data["fork_from"] == _FORK_SOURCE
    assert data["session_id"] is None
    assert data["cwd"] == cwd
    assert data["model"] == "haiku"


def test_save_fork_from_rejects_broken_bridge_root(bridge_file):
    bridge_file.parent.mkdir(parents=True)
    bridge_file.write_text("[]\n", encoding="utf-8")

    assert panel._save_fork_from(_FORK_SOURCE) is False
    assert bridge_file.read_text(encoding="utf-8") == "[]\n"


@pytest.mark.parametrize(
    ("bridge", "expected"),
    [
        (None, "Claude Code default"),
        ({}, "Claude Code default"),
        ({"model": "sonnet"}, "Sonnet"),
        ({"model": "claude-fable-5"}, "claude-fable-5"),
    ],
)
def test_model_label_maps_aliases_and_passes_unknown_through(bridge, expected):
    assert panel._model_label(bridge) == expected


@pytest.mark.parametrize(
    "replacement", [_OTHER_FORK, None], ids=["replaced", "cleared"]
)
def test_save_is_skipped_when_fork_from_moves_during_the_run(
    bridge_file, tmp_path, monkeypatch, replacement
):
    cwd = _project_cwd(tmp_path)
    _write_bridge(bridge_file, cwd, session_id=None, fork_from=_FORK_SOURCE)
    calls = _stub_claude_run(
        monkeypatch,
        _result_line(_NEW_SESSION),
        on_call=lambda: _write_bridge(
            bridge_file, cwd, session_id=None, fork_from=replacement
        ),
    )

    panel._run_claude("塔を生やして")

    assert _resume_target(calls[-1]) == _FORK_SOURCE
    data = json.loads(bridge_file.read_text(encoding="utf-8"))
    assert data["session_id"] is None
    assert data["fork_from"] == replacement


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        pytest.param(_result_line(_NEW_SESSION, is_error=True), 0, id="is_error"),
        pytest.param(_result_line(_NEW_SESSION), 1, id="returncode-nonzero"),
    ],
)
def test_failed_fork_run_stores_nothing_and_keeps_fork_from(
    bridge_file, tmp_path, monkeypatch, stdout, returncode
):
    cwd = _project_cwd(tmp_path)
    original = _write_bridge(
        bridge_file, cwd, session_id=None, fork_from=_FORK_SOURCE
    )
    _stub_claude_run(monkeypatch, stdout, returncode=returncode)

    panel._run_claude("塔を生やして")

    assert bridge_file.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "returned",
    [_MISSING, None, "", "not-a-uuid", "environment-session-12345678"],
    ids=["absent", "null", "empty", "not-uuid", "free-form"],
)
def test_fork_result_without_a_valid_uuid_keeps_fork_from(
    bridge_file, tmp_path, monkeypatch, returned
):
    cwd = _project_cwd(tmp_path)
    original = _write_bridge(
        bridge_file, cwd, session_id=None, fork_from=_FORK_SOURCE
    )
    _stub_claude_run(monkeypatch, _result_line(returned))

    panel._run_claude("塔を生やして")

    assert bridge_file.read_text(encoding="utf-8") == original
