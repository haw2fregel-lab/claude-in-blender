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


# 引数付きで CLI を叩く版。引数なしの _build() は既存テストの経路としてそのまま残す。
def _build_with(working_copy, *args):
    return subprocess.run(
        [sys.executable, str(working_copy / "tools" / "build_extension.py"), *args],
        cwd=working_copy,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=BUILD_TIMEOUT_S,
    )


# 呼ばれた事実だけを残して成功する偽 Blender。validate にどの path が渡るかを外から観測する。
def _plant_recording_blender(directory):
    directory.mkdir(parents=True, exist_ok=True)
    invocations = directory / "invocations.txt"
    if sys.platform == "win32":
        exe = directory / "blender.bat"
        exe.write_text(f'@echo off\n>>"{invocations}" echo %*\n', encoding="utf-8")
    else:
        exe = directory / "blender"
        exe.write_text(f'#!/bin/sh\necho "$@" >> "{invocations}"\n', encoding="utf-8")
        exe.chmod(0o755)
    return exe, invocations


# --blender 指定時は標準ディレクトリの探索を経由しないので、実機の Blender の入り方に依らない。
def test_explicit_blender_path_is_handed_to_validation(working_copy, tmp_path):
    fake, invocations = _plant_recording_blender(tmp_path / "picked")

    completed = _build_with(working_copy, "--blender", str(fake))

    assert len(_built_zips(working_copy)) == 1, completed.stdout + completed.stderr
    assert invocations.exists(), completed.stdout + completed.stderr
    assert str(fake) in completed.stdout + completed.stderr


def test_missing_blender_path_fails_without_building_a_zip(working_copy, tmp_path):
    missing = tmp_path / "nowhere" / "blender.exe"

    completed = _build_with(working_copy, "--blender", str(missing))

    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert str(missing) in completed.stderr
    assert _built_zips(working_copy) == [], completed.stdout + completed.stderr


def test_directory_blender_path_fails_without_building_a_zip(working_copy, tmp_path):
    directory = tmp_path / "Blender.app"
    directory.mkdir()

    completed = _build_with(working_copy, "--blender", str(directory))

    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert _built_zips(working_copy) == [], completed.stdout + completed.stderr


# 探索の seam（WINDOWS_BLENDERS / MAC_APPLICATIONS / which）を差し替えるため CLI を in-process で叩く。
# 本物のリポを触らないよう path 定数を複製へ向ける——dist 出力も manifest 読みもこの5つから導出される。
# stderr は CLI の契約どおり fd 単位（capfd）で観測する。
def _build_in_process(working_copy, monkeypatch, *args):
    monkeypatch.setattr(build_extension, "ROOT", working_copy)
    monkeypatch.setattr(build_extension, "SRC", working_copy / "claude_bridge")
    monkeypatch.setattr(build_extension, "README", working_copy / "README.md")
    monkeypatch.setattr(build_extension, "README_JA", working_copy / "README.ja.md")
    monkeypatch.setattr(build_extension, "LICENSE", working_copy / "LICENSE")
    return build_extension.main(list(args))


# 実機に Blender がどう入っていても候補ゼロにする。sys.platform は触らずどちらの探索先も空にする。
def _clear_blender_search_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(build_extension, "WINDOWS_BLENDERS", tmp_path / "no-installs")
    monkeypatch.setattr(build_extension, "MAC_APPLICATIONS", tmp_path / "no-installs")


def test_two_installs_stop_before_validating_either_of_them(
    monkeypatch, capfd, working_copy, tmp_path
):
    monkeypatch.setattr(build_extension.sys, "platform", "win32")
    monkeypatch.setattr(build_extension, "WINDOWS_BLENDERS", tmp_path / "installs")
    monkeypatch.delenv("CLAUDE_BRIDGE_REQUIRE_BLENDER", raising=False)
    older = _plant_blender(tmp_path / "installs", Path("Blender 4.2/blender.exe"))
    newer = _plant_blender(tmp_path / "installs", Path("Blender 4.5/blender.exe"))

    exit_code = _build_in_process(working_copy, monkeypatch)
    captured = capfd.readouterr()

    assert exit_code == 1, captured.out + captured.err
    assert str(older) in captured.err
    assert str(newer) in captured.err
    assert "--blender" in captured.err
    # バージョン降順——整形には踏み込まず、新しい方が先に出ることだけ見る。
    assert captured.err.index(str(newer)) < captured.err.index(str(older))
    # 止まるのは validate 段。zip build は行う契約。
    assert len(_built_zips(working_copy)) == 1, captured.out + captured.err


def test_path_lookup_result_is_handed_to_validation(
    monkeypatch, capfd, working_copy, tmp_path
):
    _clear_blender_search_dirs(monkeypatch, tmp_path)
    fake, invocations = _plant_recording_blender(tmp_path / "on-path")
    monkeypatch.setattr(
        build_extension.shutil,
        "which",
        lambda name: str(fake) if name == "blender" else None,
    )

    # 終了コードは validate の成否判定に依るので見ない。契約は「その path が使われる」まで。
    _build_in_process(working_copy, monkeypatch)
    captured = capfd.readouterr()

    assert invocations.exists(), captured.out + captured.err


def test_no_blender_anywhere_fails_when_validation_is_required(
    monkeypatch, capfd, working_copy, tmp_path
):
    _clear_blender_search_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(build_extension.shutil, "which", lambda name: None)
    monkeypatch.setenv("CLAUDE_BRIDGE_REQUIRE_BLENDER", "1")

    exit_code = _build_in_process(working_copy, monkeypatch)
    captured = capfd.readouterr()

    assert exit_code != 0, captured.out + captured.err


def test_no_blender_anywhere_skips_validation_and_still_succeeds(
    monkeypatch, capfd, working_copy, tmp_path
):
    _clear_blender_search_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(build_extension.shutil, "which", lambda name: None)
    monkeypatch.delenv("CLAUDE_BRIDGE_REQUIRE_BLENDER", raising=False)

    exit_code = _build_in_process(working_copy, monkeypatch)
    captured = capfd.readouterr()

    assert exit_code == 0, captured.out + captured.err
    assert len(_built_zips(working_copy)) == 1, captured.out + captured.err
