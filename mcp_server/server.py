import base64
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from bridge import BlenderBridge

_SCRATCH_DIR = os.path.join(tempfile.gettempdir(), "claude-in-blender", "scratch")
_SCRATCH_MAX_BYTES = 1024 * 1024
_CLAUDE_CONFIG_DIR = Path(
    os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
)
_PROJECTS_ROOT = _CLAUDE_CONFIG_DIR / "projects"

mcp = FastMCP(
    "claude-in-blender",
    instructions=(
        "Blender 3D scene manipulation.\n\n"
        "Combine related operations into one execute_code call to save tokens,"
        " but keep enough granularity to identify the state after a partial"
        " failure. For example, creating an object, setting its material, and"
        " adjusting lighting can be one script when that granularity is retained.\n\n"
        "Mutating scripts are not transactional. If a script fails, assume earlier"
        " statements may already have changed Blender. Inspect the affected state"
        " before retrying, then run only the unapplied or corrective steps.\n\n"
        "Mutating code runs on Blender's main thread and the UI freezes until the"
        " script returns. For heavy work, inspect state with read-only tools first,"
        " then apply mutations in short steps instead of one long script.\n\n"
        "For anything longer than a few lines, use write_scratch"
        f" to create a .py file under {_SCRATCH_DIR}"
        " and run it with execute_file. Use edit_scratch for a"
        " small edit and re-run it; this is much cheaper than"
        " resending the whole script through execute_code.\n\n"
        "Use get_doc to look up API names before writing code"
        " to avoid retries from wrong function names or"
        " parameters.\n\n"
        "Verify results through execute_code return values,"
        " not screenshots. When you need to verify the"
        " outcome, set the result variable to report what"
        " changed (created object name, count, etc.)."
        " Only use get_viewport_screenshot or capture_after"
        " when you need to check visual appearance"
        " (lighting, materials, composition).\n\n"
        "Write execute_code scripts as straight-line throwaway"
        " code: the goal is already decided, so no defensive"
        " if-branches, no comments, no helper abstractions."
        " If it fails, inspect the affected state before deciding which"
        " unapplied or corrective code to run. (Detailed result values"
        " for verification are still fine.)\n\n"
        "When the current state is uncertain (selection,"
        " dimensions, existing names), do not hedge with"
        " if-branches. Split the work: first send a read-only"
        " query script (or use get_selection / get_scene_info /"
        " get_object_info) to learn the state, then write the"
        " straight-line action script against the confirmed"
        " conditions.\n\n"
        "When execute_code times out with outcome_unknown, do not retry."
        " First call get_request_status with the returned request ID."
        " A new execute remains blocked until that request reaches succeeded"
        " or failed and its final status is observed. Use scene-info tools"
        " afterward when you also need to verify the resulting Blender state."
        " If the bridge restarted or get_request_status cannot return a status,"
        " inspect the scene state before retrying."
    ),
)
bridge = BlenderBridge()


def _fmt(result: dict) -> str:
    """Format bridge result for AI — strip envelope, compact JSON."""
    return json.dumps(result["data"], ensure_ascii=False)


def _bridge_error(result: dict, prefix: str = "") -> RuntimeError:
    # prefix は封筒の外から来る注意書き（ファイル切替）。失敗の文面より前に置く。
    error = result.get("error") or {}
    msg = error.get("message", "Unknown error")
    tb = error.get("traceback")
    if result.get("status") == "outcome_unknown" and result.get("request_id"):
        request_id = result["request_id"]
        guidance = f"Call get_request_status({request_id!r}) before another execute."
        if guidance not in msg:
            msg = f"{msg}\n{guidance}"
    return RuntimeError(prefix + (f"{msg}\n{tb}" if tb else msg))


def _validate_scratch_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]+", name)
        or ".." in name
    ):
        raise RuntimeError(
            "Scratch file name must use only A-Z, a-z, 0-9, ., _, or - and not contain '..'"
        )
    if not name.endswith(".py"):
        raise RuntimeError("Scratch file name must end with .py")
    return name


def _validate_scratch_content(content: str) -> int:
    content_size = len(content.encode("utf-8"))
    if content_size > _SCRATCH_MAX_BYTES:
        raise RuntimeError(
            "Scratch content exceeds the 1 MiB limit "
            f"({_SCRATCH_MAX_BYTES:,} bytes): {content_size:,} bytes"
        )
    return content_size


