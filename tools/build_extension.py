#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude_bridge/ を Extension zip (dist/claude_bridge-<version>.zip) に固める。

バージョンは blender_manifest.toml から読む（一元管理）。
"""
import re
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
src = root / "claude_bridge"

manifest = (src / "blender_manifest.toml").read_text(encoding="utf-8")
version = re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.M).group(1)

out = root / "dist" / f"claude_bridge-{version}.zip"
out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(src.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            z.write(f, f.relative_to(src))

print(out)
