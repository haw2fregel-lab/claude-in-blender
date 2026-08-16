# claude-in-blender

**English** | [日本語](README.ja.md)

**Ask your Claude Code — from inside Blender.**

claude-in-blender opens a small window in Blender's N-panel and sends your requests straight into the Claude Code session you're already using. The connection runs in reverse: Claude does the work by driving Blender directly through the bundled MCP server — your desktop conversation, continued, without leaving Blender.

Everything lives in this repository: a Blender add-on + a bundled MCP server + your Claude Code. That's the whole stack.

> Work in progress (prototype v0.12.0). 1.0.0 will be the first public release. Tested on: Blender 5.1.2 / Claude Code 2.1.228 / Windows

## What it does

- Send requests from the N-panel (sidebar "Claude" tab) in your everyday words
- Context toggles — "target my selection", "check the scene info first", "look up the docs first", "check a screenshot first" — each one a single checkbox on your request
- The responder is the **same session** as your desktop Claude Code — conversation context, your settings, and your CLAUDE.md all carry over
- Every line of code Claude runs is kept in **`claude_bridge_log`** in the Text editor — you can trace afterwards exactly what was done

## Security

- **No extra keys to register** — authentication and usage both ride on your logged-in Claude Code account: subscription login runs on your subscription's quota, API accounts on API billing
- The add-on never reads, stores, or transmits credentials (API keys, tokens)
- Claude launched from the panel only gets the bundled MCP tools — Claude Code's file tools are never handed over
- The endpoint binds to 127.0.0.1 only; connecting requires a session token
- `execute_code` runs Python inside Blender — assume it can do anything the Blender process can do, because in principle it can. Set `CLAUDE_BRIDGE_EXECUTE=0` to disable it

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
