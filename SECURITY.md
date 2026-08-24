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

- Only one bridge can be active for the same user at a time, on the default
  **`127.0.0.1:9877`**. It is never exposed to the network, and no inbound connection
  from another host is possible.
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
--strict-mcp-config --mcp-config <generated: one server> \
--tools Skill -p \
--allowedTools "Skill(blender-modeling)" "Skill(blender-quick-edit)" \
    "Skill(blender-param-panel)" "Skill(blender-modifier-inject)" \
    mcp__claude-in-blender__* \
--disallowedTools "Skill(blender-setup)" "Skill(blender-update)" "Skill(blender-bridge)"
```

Read that as three separate facts:

- **Only the bundled MCP server is visible.** `--strict-mcp-config` discards every MCP
  server configured anywhere else — user-level, project-level, `.mcp.json`. The config is
  generated at send time and names exactly one server: the one shipped with this add-on.
  Anything else you have connected to Claude Code is out of reach from the panel.
- **Built-in tools are limited to `Skill`.** No file reads, no file writes, no shell, no
  web. `Skill` is enabled so requests can load the bundled skills (the modeling notes and
  the three adjustment skills). Two honest caveats: the `--allowedTools` entries do **not**
  act as a filter — in current Claude Code, a skill *not* named there can still load, so
  any skill present in the work directory's repository is readable by a request. What
  actually closes a skill is `--disallowedTools`, which is why the developer skills
  (setup, update, bridge) are denied there by name (verified by test: a denied skill fails
  to load).
- **What is left is pre-approved.** Once you press Send, Claude runs the bundled MCP tools
  and that one skill **without asking you again** for that request. The request runs
  non-interactively (`-p`), so there is no per-tool confirmation dialog to fall back on.
  The Send button *is* the approval.

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

| What | Where | Lifecycle and removal |
| --- | --- | --- |
| Submitted code, 200-character result excerpts, and error messages — plus the full message and traceback when an error is too large for the response (over 2 KB) | `claude_bridge_log`, a Text datablock | Created by `execute_code` and saved inside your `.blend`; use the panel's **Clear log** button to remove it. It does not capture stdout or stderr, and after 5,000 lines it retains about 2,500 recent lines. |
| Session token | `<temp>/claude-in-blender/blender-session-token` | Created when the bridge starts; removed when it stops. |
| Scratch scripts | `<temp>/claude-in-blender/scratch/<pid>-<random>/` — one directory per MCP server process | Created by scratch-file operations; a script can only be run by the process that wrote it. Directories accumulate (one per process) and remain until the temp directory is cleared. |
| Conversation transcripts | Your Claude Code projects directory | Managed by Claude Code, not by this add-on. |
| Bridge session registration | `~/.claude/blender-bridge-session.json` | Created or updated by bridge registration and panel connection changes; contains the work directory, the add-on source repository, up to five recently used work directories, the Claude executable, fork source/session ID, model, and registration time. It persists until you delete the file. |
| Approval-repair backup | Beside `.claude.json`, as `.claude.json.bak-*` | Created only when approval repair changes `.claude.json`; keeps the pre-repair settings until you delete the backup. |
| Viewport screenshot PNG | `<temp>/claude-in-blender/viewport-*.png` | Created when a viewport screenshot is requested and normally deleted after the MCP server reads it. A crash can leave it in temp; the next bridge start cleans it, or you can delete it manually. |

**The log lives in your `.blend`.** If you share or publish a `.blend` file that was used
with this add-on, you are also sharing submitted code, result excerpts, error messages,
and possibly the full traceback of a large error. Use the panel's **Clear log** button
before handing the file to someone else.

The add-on itself does not request or use API keys. Authentication and usage both run
through your already-logged-in Claude Code account — a subscription login spends
subscription quota, an API account bills through the API. Python passed through
`execute_code` has Blender's full file and network access.

## Known limitations

These are design decisions, not oversights. They are listed here so you can judge them.

- **Switching `.blend` files mid-request is reported, not blocked.** If you open a
  different file while Claude is still working, the panel warns you before your *next*
  request, and the tool response tells Claude that the file changed. The operation itself
  is not refused — the judgement is deliberately left to you and to Claude. An operation
  that arrives after the switch acts on the file that is open at that moment.
- **A request in flight cannot be cancelled, and the UI freezes while code runs.**
  Mutating code runs on Blender's main thread, so a long operation makes Blender appear
  unresponsive — it is working, not broken. The 30-second per-script deadline uses
  `sys.settrace`, so it is not a hard stop and can be exceeded while a native call blocks.
  The 60-second bridge wait for Blender's main thread and the 300-second panel wait are
  caller observation deadlines: the Blender-side operation can continue, and there is no
  rollback. Save your work before asking for a heavy operation. Closing the panel does
  not stop the Claude process that was launched.
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
