# claude-in-blender

**English** | [日本語](README.ja.md)

**Ask Claude Code — from inside Blender.** Your hands stay in the viewport — Claude sees your selection and helps from a side panel.

claude-in-blender adds a small panel to Blender's N-panel and sends your requests to a fork of the Claude Code session you're already using. The connection runs in reverse: Claude does the work by driving Blender directly through the bundled MCP server — carrying over the context of your desktop conversation, without ever leaving Blender.

Everything you need to add lives in this repository: a Blender add-on + a bundled MCP server. Plus the Claude Code you already have — that's the whole stack.

> **v1.0.0** — Tested on: Blender 5.1.2 / Claude Code 2.1.228 / Windows + macOS (Linux untested) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md)

## What it does

- Send requests in plain language from the "Claude" tab in Blender's N-panel
- Context toggles — "target my selection", "check the scene info first", "look up the docs first", and "check a screenshot first" — each available as a single checkbox per request. Toggles point at the live state: "my selection" means whatever is selected at the moment Claude checks, not a snapshot taken when you press Send
- The panel uses a **fork** of your existing Claude Code session — your conversation context and CLAUDE.md carry over, while the original session is never written to. The panel uses its own model selection and restricted tool/MCP configuration.
- If the `.blend` changes, the panel warns before the next request and the tool response reports the switch as `file_switched`. In-flight operations are not blocked.
- **`claude_bridge_log`** in Blender's Text Editor keeps submitted code, 200-character result excerpts, and error messages — and when an error is too large for the response (over 2 KB), its full message and traceback go to the log. It does not capture stdout or stderr; after 5,000 lines, it trims the older side and retains about 2,500 recent lines.

## Security

- **No additional credentials to configure** — authentication and billing both run through your logged-in Claude Code account: a subscription login spends subscription quota, an API account bills through the API. The add-on itself does not request or use API keys; Python passed through `execute_code` has Blender's full file and network access
- **Pressing Send is your approval.** Claude Code's built-in file and shell tools are disabled, but every bundled MCP tool is pre-approved for that request — there is no second confirmation once you send
- **`execute_code` is not a sandbox** — it runs Python inside Blender, so assume it can do anything the Blender process can. `CLAUDE_BRIDGE_EXECUTE=0` disables it
- **Long operations make Blender's UI appear frozen** — mutating code runs on Blender's main thread, with no hard cancellation and no rollback. Save your work before asking for a heavy operation
- **Claude can search this project's past sessions** (`search_session_history`), and the panel reads the same transcripts to list your recent sessions
- **The log is stored inside your `.blend`** — `claude_bridge_log` travels with the file and can contain submitted code, result excerpts, error messages, and the full traceback of any large error. Clear it before sharing the file
- Only one bridge can be active for the same user at a time (default port 9877). It listens on 127.0.0.1 only and requires a session token — this protects against other machines, not against other processes running under your account

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

**Work dir**, at the top of the panel, is where your requests run. **It does not have to be this repository** — point it at your own project and that repository's `CLAUDE.md` and skills apply. The **+** next to the dropdown picks one in the file browser (one-off: it is not kept in the history). To register one into the history, run this from this repository:

```bash
python tools/bridge_register.py --cwd /path/to/your/project
```

Registered directories stay in the dropdown (latest five). Switching drops the current conversation, so both the picker and the **+** are disabled while connected — disconnect first.

Modeling know-how ships as a Skill ([`.claude/skills/blender-modeling/`](.claude/skills/blender-modeling/SKILL.md)) — where names and enums moved in Blender 5.x, how data names behave under a translated UI, how to verify Geometry Nodes. It is available to panel requests too, but whether Claude actually reads it is Claude's own call based on what you asked (no instruction forcing the load is added to your message). **Feel free to copy it into your own repositories for Blender work elsewhere** — unlike the rest of this repository (GPL-3.0), the skill is dedicated to the public domain ([CC0](.claude/skills/blender-modeling/LICENSE)), so no attribution and no license obligations follow it.

## License

[GPL-3.0-or-later](LICENSE). As a Blender add-on (using bpy), it follows Blender's own GPL.