def _reject_scratch_symlink(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(
            f"Cannot write scratch file: refusing to overwrite symlink: {path}"
        )


def _write_scratch_file(path: Path, content: str) -> None:
    temp_path = None
    try:
        _validate_scratch_content(content)
        _reject_scratch_symlink(path)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(content)
        # os.replace never follows the destination symlink. The pre-check gives
        # callers a clear refusal for an existing symlink; replacement stays safe
        # if one appears before this atomic swap.
        os.replace(temp_path, path)
        temp_path = None
    except OSError as e:
        raise RuntimeError(f"Cannot write scratch file: {e}") from e
    finally:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _resolve_scratch_file(path: str) -> Path:
    try:
        scratch_dir = Path(_SCRATCH_DIR).resolve()
        scratch_file = Path(path).resolve(strict=False)
    except (OSError, TypeError, ValueError):
        raise RuntimeError(f"Invalid scratch file path: {path}") from None
    try:
        scratch_file.relative_to(scratch_dir)
    except ValueError:
        raise RuntimeError(
            f"Scratch file must be under {_SCRATCH_DIR}: {path}"
        ) from None
    if scratch_file.suffix != ".py":
        raise RuntimeError(f"Scratch file must end with .py: {path}")
    if not scratch_file.is_file():
        raise RuntimeError(f"Scratch file does not exist: {path}")
    try:
        file_size = scratch_file.stat().st_size
    except OSError as e:
        raise RuntimeError(f"Cannot inspect scratch file: {path}: {e}") from e
    if file_size > _SCRATCH_MAX_BYTES:
        raise RuntimeError(
            "Scratch file exceeds the 1 MiB limit "
            f"({_SCRATCH_MAX_BYTES:,} bytes): {path}"
        )
    return scratch_file


def _project_slug() -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path.cwd()))


def _history_search_summary(
    searched: int,
    total: int,
    too_large: int,
    unreadable: int,
    matches: int | None = None,
) -> str:
    """Format the observable scope of a session-history search."""
    parts = [f"searched {searched} of {total} files"]
    if matches is not None:
        parts[0] += " (newest first)"
    skipped = too_large + unreadable
    if skipped:
        skipped_parts = []
        if too_large:
            skipped_parts.append(f"too large: {too_large}")
        if unreadable:
            skipped_parts.append(f"unreadable: {unreadable}")
        parts.append(f"skipped {skipped} ({', '.join(skipped_parts)})")
    if matches is not None:
        parts.append(f"showing {matches} matches")
    return f"[{', '.join(parts)}]"


def _message_text(record: dict) -> str:
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _session_timestamp(value: object) -> str:
    if not isinstance(value, str):
        return "--/-- --:--"
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%m/%d %H:%M")
        )
    except (OSError, OverflowError, ValueError):
        return "--/-- --:--"


def _session_excerpt(text: str, query_word: str) -> str:
    match_index = text.lower().find(query_word)
    start = max(match_index - 80, 0)
    end = min(match_index + len(query_word) + 80, len(text))
    excerpt = text[start:end].replace("\r", " ").replace("\n", " ")
    return f"{'...' if start else ''}{excerpt}{'...' if end < len(text) else ''}"


@mcp.tool()
def get_bridge_status() -> str:
    """Check if Blender is connected and the bridge addon is running."""
    result = bridge.send("ping")
    if not result.get("ok"):
        raise _bridge_error(result)
    return _fmt(result)


@mcp.tool()
def get_request_status(request_id: str) -> str:
    """Get the tracked status of an execute_code request.

    Use this first when execute_code reports outcome_unknown. A final succeeded
    or failed response acknowledges the result and permits the next execute.

    Args:
        request_id: Request ID returned by the timed-out execute_code call
    """
    result = bridge.send("get_request_status", {"operation_id": request_id})
    if not result.get("ok"):
        raise _bridge_error(result)
    return _fmt(result)


@mcp.tool()
def get_viewport_screenshot() -> list:
    """Capture a screenshot of Blender's 3D viewport."""
    result = bridge.send("get_viewport_screenshot")
    if not result.get("ok"):
        raise _bridge_error(result)
    data = result["data"]
    path = data["screenshot_path"]
    try:
        with open(path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("ascii")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return [
        TextContent(
            type="text",
            text=f"Viewport screenshot ({data['width']}x{data['height']})",
        ),
        ImageContent(
            type="image",
            data=img_b64,
            mimeType="image/png",
        ),
    ]


@mcp.tool()
def get_scene_info() -> str:
    """Get current Blender scene info: objects, types, positions, settings."""
    result = bridge.send("get_scene_info")
    if not result.get("ok"):
        raise _bridge_error(result)
    return _fmt(result)


@mcp.tool()
def write_scratch(name: str, content: str) -> str:
    """Write a Python script to the scratch directory for execute_file."""
    name = _validate_scratch_name(name)
    content_size = _validate_scratch_content(content)
    os.makedirs(_SCRATCH_DIR, exist_ok=True)
    path = Path(_SCRATCH_DIR) / name
    _write_scratch_file(path, content)
    return f"Wrote {path.resolve()} ({content_size} bytes)"


@mcp.tool()
def edit_scratch(name: str, old: str, new: str) -> str:
    """Replace one unique snippet in a scratch Python script before re-running it."""
    name = _validate_scratch_name(name)
    path = Path(_SCRATCH_DIR) / name
    try:
        _reject_scratch_symlink(path)
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"Cannot read scratch file: {e}") from e
    matches = content.count(old)
    if matches != 1:
        raise RuntimeError(f"Expected one match for old text, found {matches}")
    _write_scratch_file(path, content.replace(old, new, 1))
    return f"Updated {path.resolve()} (replaced 1 unique match)"


