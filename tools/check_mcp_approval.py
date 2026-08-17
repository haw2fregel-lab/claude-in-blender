#!/usr/bin/env python3
"""Claude Code の MCP 承認状態を検知・修復する。"""
import argparse
import codecs
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

SERVER_NAME = "claude-in-blender"


def project_keys(projects, repo):
    forward = str(repo).replace("\\", "/")
    backward = forward.replace("/", "\\")
    candidates = {forward, backward}
    exact = [key for key, value in projects.items()
             if key in candidates and isinstance(value, dict)]
    if exact:
        return exact
    folded = {candidate.casefold() for candidate in candidates}
    return [key for key, value in projects.items()
            if key.casefold() in folded and isinstance(value, dict)]


def entry_state(entry):
    servers = entry.get("enabledMcpjsonServers")
    return {
        "hasTrustDialogAccepted": entry.get("hasTrustDialogAccepted") is True,
        "serverEnabled": isinstance(servers, list) and SERVER_NAME in servers,
    }


def entries_state(projects, keys):
    return {key: entry_state(projects[key]) for key in keys}


def status_for(entries):
    if not entries:
        return "no-entry"
    if all(entry["hasTrustDialogAccepted"] and entry["serverEnabled"]
           for entry in entries.values()):
        return "ok"
    return "missing"


def backup_path_for(claude_json):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = claude_json.with_name(f"{claude_json.name}.bak-{stamp}")
    number = 2
    while candidate.exists():
        candidate = claude_json.with_name(
            f"{claude_json.name}.bak-{stamp}-{number}")
        number += 1
    return candidate


def enable_server(entry):
    entry["hasTrustDialogAccepted"] = True
    servers = entry.get("enabledMcpjsonServers")
    if isinstance(servers, list):
        if SERVER_NAME not in servers:
            servers.append(SERVER_NAME)
    elif servers is None:
        entry["enabledMcpjsonServers"] = [SERVER_NAME]
    else:
        entry["enabledMcpjsonServers"] = [servers, SERVER_NAME]


def output(status, claude_json, entries, fixed=False, backup=None):
    print(json.dumps({
        "status": status,
        "claude_json": str(claude_json),
        "entries": entries,
        "fixed": fixed,
        "backup": str(backup) if backup else None,
    }, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code の MCP 承認状態を確認する")
    parser.add_argument("--repo", default=".", help="対象リポジトリ")
    parser.add_argument("--claude-json", help=".claude.json のパス")
    parser.add_argument("--fix", action="store_true", help="不足時だけ修復する")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    claude_json = (Path(args.claude_json).expanduser().resolve()
                   if args.claude_json else (Path.home() / ".claude.json").resolve())
    if not claude_json.is_file():
        output("no-file", claude_json, {})
        return 0
    try:
        raw = claude_json.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        output("broken", claude_json, {})
        return 2

    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        output("no-entry", claude_json, {})
        return 0
    keys = project_keys(projects, repo)
    entries = entries_state(projects, keys)
    status = status_for(entries)
    if not args.fix or status != "missing":
        output(status, claude_json, entries)
        return 0

    backup = backup_path_for(claude_json)
    shutil.copy2(claude_json, backup)
    for key in keys:
        enable_server(projects[key])
    encoding = "utf-8-sig" if raw.startswith(codecs.BOM_UTF8) else "utf-8"
    claude_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding=encoding)
    entries = entries_state(projects, keys)
    status = status_for(entries)
    output(status, claude_json, entries, fixed=status == "ok", backup=backup)
    return 0


if __name__ == "__main__":
    sys.exit(main())
