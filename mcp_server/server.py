import base64
import json
import os
import tempfile

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from bridge import BlenderBridge

_SCRATCH_DIR = os.path.join(tempfile.gettempdir(), "claude-in-blender", "scratch")

mcp = FastMCP(
    "claude-in-blender",
    instructions=(
        "Blender 3D scene manipulation.\n\n"
        "Combine multiple operations into one execute_code call"
        " to save tokens. For example, creating an object,"
        " setting its material, and adjusting lighting should"
        " be one script, not three separate calls.\n\n"
        "For anything longer than a few lines, write the script"
        f" to a .py file under {_SCRATCH_DIR}"
        " and run it with execute_file. Fixing the file with a"
        " small edit and re-running is much cheaper than"
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
        " conditions."
    ),
)
bridge = BlenderBridge()


def _fmt(result: dict) -> str:
    """Format bridge result for AI — strip envelope, compact JSON."""
    return json.dumps(result["data"], ensure_ascii=False)


@mcp.tool()
def get_bridge_status() -> str:
    """Check if Blender is connected and the bridge addon is running."""
    result = bridge.send("ping")
    if not result.get("ok"):
        raise RuntimeError(result.get("error", {}).get("message", "Unknown error"))
    return _fmt(result)


@mcp.tool()
def get_viewport_screenshot() -> list:
    """Capture a screenshot of Blender's 3D viewport."""
    result = bridge.send("get_viewport_screenshot")
    if not result.get("ok"):
        raise RuntimeError(result.get("error", {}).get("message", "Unknown error"))
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
        raise RuntimeError(result.get("error", {}).get("message", "Unknown error"))
    return _fmt(result)


def _run_code(code: str, capture_after: bool, filename: str = "<execute_code>") -> list:
    # 構文エラーは Blender まで運ばず手前で弾く（行番号はファイルの実行と一致する）
    try:
        compile(code, filename, "exec")
    except SyntaxError as e:
        raise RuntimeError(
            f"SyntaxError: {e.msg} ({filename}, line {e.lineno}): {(e.text or '').strip()}"
        )
    result = bridge.send("execute_code", {"code": code})
    if not result.get("ok"):
        raise RuntimeError(result.get("error", {}).get("message", "Unknown error"))

    def _fmt_exec(res: dict) -> str:
        val = res["data"]["result"]
        if val is None:
            return "OK"
        if isinstance(val, str):
            return val
        return json.dumps(val, ensure_ascii=False)

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

    For anything longer than a few lines, prefer execute_file.

    Args:
        code: Python code to execute in Blender's context
        capture_after: If True, capture a viewport screenshot after execution
    """
    return _run_code(code, capture_after)


@mcp.tool()
def execute_file(path: str, capture_after: bool = False) -> list:
    """Execute a Python file inside Blender (bpy available).
    Set a variable called 'result' in the file to return data back.

    Prefer this over execute_code for longer scripts: write the file,
    run it, fix it with small edits, re-run — much cheaper than
    resending the whole script. Syntax errors are caught locally
    (with file line numbers) before reaching Blender.

    Args:
        path: Absolute path to a Python (.py) file
        capture_after: If True, capture a viewport screenshot after execution
    """
    try:
        with open(path, encoding="utf-8") as f:
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
        raise RuntimeError(result.get("error", {}).get("message", "Unknown error"))
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
        raise RuntimeError(result.get("error", {}).get("message", "Unknown error"))
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
        raise RuntimeError(result.get("error", {}).get("message", "Unknown error"))
    return _fmt(result)


if __name__ == "__main__":
    mcp.run()
