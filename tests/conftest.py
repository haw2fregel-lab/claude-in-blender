import functools
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


class FakeOperator:
    """`bpy.types.Operator` の身代わり。

    MagicMock を基底にした `class Foo(bpy.types.Operator)` はクラスではなく mock を作る
    （メタクラスが MagicMock になる）。そのままだとオペレーターの invoke / execute へ
    テストから手が届かないので、影の中でここだけ本物のクラスにする。
    register されないので RNA の機能は持たず、`report` だけ実物に合わせて受ける。
    """

    def __init__(self, **fields):
        self.reports = []
        for name, value in fields.items():
            setattr(self, name, value)

    def report(self, level, message):
        self.reports.append((set(level), str(message)))


_bpy = sys.modules["bpy"]
if isinstance(_bpy, MagicMock):
    _bpy.types.Operator = FakeOperator

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


# --- WM プロパティの fake ---
# 実測（Blender 5.1.2, headless）: window_manager のカスタムプロパティは
#   保存では値が残る / .blend を開くと default に戻る / 登録自体はどちらでも生き残る。
# MagicMock はこの差を持てず、値の読み書きも吸ってしまうため、
# ファイル切替を見るテストだけこの fake へ差し替える。


@dataclass
class WmCall:
    name: str
    args: tuple
    kwargs: dict


class FakeWindowManager:
    """値を保持する window_manager。ファイルを開くと default へ戻る。

    - `claude_bridge_*` は登録済み（= default を与えた）名前だけ読める。未登録は
      Blender と同じく AttributeError。
    - それ以外の名前は WM の API とみなし、呼ばれた事実を `calls` へ記録する callable。
    - 書き込みは名前を問わず受けるが、default に無い名前はロードで消える
      （.blend を跨いで器として残るのは登録済みプロパティだけ、を模す）。
    """

    # UI の再描画は wm.windows を辿る。空で置いて、描画は空回りさせる。
    windows = ()

    def __init__(self, defaults):
        object.__setattr__(self, "_defaults", dict(defaults))
        object.__setattr__(self, "_values", dict(defaults))
        object.__setattr__(self, "calls", [])

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]
        if name.startswith("claude_bridge_"):
            raise AttributeError(name)
        return functools.partial(self._record, name)

    def __setattr__(self, name, value):
        object.__getattribute__(self, "_values")[name] = value

    def _record(self, name, *args, **kwargs):
        self.calls.append(WmCall(name=name, args=args, kwargs=kwargs))
        return {"RUNNING_MODAL"}

    def simulate_file_load(self):
        """.blend を開いた時: プロパティの登録は残り、値だけ default に戻る。"""
        values = object.__getattribute__(self, "_values")
        values.clear()
        values.update(object.__getattribute__(self, "_defaults"))

    def simulate_file_save(self):
        """保存した時: 何も戻らない。保存で誤検知しないことが設計の成立条件。"""


@pytest.fixture
def fake_window_manager(monkeypatch):
    """`bpy.context.window_manager` を値の残る fake へ差し替える factory。

    どのプロパティが既定値いくつで居るかは案件ごとに違うので、呼ぶ側が渡す。
    """
    import bpy

    def install(**defaults):
        window_manager = FakeWindowManager(defaults)
        monkeypatch.setattr(bpy.context, "window_manager", window_manager)
        return window_manager

    return install