# bridge が file_switched を立てた時、Claude が読む返答文の頭に置く注意。
# 弾かずに知らせる形なので、成否どちらの返答にも同じ一文を前置きする。
_FILE_SWITCH_NOTICE = (
    "The .blend file was switched since the previous operation. Re-check the scene "
    "state before assuming anything about object names, selection, or the file path."
)


def _run_code(code: str, capture_after: bool, filename: str = "<execute_code>") -> list:
    # 構文エラーは Blender まで運ばず手前で弾く（行番号はファイルの実行と一致する）
    try:
        compile(code, filename, "exec")
    except SyntaxError as e:
        raise RuntimeError(
            f"SyntaxError: {e.msg} ({filename}, line {e.lineno}): {(e.text or '').strip()}"
        )
    result = bridge.send("execute_code", {"code": code, "filename": filename})
    # 切替が挟まった時だけ bridge が印を立てる（通常はキー自体が来ない）
    notice = f"{_FILE_SWITCH_NOTICE}\n\n" if result.get("file_switched") else ""
    if not result.get("ok"):
        raise _bridge_error(result, prefix=notice)

    def _fmt_exec(res: dict) -> str:
        data = res["data"]
        val = data["result"]
        if val is None:
            text = "OK"
        elif isinstance(val, str):
            text = val
        else:
            text = json.dumps(val, ensure_ascii=False)
        if data.get("output_truncated"):
            return (
                "[output truncated: showing first ~50 KB of "
                f"{data['original_bytes']:,} bytes]\n{text}"
            )
        return text

    text_content = TextContent(type="text", text=notice + _fmt_exec(result))
    if not capture_after:
        return [text_content]

    ss_result = bridge.send("get_viewport_screenshot")
    if not ss_result.get("ok"):
        reason = (ss_result.get("error") or {}).get("message") or "Unknown error"
        return [
            TextContent(
                type="text",
                text=(
                    f"{text_content.text}\n"
                    f"Execution succeeded, but capture_after failed: {reason}"
                ),
            )
        ]

    data = ss_result["data"]
    path = data["screenshot_path"]
    try:
        with open(path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        return [
            TextContent(
                type="text",
                text=(
                    f"{text_content.text}\n"
                    f"Execution succeeded, but capture_after failed: {e}"
                ),
            )
        ]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    return [
        text_content,
        ImageContent(
            type="image",
            data=img_b64,
            mimeType="image/png",
        ),
    ]


@mcp.tool()
def execute_code(code: str, capture_after: bool = False) -> list:
    """Execute Python code inside Blender (bpy available).
    Set a variable called 'result' to return data back.
    Mutating scripts are not transactional; inspect affected state before retrying.

    For anything longer than a few lines, use write_scratch then execute_file.

    Args:
        code: Python code to execute in Blender's context
        capture_after: If True, capture a viewport screenshot after execution
    """
    return _run_code(code, capture_after)


@mcp.tool()
def execute_file(path: str, capture_after: bool = False) -> list:
    """Execute a Python file inside Blender (bpy available).
    Set a variable called 'result' in the file to return data back.
    Mutating scripts are not transactional; inspect affected state before retrying.
    Only accepts a .py file up to 1 MiB at the absolute scratch path returned by
    write_scratch.

    Prefer this over execute_code for longer scripts: use write_scratch,
    run the returned path, fix it with edit_scratch, re-run — much cheaper than
    resending the whole script. Syntax errors are caught locally
    (with file line numbers) before reaching Blender.

    Args:
        path: Absolute scratch path returned by write_scratch (.py, maximum 1 MiB)
        capture_after: If True, capture a viewport screenshot after execution
    """
    scratch_file = _resolve_scratch_file(path)
    try:
        with scratch_file.open(encoding="utf-8") as f:
            code = f.read()
    except OSError as e:
        raise RuntimeError(f"Cannot read file: {e}")
    return _run_code(code, capture_after, filename=path)


@mcp.tool()
def get_selection() -> str:
    """Get current selection state in Blender.

    In Object mode: selected objects and active object.
    In Edit mode: count of selected vertices, edges, and faces.
    """
    result = bridge.send("get_selection")
    if not result.get("ok"):
        raise _bridge_error(result)
    return _fmt(result)


@mcp.tool()
def get_object_info(name: str = "") -> str:
    """Get detailed info about a Blender object.

    Returns vertex/face counts, materials, modifiers, UV layers, etc.

    Args:
        name: Object name. If empty, uses the active object.
    """
    params = {"name": name} if name else {}
    result = bridge.send("get_object_info", params)
    if not result.get("ok"):
        raise _bridge_error(result)
    return _fmt(result)


@mcp.tool()
def get_doc(identifier: str) -> str:
    """Look up Blender Python API documentation.

    Modes:
    - Direct: 'bpy.ops.mesh.primitive_cube_add', 'bpy.types.Object'
    - Wildcard: 'bpy.ops.mesh.*' (lists members)
    - Keyword search: 'primitive', 'material', 'Vector'
      (searches bpy.ops, bpy.types, mathutils, bmesh)

    Allowed roots: bpy, mathutils, bmesh, bpy_extras.

    Args:
        identifier: Dotted path for direct/wildcard lookup, or a keyword for search
    """
    result = bridge.send("get_doc", {"identifier": identifier})
    if not result.get("ok"):
        raise _bridge_error(result)
    return _fmt(result)


@mcp.tool()
def search_session_history(query: str, max_results: int = 8) -> str:
    """Search previous Claude Code session transcripts for this project.

    Searches the newest 10 JSONL files, reading files up to 10 MiB each, and
    returns at most 20 excerpts with roughly 4,000 characters of excerpt text.

    Args:
        query: Space-separated terms that all must appear in a message.
        max_results: Maximum number of matching excerpts to return.
    """
    no_match = f"No match for: {query}"
    if not isinstance(query, str):
        return no_match
    query_words = query.lower().split()
    if not query_words:
        return no_match
    try:
        result_limit = min(max(int(max_results), 1), 20)
    except (OverflowError, TypeError, ValueError):
        result_limit = 8

    try:
        project_dir = _PROJECTS_ROOT / _project_slug()
        if not project_dir.is_dir():
            return "Session history unavailable: project directory not found"

        all_session_files = list(project_dir.glob("*.jsonl"))
        session_files = []
        too_large = 0
        unreadable = 0
        for path in all_session_files:
            try:
                stat_result = path.stat()
            except OSError:
                unreadable += 1
                continue
            # 10MB 超は候補に入れない（「新しい10件」の枠を skip で食わないよう、読む前に絞る）
            if stat_result.st_size > 10 * 1024 * 1024:
                too_large += 1
                continue
            session_files.append((stat_result.st_mtime, path))

        excerpts = []
        excerpt_length = 0
        searched = 0
        for _, path in sorted(session_files, key=lambda item: item[0], reverse=True)[
            :10
        ]:
            try:
                with path.open(encoding="utf-8") as f:
                    lines = f.readlines()
            except (OSError, UnicodeError):
                unreadable += 1
                continue

            records = []
            nonempty_lines = 0
            invalid_records = 0
            for line in reversed(lines):
                if line.strip():
                    nonempty_lines += 1
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    if line.strip():
                        invalid_records += 1
                    continue
                records.append(record)

            if nonempty_lines and invalid_records == nonempty_lines:
                unreadable += 1
                continue

            searched += 1
            for record in records:
                if not isinstance(record, dict) or record.get("type") not in {
                    "user",
                    "assistant",
                }:
                    continue
                if record.get("isMeta"):
                    continue
                text = _message_text(record)
                if not text or not all(word in text.lower() for word in query_words):
                    continue

                excerpt = _session_excerpt(text, query_words[0])
                formatted = f"[{path.stem[:8]} {_session_timestamp(record.get('timestamp'))} {record['type']}] {excerpt}"
                if excerpt_length + len(formatted) + (1 if excerpts else 0) > 4000:
                    excerpts.append("... (truncated)")
                    summary = _history_search_summary(
                        searched,
                        len(all_session_files),
                        too_large,
                        unreadable,
                        len(excerpts) - 1,
                    )
                    return "\n".join([summary, *excerpts])
                excerpts.append(formatted)
                excerpt_length += len(formatted) + (1 if len(excerpts) > 1 else 0)
                if len(excerpts) >= result_limit:
                    summary = _history_search_summary(
                        searched,
                        len(all_session_files),
                        too_large,
                        unreadable,
                        len(excerpts),
                    )
                    return "\n".join([summary, *excerpts])
    except Exception as exc:
        return f"Session history unavailable: {type(exc).__name__}"

    if not searched and all_session_files and unreadable:
        return "Session history unavailable: all session files were unreadable"
    if excerpts:
        summary = _history_search_summary(
            searched,
            len(all_session_files),
            too_large,
            unreadable,
            len(excerpts),
        )
        return "\n".join([summary, *excerpts])
    return f"{no_match} {_history_search_summary(searched, len(all_session_files), too_large, unreadable)}"


if __name__ == "__main__":
    mcp.run()
