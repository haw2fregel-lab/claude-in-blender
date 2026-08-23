#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""橋の登録ファイル (~/.claude/blender-bridge-session.json) を書く。

使い方:
  python tools/bridge_register.py --cwd .                     # セッション自動特定
  python tools/bridge_register.py --cwd . --session-id <uuid> # 明示指定
  python tools/bridge_register.py --cwd . --cwd-only          # cwd だけ登録

--session-id 省略時は、まず Claude Code セッション内の
CLAUDE_CODE_SESSION_ID を使う。環境変数が無い場合だけ、cwd のプロジェクトの
transcript (~/.claude/projects/<slug>/*.jsonl) の最新ファイルを fallback として使う。
取得した ID はパネルが初回送信時に写しを作る fork 元として登録する。fallback は
並行セッションを取り違えうるため、出力の fork 元の末尾8文字を Blender パネルの
「接続先」表示と目で照合すること。CLAUDE_CONFIG_DIR が非空なら、その配下を使う。
"""
import argparse
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

CLAUDE_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
BRIDGE_FILE = CLAUDE_CONFIG_DIR / "blender-bridge-session.json"


@contextmanager
def lock_bridge_file(bridge_file):
    """Blender panel と登録処理の read-modify-write を直列化する。"""
    lock_file = bridge_file.with_suffix(bridge_file.suffix + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def project_slug(cwd):
    """Claude Code の projects ディレクトリ名（英数字以外を - に置換）。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))


def latest_session(cwd):
    proj = CLAUDE_CONFIG_DIR / "projects" / project_slug(cwd)
    files = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def push_recent(data, cwd, limit=5):
    """作業ディレクトリを履歴の先頭へ積み、重複と上限超過を除く。"""
    if "recent_cwds" not in data:
        previous = data.get("cwd")
        recent = [previous] if isinstance(previous, str) and previous else []
    elif not isinstance(data.get("recent_cwds"), list):
        recent = []
    else:
        recent = data["recent_cwds"]
    ordered = []
    for path in [cwd] + recent:
        if path not in ordered:
            ordered.append(path)
    data["recent_cwds"] = ordered[:limit]


def find_claude():
    for name in ("claude", "claude.exe", "claude.cmd"):
        p = shutil.which(name)
        if p:
            return p
    # native installer の既定置き場。.exe を先に見る——Windows に Git Bash 用の
    # 拡張子なしシムが同居していても、subprocess で実行できる方を返すため。
    for fallback in (
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude",
    ):
        if fallback.exists():
            return str(fallback)
    return None


def main():
    ap = argparse.ArgumentParser(description="Blender ブリッジのセッション登録")
    ap.add_argument("--cwd", required=True, help="Claude を起動する作業ディレクトリ")
    ap.add_argument("--repo", default=None, help="アドオンのソースリポ（省略時は既存値を保持）")
    ap.add_argument("--session-id", default=None, help="fork 元セッション ID（省略で自動特定）")
    ap.add_argument("--cwd-only", action="store_true", help="cwd だけ登録（既存のセッション設定は変えない）")
    args = ap.parse_args()

    cwd = str(Path(args.cwd).resolve()).replace("\\", "/")
    repo = (str(Path(args.repo).resolve()).replace("\\", "/")
            if args.repo is not None else None)
    fork_from = None
    session_source = None
    if not args.cwd_only:
        if args.session_id:
            fork_from = args.session_id
            session_source = "explicit"
        else:
            fork_from = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
            session_source = "env" if fork_from else "fallback"
            if not fork_from:
                fork_from = latest_session(cwd)

    with lock_bridge_file(BRIDGE_FILE):
        data = {}
        if BRIDGE_FILE.exists():
            try:
                loaded = json.loads(BRIDGE_FILE.read_text(encoding="utf-8"))
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                data = {}

        push_recent(data, cwd)
        data.update({
            "cwd": cwd,
            "claude_exe": data.get("claude_exe") or find_claude(),
            "registered_at": time.strftime("%Y-%m-%d %H:%M"),
            "registered_by": "bridge_register.py",
        })
        if repo is not None:
            data["repo"] = repo
        if not args.cwd_only:
            data["fork_from"] = fork_from
            data["session_id"] = None
        temp_file = BRIDGE_FILE.with_suffix(".json.tmp")
        temp_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_file, BRIDGE_FILE)

    print(f"registered: {BRIDGE_FILE}")
    print(f"  cwd       : {cwd}")
    if args.cwd_only:
        print("  session   : (変更なし)")
    elif fork_from:
        if session_source == "env":
            print(f"  fork 元   : ...{fork_from[-8:]}  (セッション自動特定 (env); この ID の写しをパネル側に作る (fork))")
        elif session_source == "fallback":
            print(f"  fork 元   : ...{fork_from[-8:]}  (パネルの「接続先」表示と fork 元の末尾8文字を照合してね; この ID の写しをパネル側に作る (fork))")
        else:
            print(f"  fork 元   : ...{fork_from[-8:]}  (明示指定; この ID の写しをパネル側に作る (fork))")
    else:
        print("  fork 元   : (未指定 — パネル側で新規セッションを作って送信)")


if __name__ == "__main__":
    main()
