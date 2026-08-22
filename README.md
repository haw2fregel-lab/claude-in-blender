# claude-in-blender

**English** | [日本語](README.ja.md)

**Ask Claude Code — from inside Blender.** Same context, same CLAUDE.md — in a separate forked session. The original session is never touched.

claude-in-blender adds a small panel to Blender's N-panel and sends your requests to a fork of the Claude Code session you're already using. The connection runs in reverse: Claude does the work by driving Blender directly through the bundled MCP server — carrying over the context of your desktop conversation, without ever leaving Blender.

Everything you need to add lives in this repository: a Blender add-on + a bundled MCP server. Plus the Claude Code you already have — that's the whole stack.

> **v1.0.0** — Tested on: Blender 5.1.2 / Claude Code 2.1.228 / Windows (macOS/Linux untested) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md)

## What it does

- Send requests in plain language from the "Claude" tab in Blender's N-panel
- Context toggles — "target my selection", "check the scene info first", "look up the docs first", and "check a screenshot first" — each available as a single checkbox per request
- The panel uses a **fork** of your existing Claude Code session — your conversation context, settings, and CLAUDE.md all carry over, while the original session is never written to
- If you switch to a different `.blend` file, the panel asks you to confirm before the next request and tells Claude that the file changed — so nothing lands silently in the wrong file
- Every line of code Claude runs is recorded in **`claude_bridge_log`** in Blender's Text Editor — you can trace exactly what was done afterward

## Security

- **No additional credentials to configure** — authentication and billing both run through your logged-in Claude Code account: a subscription login spends subscription quota, an API account bills through the API. The add-on never reads, stores, or transmits credentials
- **Pressing Send is your approval.** Claude Code's built-in file and shell tools are disabled, but every bundled MCP tool is pre-approved for that request — there is no second confirmation once you send
- **`execute_code` is not a sandbox** — it runs Python inside Blender, so assume it can do anything the Blender process can. `CLAUDE_BRIDGE_EXECUTE=0` disables it
- **Claude can search this project's past sessions** (`search_session_history`), and the panel reads the same transcripts to list your recent sessions
- **The log is stored inside your `.blend`** — `claude_bridge_log` contains every line of code Claude executed and travels with the file. Clear it before sharing the file
- The bridge listens on 127.0.0.1 only and requires a session token — this protects against other machines, not against other processes running under your account

**Read [SECURITY.md](SECURITY.md)** for the full trust boundary, where each kind of data lives, and how to turn things off.

## Requirements

- [Claude Code](https://claude.com/claude-code) (logged in — subscription or API account both work; check which one you're on before you start)
- Python 3.10+ (for the MCP server)
- Blender 4.2+ (the Extension format's minimum; tested on 5.1.2 / Windows)

## Get started

1. Clone this repository and open it in Claude Code — the bundled `.mcp.json` registers the MCP server (you'll approve it once)
2. Ask Claude Code to **set up Blender** (`/blender-setup`) — Python deps, add-on, bridge registration and a connectivity check, in one go
3. In the 3D Viewport: N key → "Claude" tab → write your request and send

Want the panel to carry the context of another conversation? Say **connect Blender** (`/blender-bridge`). The panel forks that session and works from the copy — nothing is ever appended to the original session.

## License

[GPL-3.0-or-later](LICENSE). As a Blender add-on (using bpy), it follows Blender's own GPL.
