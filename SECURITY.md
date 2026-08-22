# Security

This add-on runs code inside Blender on your machine, launched by your own Claude Code
account. That is the point of the tool — and it is also the whole security story. This
page states plainly what is trusted, what is authorized, and where your data ends up, so
you can decide whether that trade is one you want.

## Threat model in one line

**All processes running under your user account are trusted; the bridge is not reachable
from outside this host.** The add-on is designed to expose Blender's capabilities to a
local Claude Code process, not to sandbox that process. If an attacker already runs code as your user, this add-on gives them no
new capability they did not already have — but it also does not defend against them.

## The trust boundary

- The bridge listens on **`127.0.0.1:9877` only** (`_DEFAULT_PORT`, configurable). It is never
  exposed to the network, and no inbound connection from another host is possible.
- Every request must carry a **session token** — generated with `secrets.token_hex(16)`, a
  32-character hexadecimal string representing 16 random bytes, regenerated each time the
  bridge starts. Requests with a wrong token are rejected.
- The token is published as a plain-text file at
  `<system temp dir>/claude-in-blender/blender-session-token`, protected by ordinary
  filesystem permissions. It is removed when the bridge stops.

**What this means:** the token separates the bridge from *other users and other machines*,
not from *other processes running as you*. Any process with your user's privileges can read
the token file and drive Blender through the bridge. On a shared or compromised account,
treat the bridge as open.

## What pressing Send authorizes

Sending a request from the panel launches Claude Code with these flags:

```
--strict-mcp-config --mcp-config <repo>/.mcp.json --tools "" \
--allowedTools mcp__claude-in-blender__*
```

Read that as two separate facts:

- **Claude Code's built-in tools are disabled.** `--tools ""` means no file reads, no file
  writes, no shell. The bundled MCP server is the only way out.
- **Every bundled MCP tool is pre-approved.** `--allowedTools mcp__claude-in-blender__*`
  means that once you press Send, Claude will run those tools **without asking you again**
  for that request. There is no per-tool confirmation dialog. The Send button *is* the
  approval.

Your request text is passed on **stdin**, not as a command-line argument, so it does not
appear in the process list.

## What Claude can reach

Through the bundled MCP server, a request can:

- **`execute_code` — run arbitrary Python inside Blender.** This is **not a sandbox**.
  Blender's Python can read and write files, open network connections, and call into the
  OS. Assume anything the Blender process can do, this tool can do, because in principle
  it can.
- **`search_session_history` — read your past Claude Code transcripts for this project.**
  It searches the `.jsonl` session files under your Claude Code projects directory for the
  current project and returns matching excerpts. If earlier conversations in this project
  contained sensitive text, that text is reachable.
- Read scene, object, and selection data; capture viewport screenshots; look up Blender
  API documentation; write and run scratch `.py` files under the temp directory.

The panel's session picker also reads those same `.jsonl` transcript files, in order to
list your recent sessions by their opening message.

## Where data lives

| What | Where | Survives Blender restart? |
| --- | --- | --- |
| Every line of code Claude ran | `claude_bridge_log`, a Text datablock | **Yes — it is saved inside your `.blend` file** |
| Session token | `<temp>/claude-in-blender/blender-session-token` | No — removed on stop |
| Scratch scripts | `<temp>/claude-in-blender/scratch/` | Yes, until the temp dir is cleared |
| Conversation transcripts | Your Claude Code projects directory | Yes — managed by Claude Code, not by this add-on |

**The log lives in your `.blend`.** If you share or publish a `.blend` file that was used
with this add-on, you are also sharing the full record of code that was run in it,
including anything that code printed. Use the panel's **Clear log** button before handing
the file to someone else.

The add-on never reads, stores, or transmits credentials. Authentication and usage both
run through your already-logged-in Claude Code account — a subscription login spends
subscription quota, an API account bills through the API. No additional key is registered
anywhere.

## Known limitations

These are design decisions, not oversights. They are listed here so you can judge them.

- **Switching `.blend` files mid-request is reported, not blocked.** If you open a
  different file while Claude is still working, the panel warns you before your *next*
  request, and the tool response tells Claude that the file changed. The operation itself
  is not refused — the judgement is deliberately left to you and to Claude. An operation
  that arrives after the switch acts on the file that is open at that moment.
- **A request in flight cannot be cancelled.** It runs to completion or times out — 30
  seconds per script, 60 seconds waiting on Blender's main thread, 300 seconds at the
  panel. Closing the panel does not stop the Claude process that was launched.
- **`CLAUDE_BRIDGE_EXECUTE=0` is read once, when the bridge starts.** Changing the
  environment variable while Blender is running has no effect until the add-on is
  re-enabled.

## Turning things off

- `CLAUDE_BRIDGE_EXECUTE=0` in the environment Blender starts with disables `execute_code`
  entirely. Read-only tools (scene info, screenshots, docs) keep working. This is the
  single most effective control if you want inspection without mutation.
- Disabling the add-on stops the listener and removes the token file.

## Reporting a vulnerability

Please open a
[security advisory](https://github.com/haw2fregel-lab/claude-in-blender/security/advisories/new)
rather than a public issue. Include the Blender version, the OS, and the smallest sequence
of steps that reproduces the problem.
