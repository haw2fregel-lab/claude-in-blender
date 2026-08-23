import os

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

    path = tmp_path / "scratch" / "script.py"
    assert server.write_scratch("script.py", "value = 1\n") == (
        f"Wrote {path.resolve()} (10 bytes)"
    )
    assert server.edit_scratch("script.py", "value = 1", "value = 2") == (
        f"Updated {path.resolve()} (replaced 1 unique match)"
    )
    assert path.read_text(encoding="utf-8") == "value = 2\n"

    with pytest.raises(RuntimeError, match="found 0"):
        server.edit_scratch("script.py", "missing", "anything")

    server.write_scratch("repeated.py", "same\nsame\n")
    with pytest.raises(RuntimeError, match="found 2"):
        server.edit_scratch("repeated.py", "same", "different")


def _make_symlink_or_skip(link, target):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")


@pytest.mark.parametrize("operation", ["write", "edit"])
def test_scratch_mutations_reject_existing_symlink_without_changing_target(
    tmp_path, monkeypatch, operation
):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = tmp_path / "outside.py"
    target.write_text("value = 1\n", encoding="utf-8")
    link = scratch / "linked.py"
    _make_symlink_or_skip(link, target)
    monkeypatch.setattr(server, "_SCRATCH_DIR", str(scratch))

    with pytest.raises(RuntimeError):
        if operation == "write":
            server.write_scratch("linked.py", "value = 2\n")
        else:
            server.edit_scratch("linked.py", "value = 1", "value = 2")

    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "value = 1\n"


# --- 契約: scratch の 1MiB 上限（票B2 契約2 / 完了条件 (a)(b)） ---
# 上限は 1MiB = 1,048,576 bytes。ちょうどは通り、1 byte 超えたら書く前に断る。
SCRATCH_MAX_BYTES = 1024 * 1024
# 「上限値をエラーメッセージに含める」の見せ方までは契約が決めていない。
# byte 数でも MiB 表記でも、上限が読み取れれば通す。
LIMIT_RENDERINGS = ("1048576", "1,048,576", "1 MiB", "1MiB", "1.0 MiB")


def _mentions_the_limit(message):
    return any(rendering in message for rendering in LIMIT_RENDERINGS)


def _plant_scratch_file(scratch, name, content):
    """ツールを通さず scratch へ直に置く。改行なしの content 前提（byte 数 = 文字数）。"""
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / name
    path.write_text(content, encoding="utf-8")
    return path


def test_write_scratch_accepts_a_file_of_exactly_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_SCRATCH_DIR", str(tmp_path / "scratch"))
    at_limit = "#" * SCRATCH_MAX_BYTES  # ASCII だけなので 1 文字 = 1 byte

    returned = server.write_scratch("at_limit.py", at_limit)

    path = tmp_path / "scratch" / "at_limit.py"
    assert returned == f"Wrote {path.resolve()} ({SCRATCH_MAX_BYTES} bytes)"
    assert path.stat().st_size == SCRATCH_MAX_BYTES


