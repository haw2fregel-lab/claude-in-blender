# claude-in-blender

**English** | [日本語](README.ja.md)

**Ask your Claude Code — from inside Blender.** Same context, same CLAUDE.md — in a forked session of its own. The original conversation is never touched.

claude-in-blender opens a small window in Blender's N-panel and sends your requests straight into the Claude Code session you're already using. The connection runs in reverse: Claude does the work by driving Blender directly through the bundled MCP server — your desktop conversation carried over, without leaving Blender.

Everything lives in this repository: a Blender add-on + a bundled MCP server + your Claude Code. That's the whole stack.

> **v1.0.0** — Tested on: Blender 5.1.2 / Claude Code 2.1.228 / Windows (macOS/Linux untested) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md)

## What it does

- Send requests from the N-panel (sidebar "Claude" tab) in your everyday words
- Context toggles — "target my selection", "check the scene info first", "look up the docs first", "check a screenshot first" — each one a single checkbox on your request
- The responder is a **fork** of your desktop Claude Code session — conversation context, your settings, and your CLAUDE.md all carry over, and the original conversation is never written to
- If you open a different `.blend` along the way, the panel asks before your next request, and Claude is told the file changed — nothing lands silently in the wrong file
- Every line of code Claude runs is kept in **`claude_bridge_log`** in the Text editor — you can trace afterwards exactly what was done

## Security

- **No extra keys to register** — authentication and usage both ride on your logged-in Claude Code account: subscription login runs on your subscription's quota, API accounts on API billing. The add-on never reads, stores, or transmits credentials
- **Pressing Send is the approval.** Claude Code's own file and shell tools are switched off, but every bundled MCP tool is pre-approved for that request — there is no second confirmation once you send
- **`execute_code` is not a sandbox** — it runs Python inside Blender, so assume it can do anything the Blender process can. `CLAUDE_BRIDGE_EXECUTE=0` disables it
- **Claude can search this project's past sessions** (`search_session_history`), and the panel reads the same transcripts to list your recent sessions
- **The log is saved inside your `.blend`** — `claude_bridge_log` keeps every line Claude ran and travels with the file. Clear it before sharing the file
- The endpoint binds to 127.0.0.1 only and requires a session token — a boundary against other machines, not against other processes running as you

**Read [SECURITY.md](SECURITY.md)** for the full trust boundary, where each kind of data lives, and how to turn things off.

## Requirements

- [Claude Code](https://claude.com/claude-code) (logged in — subscription or API account both work; check which one you're on before you start)
- Python 3.10+ (for the MCP server)
- Blender 4.2+ (the Extension format's minimum; tested on 5.1.2 / Windows)

## Get started

1. Clone this repository and open it in Claude Code — the bundled `.mcp.json` registers the MCP server (you'll approve it once)
2. Ask Claude Code to **set up Blender** (`/blender-setup`) — Python deps, add-on, bridge registration and a connectivity check, in one go
3. In the 3D Viewport: N key → "Claude" tab → write your request and send

Want the panel to carry the context of another conversation? Say **connect Blender** (`/blender-bridge`). The panel grows a copy (fork) of that session as its own — nothing is ever appended to the original conversation.

## License

[GPL-3.0-or-later](LICENSE). As a Blender add-on (using bpy), it follows Blender's own GPL.
