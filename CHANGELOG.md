# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-08-22

First public release. Everything below shipped together; earlier `0.x` tags were
development milestones and were never published.

### Added

- **Send requests to Claude Code from Blender's N-panel.** Type in your everyday words
  and press Send; Claude drives Blender back through the bundled MCP server.
- **Forked sessions.** The panel grows a copy of your desktop Claude Code session —
  conversation context and your `CLAUDE.md` carry over, and the original conversation is
  never written to. The panel uses its own model selection and restricted tool/MCP
  configuration.
- **Context toggles.** "Target my selection", "check the scene info first", "look up the
  docs first" and "check a screenshot first", each a single checkbox on the request.
- **A traceable log.** `claude_bridge_log` keeps submitted code, 200-character result
  excerpts, and error excerpts; it does not capture stdout or stderr and trims older lines
  after 5,000 lines.
- **Model selection** for requests that start a new session.
- **File-switch notification.** If you open a different `.blend` along the way, the panel
  asks before your next request, and the tool response tells Claude that the file
  changed. Operations are reported, never silently refused.
- **`SECURITY.md`** documenting the trust boundary, what pressing Send authorizes, where
  each kind of data lives, and the known limitations.
- **`/blender-setup`** — one command that installs the Python dependencies, the add-on
  and the bridge registration, then checks connectivity. It also detects the MCP approval
  gate and only repairs it after you confirm.
- **`/blender-bridge`** — point the panel at a different conversation.

### Security

- The bridge binds to `127.0.0.1` only and requires a per-session token.
- Claude launched from the panel gets the bundled MCP tools and nothing else; Claude
  Code's own file and shell tools are switched off.
- The add-on itself does not request or use API keys. Authentication and usage ride on
  your already-logged-in Claude Code account; code passed to `execute_code` has Blender's
  full file and network access.
- `execute_code` can be disabled entirely with `CLAUDE_BRIDGE_EXECUTE=0`.

### Known limitations

- Switching `.blend` files mid-request is reported, not blocked.
- A request in flight cannot be cancelled.
- Tested on Windows. macOS support is implemented but not yet verified on hardware;
  Linux is untested.

[1.0.0]: https://github.com/haw2fregel-lab/claude-in-blender/releases/tag/v1.0.0
