#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""橋の登録ファイル (~/.claude/blender-bridge-session.json) を書く。

使い方:
  python tools/bridge_register.py --cwd .                     # セッション自動特定
  python tools/bridge_register.py --cwd . --session-id <uuid> # 明示指定
  python tools/bridge_register.py --cwd . --cwd-only          # cwd だけ登録

--session-id 省略時は、cwd のプロジェクトの transcript
(~/.claude/projects/<slug>/*.jsonl) の最新ファイルを「今のセッション」とみなす。
並行セッションがあると取り違えうるので、出力の末尾8文字を
Blender パネルの「接続先」表示と目で照合すること。
"""
import argparse
import json
import re
import shutil
import time
from pathlib import Path

BRIDGE_FILE = Path.home() / ".claude" / "blender-bridge-session.json"


def project_slug(cwd):
    """Claude Code の projects ディレクトリ名（英数字以外を - に置換）。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))


def latest_session(cwd):
    proj = Path.home() / ".claude" / "projects" / project_slug(cwd)
    files = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def find_claude():
    for name in ("claude", "claude.exe", "claude.cmd"):
        p = shutil.which(name)
        if p:
            return p
    fallback = Path.home() / ".local" / "bin" / "claude.exe"
    return str(fallback) if fallback.exists() else None


def main():
    ap = argparse.ArgumentParser(description="Blender ブリッジのセッション登録")
    ap.add_argument("--cwd", required=True, help="橋の作業ディレクトリ（.mcp.json のあるリポ）")
    ap.add_argument("--session-id", default=None, help="接続先セッション ID（省略で自動特定）")
    ap.add_argument("--cwd-only", action="store_true", help="cwd だけ登録（セッションは未接続のまま）")
    args = ap.parse_args()

    cwd = str(Path(args.cwd).resolve()).replace("\\", "/")
    session_id = None
    if not args.cwd_only:
        session_id = args.session_id or latest_session(cwd)

    data = {}
    if BRIDGE_FILE.exists():
        try:
            data = json.loads(BRIDGE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

    data.update({
        "session_id": session_id,
        "cwd": cwd,
        "claude_exe": data.get("claude_exe") or find_claude(),
        "registered_at": time.strftime("%Y-%m-%d %H:%M"),
        "registered_by": "bridge_register.py",
    })
    BRIDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BRIDGE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"registered: {BRIDGE_FILE}")
    print(f"  cwd       : {cwd}")
    if session_id:
        print(f"  session   : ...{session_id[-8:]}  (パネルの「接続先」表示と照合してね)")
    else:
        print("  session   : (未接続 — パネル側で選択するか、新規セッションで送信)")


if __name__ == "__main__":
    main()
