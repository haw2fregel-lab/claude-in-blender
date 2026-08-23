#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the Extension zip and verify its published metadata stays current."""
import argparse
import os
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


# 版マーカーは宣言文から独立させる。"prototype" / "試作" は 1.0 公開で外れる語なので、
# それを手がかりにすると公開作業そのもので版照合が割れる。
VERSION_MARKER = re.compile(r"^> \*\*v([0-9]+(?:\.[0-9]+)+)\*\*", re.MULTILINE)


def _readme_mismatches(version: str) -> list[str]:
    """英語正面と日本語 .ja、二枚の README の版マーカーを manifest と照合する。"""
    errors = []
    for path in (README, README_JA):
        label = path.name
        match = VERSION_MARKER.search(path.read_text(encoding="utf-8"))
        if not match:
            errors.append(
                f"version marker was not found in {label} (expected a line starting with '> **vX.Y.Z**')"
            )
        elif match.group(1) != version:
            errors.append(
                f"{label} says v{match.group(1)}, but blender_manifest.toml is v{version}"
            )
    return errors


WINDOWS_BLENDERS = Path(r"C:\Program Files\Blender Foundation")
MAC_APPLICATIONS = Path("/Applications")


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", name))


def _installed_blenders() -> list[Path]:
    """標準の導入先に入っている Blender を、バージョン降順（新しい順）で返す。

    順序は呼び出し側の契約。先頭が「自動で選ぶなら これ」で、複数あった時は
    そのまま利用者へ提示する一覧になる。PATH 上の blender はここには含めない。
    """
    if sys.platform == "darwin":
        # 公式 dmg は Blender.app。複数共存はリネーム慣習（Blender 4.5.app など）で、
        # 数字なしの Blender.app はバージョン共存時に最古扱いになる。
        found = MAC_APPLICATIONS.glob("Blender*.app/Contents/MacOS/Blender")
        return sorted(found, key=lambda p: _version_key(p.parents[2].name), reverse=True)
    found = WINDOWS_BLENDERS.glob("Blender */blender.exe")
    return sorted(found, key=lambda p: _version_key(p.parent.name), reverse=True)


def _find_blender() -> str | None:
    installed = _installed_blenders()
    if installed:
        return str(installed[0])
    return shutil.which("blender") or shutil.which("blender.exe")


def _validate_extension(archive: Path, blender: str | None = None) -> int:
    """zip を Blender の extension validate にかける。

    blender を渡すとその実行ファイルを使う（--blender で明示された場合）。省略時は
    標準の導入先を探す。複数見つかった時は選ばずに 1 を返す。
    """
    if blender is None:
        installed = _installed_blenders()
        if len(installed) > 1:
            # 最新版を黙って選ぶと、利用者が使っていない版で validate を通して
            # 「セットアップ成功」と報告してしまう。静かに間違うより、ここで止める。
            print(
                f"validate stopped: the zip was built, but {len(installed)} Blender "
                "installs were found (newest first):",
                file=sys.stderr,
            )
            for candidate in installed:
                print(f"  {candidate}", file=sys.stderr)
            print(
                "pick the one you use: python tools/build_extension.py --blender <path>",
                file=sys.stderr,
            )
            return 1
        blender = _find_blender()
    if not blender:
        # CI では Blender を用意した上で検査する。「本当に無い」と「あるのに探索が
        # 壊れている」を skip で同じ扱いにすると、検出が壊れても緑のままになる。
        if os.environ.get("CLAUDE_BRIDGE_REQUIRE_BLENDER") == "1":
            print(
                "validate FAILED: Blender was required (CLAUDE_BRIDGE_REQUIRE_BLENDER=1) "
                "but none was found",
                file=sys.stderr,
            )
            return 1
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


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--blender",
        metavar="PATH",
        help=(
            "Blender executable to validate the built zip with. "
            "When given, the standard install directories are not searched."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.blender is not None and not Path(args.blender).is_file():
        # zip を作る前に弾く。指定を打ち間違えたまま dist/ に成果物だけ残ると、
        # validate を通っていない zip を「ビルド済み」と取り違える。
        print(f"--blender was given {args.blender}, but that is not a file", file=sys.stderr)
        return 1
    version = _manifest_version()
    mismatches = _readme_mismatches(version)
    if mismatches:
        for mismatch in mismatches:
            print(f"README consistency check failed: {mismatch}", file=sys.stderr)
        return 1
    archive = _build_zip(version)
    return _validate_extension(archive, args.blender)


if __name__ == "__main__":
    raise SystemExit(main())
