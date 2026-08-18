#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the Extension zip and verify its published metadata stays current."""
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "claude_bridge"
README = ROOT / "README.md"
README_JA = ROOT / "README.ja.md"

LICENSE = ROOT / "LICENSE"


def _manifest_version() -> str:
    manifest = (SRC / "blender_manifest.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.MULTILINE)
    if not match:
        raise RuntimeError("Could not read the Extension version from blender_manifest.toml")
    return match.group(1)


def _build_zip(version: str) -> Path:
    output = ROOT / "dist" / f"claude_bridge-{version}.zip"
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(SRC.rglob("*")):
            if file_path.is_file() and "__pycache__" not in file_path.parts:
                archive.write(file_path, file_path.relative_to(SRC))
        archive.write(LICENSE, "LICENSE")
    print(output)
    return output


def _readme_mismatches(version: str) -> list[str]:
    """英語正面と日本語 .ja、二枚の README の版表記を manifest と照合する。"""
    errors = []
    for path, pattern, label in (
        (README, r"prototype v([0-9]+(?:\.[0-9]+)+)", "README.md ('prototype vX.Y.Z')"),
        (README_JA, r"試作 v([0-9]+(?:\.[0-9]+)+)", "README.ja.md ('試作 vX.Y.Z')"),
    ):
        text = path.read_text(encoding="utf-8")
        match = re.search(pattern, text)
        if not match:
            errors.append(f"version marker was not found in {label}")
        elif match.group(1) != version:
            errors.append(
                f"{label} says v{match.group(1)}, but blender_manifest.toml is v{version}"
            )
    return errors


WINDOWS_BLENDERS = Path(r"C:\Program Files\Blender Foundation")
MAC_APPLICATIONS = Path("/Applications")


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", name))


def _find_blender() -> str | None:
    if sys.platform == "darwin":
        # 公式 dmg は Blender.app。複数共存はリネーム慣習（Blender 4.5.app など）で、
        # 数字なしの Blender.app はバージョン共存時に最古扱いになる。
        installed = list(MAC_APPLICATIONS.glob("Blender*.app/Contents/MacOS/Blender"))
        if installed:
            return str(max(installed, key=lambda p: _version_key(p.parents[2].name)))
    else:
        installed = list(WINDOWS_BLENDERS.glob("Blender */blender.exe"))
        if installed:
            return str(max(installed, key=lambda p: _version_key(p.parent.name)))
    return shutil.which("blender") or shutil.which("blender.exe")


def _validate_extension(archive: Path) -> int:
    blender = _find_blender()
    if not blender:
        print("validate skipped (blender not found)")
        return 0

    print(f"validating with {blender}")
    completed = subprocess.run(
        [blender, "--command", "extension", "validate", str(archive)],
        text=True,
        capture_output=True,
    )
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    if completed.stderr:
        print(completed.stderr, end="" if completed.stderr.endswith("\n") else "\n", file=sys.stderr)
    return completed.returncode


def main() -> int:
    version = _manifest_version()
    mismatches = _readme_mismatches(version)
    if mismatches:
        for mismatch in mismatches:
            print(f"README consistency check failed: {mismatch}", file=sys.stderr)
        return 1
    archive = _build_zip(version)
    return _validate_extension(archive)


if __name__ == "__main__":
    raise SystemExit(main())
