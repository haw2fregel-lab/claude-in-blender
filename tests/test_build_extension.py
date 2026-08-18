import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tools import build_extension


ROOT = Path(__file__).resolve().parents[1]
COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".pytest_cache", ".venv", "__pycache__", "*.pyc", "dist"
)
BUILD_TIMEOUT_S = 300


@pytest.fixture
def working_copy(tmp_path):
    # 本物の dist/ を汚さないため、作業樹の複製に対してビルドを走らせる。
    copy = tmp_path / "repo"
    shutil.copytree(ROOT, copy, ignore=COPY_IGNORE)
    return copy


def _build(working_copy):
    return subprocess.run(
        [sys.executable, str(working_copy / "tools" / "build_extension.py")],
        cwd=working_copy,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=BUILD_TIMEOUT_S,
    )


def _built_zips(working_copy):
    return sorted((working_copy / "dist").glob("*.zip"))


def _break_readme_version(working_copy):
    for name in ("README.md", "README.ja.md"):
        readme = working_copy / name
        text = readme.read_text(encoding="utf-8")
        broken, replaced = re.subn(r"v\d+\.\d+\.\d+", "v99.99.99", text, count=1)
        assert replaced == 1, f"{name} に版表記が見当たらない"
        readme.write_text(broken, encoding="utf-8")


def test_built_zip_ships_the_repository_license_at_its_root(working_copy):
    completed = _build(working_copy)

    built = _built_zips(working_copy)
    assert len(built) == 1, completed.stdout + completed.stderr
    with zipfile.ZipFile(built[0]) as archive:
        assert "LICENSE" in archive.namelist()
        assert archive.read("LICENSE") == (ROOT / "LICENSE").read_bytes()


def test_readme_version_mismatch_leaves_dist_without_a_zip(working_copy):
    _break_readme_version(working_copy)

    completed = _build(working_copy)

    assert _built_zips(working_copy) == [], completed.stdout + completed.stderr


def _plant_blender(root, rel):
    exe = root / rel
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    return exe


def test_find_blender_windows_picks_the_newest_install(monkeypatch, tmp_path):
    monkeypatch.setattr(build_extension.sys, "platform", "win32")
    monkeypatch.setattr(build_extension, "WINDOWS_BLENDERS", tmp_path)
    _plant_blender(tmp_path, Path("Blender 4.2/blender.exe"))
    newest = _plant_blender(tmp_path, Path("Blender 4.5/blender.exe"))

    assert build_extension._find_blender() == str(newest)


def test_find_blender_darwin_scans_applications(monkeypatch, tmp_path):
    monkeypatch.setattr(build_extension.sys, "platform", "darwin")
    monkeypatch.setattr(build_extension, "MAC_APPLICATIONS", tmp_path)
    plain = _plant_blender(tmp_path, Path("Blender.app/Contents/MacOS/Blender"))

    assert build_extension._find_blender() == str(plain)


# 数字なしの Blender.app はバージョン () 扱い——リネーム共存では数字入りが新しい。
def test_find_blender_darwin_prefers_versioned_app_over_plain(monkeypatch, tmp_path):
    monkeypatch.setattr(build_extension.sys, "platform", "darwin")
    monkeypatch.setattr(build_extension, "MAC_APPLICATIONS", tmp_path)
    _plant_blender(tmp_path, Path("Blender.app/Contents/MacOS/Blender"))
    versioned = _plant_blender(tmp_path, Path("Blender 4.5.app/Contents/MacOS/Blender"))

    assert build_extension._find_blender() == str(versioned)


def test_find_blender_falls_back_to_path_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(build_extension.sys, "platform", "darwin")
    monkeypatch.setattr(build_extension, "MAC_APPLICATIONS", tmp_path / "empty")
    monkeypatch.setattr(
        build_extension.shutil,
        "which",
        lambda name: "/opt/blender/blender" if name == "blender" else None,
    )

    assert build_extension._find_blender() == "/opt/blender/blender"
