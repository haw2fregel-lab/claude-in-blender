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
_CLAUDE_CONFIG_DIR = Path(
    os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
)
_PROJECTS_ROOT = _CLAUDE_CONFIG_DIR / "projects"

mcp = FastMCP(
    "claude-in-blender",
    instructions=(
        "Blender 3D scene manipulation.\n\n"
        "Combine multiple operations into one execute_code call"
        " to save tokens. For example, creating an object,"
        " setting its material, and adjusting lighting should"
        " be one script, not three separate calls.\n\n"
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
        " If it fails, the error comes back — just fix and"
        " retry. (Detailed result values for verification are"
        " still fine.)\n\n"
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
    ),
)
bridge = BlenderBridge()


def _fmt(result: dict) -> str:
    """Format bridge result for AI — strip envelope, compact JSON."""
    return json.dumps(result["data"], ensure_ascii=False)


def _bridge_error(result: dict) -> RuntimeError:
    error = result.get("error") or {}
    msg = error.get("message", "Unknown error")
    tb = error.get("traceback")
    if result.get("status") == "outcome_unknown" and result.get("request_id"):
        request_id = result["request_id"]
        guidance = f"Call get_request_status({request_id!r}) before another execute."
        if guidance not in msg:
            msg = f"{msg}\n{guidance}"
    return RuntimeError(f"{msg}\n{tb}" if tb else msg)


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


def _scratch_file_error(path: str) -> RuntimeError:
    return RuntimeError(
        f"Only .py files under scratch ({_SCRATCH_DIR}) can be executed: {path}"
    )


def _reject_scratch_symlink(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(
            f"Cannot write scratch file: refusing to overwrite symlink: {path}"
        )


def _write_scratch_file(path: Path, content: str) -> None:
    temp_path = None
    try:
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
        scratch_file = Path(path).resolve(strict=True)
        scratch_file.relative_to(scratch_dir)
        if scratch_file.suffix != ".py" or not scratch_file.is_file():
            raise ValueError
        if scratch_file.stat().st_size > 1024 * 1024:
            raise ValueError
    except (OSError, TypeError, ValueError):
        raise _scratch_file_error(path) from None
    return scratch_file


def _project_slug() -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path.cwd()))


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
    os.makedirs(_SCRATCH_DIR, exist_ok=True)
    path = Path(_SCRATCH_DIR) / name
    _write_scratch_file(path, content)
    return f"Wrote {path.resolve()} ({len(content.encode('utf-8'))} bytes)"


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


def _run_code(code: str, capture_after: bool, filename: str = "<execute_code>") -> list:
    # 構文エラーは Blender まで運ばず手前で弾く（行番号はファイルの実行と一致する）
    try:
        compile(code, filename, "exec")
    except SyntaxError as e:
        raise RuntimeError(
            f"SyntaxError: {e.msg} ({filename}, line {e.lineno}): {(e.text or '').strip()}"
        )
    result = bridge.send("execute_code", {"code": code, "filename": filename})
    if not result.get("ok"):
        raise _bridge_error(result)

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

    text_content = TextContent(type="text", text=_fmt_exec(result))
    if not capture_after:
        return [text_content]

    ss_result = bridge.send("get_viewport_screenshot")
    if not ss_result.get("ok"):
        return [text_content]

    data = ss_result["data"]
    path = data["screenshot_path"]
    try:
        with open(path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return [text_content]
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

    Prefer this over execute_code for longer scripts: use write_scratch,
    run the returned path, fix it with edit_scratch, re-run — much cheaper than
    resending the whole script. Syntax errors are caught locally
    (with file line numbers) before reaching Blender.

    Args:
        path: Absolute path to a Python (.py) file
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
            return no_match

        session_files = []
        for path in project_dir.glob("*.jsonl"):
            try:
                stat_result = path.stat()
            except OSError:
                continue
            # 10MB 超は候補に入れない（「新しい10件」の枠を skip で食わないよう、読む前に絞る）
            if stat_result.st_size > 10 * 1024 * 1024:
                continue
            session_files.append((stat_result.st_mtime, path))

        excerpts = []
        excerpt_length = 0
        for _, path in sorted(session_files, key=lambda item: item[0], reverse=True)[
            :10
        ]:
            try:
                with path.open(encoding="utf-8") as f:
                    lines = f.readlines()
            except (OSError, UnicodeError):
                continue

            for line in reversed(lines):
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
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
                    return "\n".join(excerpts)
                excerpts.append(formatted)
                excerpt_length += len(formatted) + (1 if len(excerpts) > 1 else 0)
                if len(excerpts) >= result_limit:
                    return "\n".join(excerpts)
    except Exception:
        return no_match

    return "\n".join(excerpts) if excerpts else no_match


if __name__ == "__main__":
    mcp.run()
