import pytest

from mcp_server import server


@pytest.mark.parametrize("name", ["scene.py", "scene-01.py", "scene_file.py"])
def test_validate_scratch_name_accepts_safe_python_names(name):
    assert server._validate_scratch_name(name) == name


@pytest.mark.parametrize(
    "name", ["folder/scene.py", "folder\\scene.py", "../scene.py", "scene..py", "scene.txt"]
)
def test_validate_scratch_name_rejects_paths_and_non_python_files(name):
    with pytest.raises(RuntimeError):
        server._validate_scratch_name(name)


def test_resolve_scratch_file_enforces_location_type_and_size(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(server, "_SCRATCH_DIR", str(scratch))

    accepted = scratch / "accepted.py"
    accepted.write_text("result = 1\n", encoding="utf-8")
    assert server._resolve_scratch_file(str(accepted)) == accepted.resolve()

    for rejected in (tmp_path / "outside.py", scratch / "missing.py", scratch / "not_python.txt"):
        if rejected.name == "not_python.txt":
            rejected.write_text("not python", encoding="utf-8")
        with pytest.raises(RuntimeError):
            server._resolve_scratch_file(str(rejected))

    too_large = scratch / "too_large.py"
    too_large.write_bytes(b"#" * (1024 * 1024 + 1))
    with pytest.raises(RuntimeError):
        server._resolve_scratch_file(str(too_large))


def test_bridge_error_includes_traceback_only_when_present():
    assert str(server._bridge_error({"error": {"message": "bridge failed"}})) == "bridge failed"
    assert str(
        server._bridge_error({"error": {"message": "bridge failed", "traceback": "trace"}})
    ) == "bridge failed\ntrace"


def test_write_then_edit_scratch_requires_a_unique_match(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_SCRATCH_DIR", str(tmp_path / "scratch"))

    server.write_scratch("script.py", "value = 1\n")
    assert "Updated" in server.edit_scratch("script.py", "value = 1", "value = 2")
    assert (tmp_path / "scratch" / "script.py").read_text(encoding="utf-8") == "value = 2\n"

    with pytest.raises(RuntimeError, match="found 0"):
        server.edit_scratch("script.py", "missing", "anything")

    server.write_scratch("repeated.py", "same\nsame\n")
    with pytest.raises(RuntimeError, match="found 2"):
        server.edit_scratch("repeated.py", "same", "different")