def test_write_scratch_rejects_one_byte_over_the_limit_before_writing(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(server, "_SCRATCH_DIR", str(scratch))
    server.write_scratch("kept.py", "value = 1\n")
    over_limit = "#" * (SCRATCH_MAX_BYTES + 1)

    with pytest.raises(RuntimeError) as rejected:
        server.write_scratch("over_limit.py", over_limit)

    assert _mentions_the_limit(str(rejected.value)), str(rejected.value)
    # 「実行前に拒否」——新しいファイルは生まれない。
    assert not (scratch / "over_limit.py").exists()

    with pytest.raises(RuntimeError):
        server.write_scratch("kept.py", over_limit)

    # 上書きも同じ。断ったのだから、元の中身が残る。
    assert (scratch / "kept.py").read_text(encoding="utf-8") == "value = 1\n"


def test_write_scratch_counts_the_limit_in_bytes_not_characters(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(server, "_SCRATCH_DIR", str(scratch))
    # 「あ」は UTF-8 で 3 bytes。文字数で数えると、どちらも上限のはるか下に見える。
    filler = (SCRATCH_MAX_BYTES - 1) // 3
    at_limit = "#" + "あ" * filler  # 1 + 3 * 349,525 = 1,048,576 bytes
    over_limit = "#" + "あ" * (filler + 1)
    assert len(at_limit.encode("utf-8")) == SCRATCH_MAX_BYTES
    assert len(over_limit.encode("utf-8")) > SCRATCH_MAX_BYTES
    assert len(over_limit) < SCRATCH_MAX_BYTES  # 文字数では上限に届かない

    server.write_scratch("multibyte.py", at_limit)
    assert (scratch / "multibyte.py").stat().st_size == SCRATCH_MAX_BYTES

    with pytest.raises(RuntimeError) as rejected:
        server.write_scratch("multibyte_over.py", over_limit)

    assert _mentions_the_limit(str(rejected.value)), str(rejected.value)
    assert not (scratch / "multibyte_over.py").exists()


def test_edit_scratch_accepts_a_result_of_exactly_the_limit(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(server, "_SCRATCH_DIR", str(scratch))
    path = _plant_scratch_file(scratch, "edited.py", "#" * (SCRATCH_MAX_BYTES - 1) + "X")

    returned = server.edit_scratch("edited.py", "X", "Y")

    assert returned == f"Updated {path.resolve()} (replaced 1 unique match)"
    assert path.stat().st_size == SCRATCH_MAX_BYTES
    assert path.read_text(encoding="utf-8").endswith("Y")


def test_edit_scratch_rejects_a_result_over_the_limit_and_keeps_the_file(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(server, "_SCRATCH_DIR", str(scratch))
    original = "#" * (SCRATCH_MAX_BYTES - 1) + "X"
    path = _plant_scratch_file(scratch, "edited.py", original)

    with pytest.raises(RuntimeError) as rejected:
        server.edit_scratch("edited.py", "X", "YZ")  # 1 byte 増えて上限超え

    assert _mentions_the_limit(str(rejected.value)), str(rejected.value)
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_size == SCRATCH_MAX_BYTES


def _without_paths(message, paths):
    """message から path の痕跡を伏せる。文言そのものが違うかだけを見るため。"""
    for path in paths:
        for text in (
            str(path.resolve()),
            path.resolve().as_posix(),
            str(path),
            path.as_posix(),
            path.name,
            path.stem,
        ):
            message = message.replace(text, "<path>")
    return message


def test_execute_file_gives_a_different_reason_for_each_rejected_path(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(server, "_SCRATCH_DIR", str(scratch))
    # 名前は中身のない語にする。stem を伏せる時に理由の語（outside 等）を削らないため。
    outside = tmp_path / "alpha.py"
    outside.write_text("result = 1", encoding="utf-8")
    missing = scratch / "bravo.py"
    not_python = scratch / "charlie.txt"
    not_python.write_text("result = 1", encoding="utf-8")
    too_large = scratch / "delta.py"
    too_large.write_bytes(b"#" * (SCRATCH_MAX_BYTES + 1))
    routes = {
        "scratch の外": outside,
        "ファイルが無い": missing,
        "拡張子が .py でない": not_python,
        "上限超え": too_large,
    }

    messages = {}
    for route, path in routes.items():
        with pytest.raises(RuntimeError) as rejected:
            server.execute_file(str(path))
        messages[route] = str(rejected.value)

    # 経路ごとに別の文言。path 名だけ違って見えるのは「識別できる」に数えない。
    reasons = {
        route: _without_paths(message, routes.values()) for route, message in messages.items()
    }
    assert len(set(reasons.values())) == len(routes), reasons
    assert _mentions_the_limit(messages["上限超え"]), messages["上限超え"]
