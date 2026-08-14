import json
import socket
import sys
import threading
import time
import types
import uuid
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = ROOT / "mcp_server"
if str(MCP_SERVER) not in sys.path:
    sys.path.insert(0, str(MCP_SERVER))

for module_name in ("bpy", "bmesh", "bpy_extras", "mathutils", "blf"):
    sys.modules.setdefault(module_name, MagicMock(name=module_name))

bridge_package = types.ModuleType("claude_bridge")
bridge_package.__path__ = [str(ROOT / "claude_bridge")]
sys.modules["claude_bridge"] = bridge_package


def send_command(server, port, command, params, timeout=10, request_id=None):
    token = Path(server._TOKEN_FILE).read_text(encoding="utf-8").strip()
    payload = {
        "token": token,
        "command": command,
        "params": params,
        "request_id": request_id or uuid.uuid4().hex,
    }
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        response = b""
        while b"\n" not in response:
            chunk = connection.recv(8192)
            if not chunk:
                break
            response += chunk
    return json.loads(response.split(b"\n", 1)[0])


@dataclass
class BridgeTcp:
    server: object
    port: int
    pump_enabled: threading.Event

    def send(self, command, params, timeout=10, request_id=None, auto_ack=True):
        response = send_command(
            self.server,
            self.port,
            command,
            params,
            timeout,
            request_id=request_id,
        )
        execute_ack = command == "execute_code" and response.get("ack_required") is True
        status_ack = (
            command == "get_request_status"
            and response.get("ok") is True
            and response["data"].get("status") in {"succeeded", "failed"}
        )
        if auto_ack and (execute_ack or status_ack):
            operation_id = (
                response["data"]["request_id"]
                if status_ack
                else response["request_id"]
            )
            send_command(
                self.server,
                self.port,
                "ack_request_result",
                {"operation_id": operation_id},
                timeout,
            )
        return response


@pytest.fixture
def bridge_tcp(tmp_path):
    from claude_bridge import bridge_server

    server = bridge_server
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    original_timeout = server._HOLDER_TIMEOUT_S
    server._TMP_DIR = str(tmp_path / "bridge-temp")
    server._TOKEN_FILE = str(tmp_path / "bridge-temp" / "blender-session-token")
    server.start_server(port=port)

    token_file = Path(server._TOKEN_FILE)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if token_file.exists() and server.is_running():
            break
        time.sleep(0.01)
    if not token_file.exists() or not server.is_running():
        server.stop_server()
        pytest.fail("bridge server did not publish its token and become ready")

    stop_pump = threading.Event()
    pump_enabled = threading.Event()
    pump_enabled.set()

    def pump():
        while not stop_pump.is_set():
            if pump_enabled.is_set():
                server._process_pending()
            time.sleep(0.01)

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()
    fixture = BridgeTcp(server=server, port=port, pump_enabled=pump_enabled)
    try:
        yield fixture
    finally:
        stop_pump.set()
        server.stop_server()
        pump_thread.join(timeout=2)
        server._HOLDER_TIMEOUT_S = original_timeout
