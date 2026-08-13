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
SERVER = ROOT / "mcp_server" / "server.py"


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
    print(output)
    return output


def _readme_mismatches(version: str) -> list[str]:
    readme = README.read_text(encoding="utf-8")
    tool_count = len(re.findall(r"^\s*@mcp\.tool\(\)", SERVER.read_text(encoding="utf-8"), re.MULTILINE))
    tool_match = re.search(r"ツール\s*(\d+)\s*個", readme)
    version_match = re.search(r"試作 v([0-9]+(?:\.[0-9]+)+)", readme)

    errors = []
    if not tool_match:
        errors.append("README tool count ('ツール N 個') was not found")
    elif int(tool_match.group(1)) != tool_count:
        errors.append(
            f"README says {tool_match.group(1)} tools, but mcp_server/server.py defines {tool_count}"
        )
    if not version_match:
        errors.append("README trial version ('試作 vX.Y.Z') was not found")
    elif version_match.group(1) != version:
        errors.append(
            f"README says trial v{version_match.group(1)}, but blender_manifest.toml is v{version}"
        )
    return errors


def _version_key(blender: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", blender.parent.name))


def _find_blender() -> str | None:
    installed = list(Path(r"C:\Program Files\Blender Foundation").glob("Blender */blender.exe"))
    if installed:
        return str(max(installed, key=_version_key))
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
    archive = _build_zip(version)
    mismatches = _readme_mismatches(version)
    if mismatches:
        for mismatch in mismatches:
            print(f"README consistency check failed: {mismatch}", file=sys.stderr)
        return 1
    return _validate_extension(archive)


if __name__ == "__main__":
    raise SystemExit(main())
