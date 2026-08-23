import functools
import json
import os
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


# --- 外形契約テストの土台（票F） ---
# ここから下は追加分で、上の fixture には手を入れていない。
# 配るのは「private 名を通らない観測点」だけ——PATH に置く claude の身代わり、
# Blender の代わりに回す timer、Claude Code が登録する bridge session ファイル、
# パネルが WindowManager へ載せるプロパティの既定値。
# 実装の内側（_run_claude / _result_box / _WM_PROPS 等）へは触らせない。


# claude の身代わり本体。受け取った argv と stdin をそのまま jsonl へ積み、
# 決められた応答を返すだけ。親は encoding="utf-8" で読み書きするので、子の側も
# 環境の既定 encoding に任せず bytes で通す（cp932 の環境で化けさせない）。
_FAKE_CLAUDE_SOURCE = r'''
import json
import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
reply = json.loads((here / "reply.json").read_text(encoding="utf-8"))
try:
    stdin_text = sys.stdin.buffer.read().decode("utf-8", "replace")
except Exception:
    stdin_text = ""
record = {"argv": sys.argv[1:], "stdin": stdin_text, "cwd": os.getcwd()}
with (here / "calls.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
sys.stdout.buffer.write(reply["stdout"].encode("utf-8"))
sys.stdout.buffer.flush()
sys.exit(reply["returncode"])
'''


@dataclass
class FakeClaude:
    """PATH に置いた claude の身代わり。起動のたびに argv と stdin が1件積まれる。"""

    directory: Path

    def calls(self):
        """記録された起動を古い順に返す。まだ起動されていなければ空。"""
        try:
            text = (self.directory / "calls.jsonl").read_text(encoding="utf-8")
        except OSError:
            return []
        records = []
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 書き込み途中の行は次の観測で拾う
        return records


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """PATH の先頭へ claude の身代わりを置く factory。

    パネルは `shutil.which("claude")` で実体を探すので、PATH を差し替えれば
    送信先だけが身代わりに変わる（送信の組み立ては実装のまま通る）。
    何を返す claude なのかは案件ごとに違うので、stdout は呼ぶ側が渡す。
    """

    def install(stdout="", returncode=0):
        directory = tmp_path / "fake-claude-bin"
        directory.mkdir(parents=True, exist_ok=True)
        script = directory / "fake_claude.py"
        script.write_text(_FAKE_CLAUDE_SOURCE, encoding="utf-8")
        (directory / "reply.json").write_text(
            json.dumps({"stdout": stdout, "returncode": returncode}),
            encoding="utf-8",
        )
        # 起動の入口。Windows は .cmd、それ以外は #! 付きの実行ファイル。
        # 中身はどちらも「この Python でスクリプトを走らせる」だけ（path は ASCII 前提）。
        # 改行は write_text の既定変換に任せる（Windows では CRLF になる）。
        if os.name == "nt":
            launcher = directory / "claude.cmd"
            launcher.write_text(
                f'@echo off\n"{sys.executable}" "{script}" %*\n',
                encoding="utf-8",
            )
        else:
            launcher = directory / "claude"
            launcher.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                encoding="utf-8",
            )
            launcher.chmod(0o755)
        monkeypatch.setenv(
            "PATH", str(directory) + os.pathsep + os.environ.get("PATH", "")
        )
        return FakeClaude(directory=directory)

    return install


@dataclass
class BlenderTimers:
    """`bpy.app.timers.register` の受け皿。Blender の代わりにテストが回す。

    Blender は登録された callback を繰り返し呼び、None が返ったところで止める。
    ここでも同じ約束で回す——「結果を渡し終えた」の合図は None。
    """

    registered: list

    def mark(self):
        """今までの登録数。ここから先に登録された分だけを見るための目印。"""
        return len(self.registered)

    def since(self, mark):
        return self.registered[mark:]

    def run(self, callback, timeout=30):
        """callback が None を返す（仕事を終える）まで回す。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if callback() is None:
                return True
            time.sleep(0.05)
        return False

    def settle(self, mark, timeout=30):
        """mark 以降に登録された timer を、Blender の代わりに終わりまで回す。"""
        pending = self.since(mark)
        assert len(pending) == 1, f"回すべき timer が1つではない: {pending}"
        assert self.run(pending[0], timeout=timeout), (
            f"timer が {timeout}s 以内に結果を渡さなかった"
        )


@pytest.fixture
def blender_timers(monkeypatch):
    """timer の登録を捕まえる。Blender が居ない場所で、Blender の役を代われるように。"""
    import bpy

    timers = BlenderTimers(registered=[])

    def register(callback, first_interval=0.0, persistent=False):
        timers.registered.append(callback)

    monkeypatch.setattr(bpy.app.timers, "register", register)
    return timers


@dataclass
class BridgeSessionFile:
    """Claude Code が登録する bridge session ファイル（temp に置いた実物と同じ形）。"""

    path: Path
    project: Path

    @property
    def cwd(self):
        """登録される cwd。tools/bridge_register.py と同じく posix 区切りで書く。"""
        return str(self.project).replace("\\", "/")

    def write(self, **fields):
        payload = {"cwd": self.cwd}
        payload.update(fields)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return self.path

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))


@pytest.fixture
def bridge_session_file(tmp_path, monkeypatch):
    """bridge session ファイルを temp へ寄せる。ユーザー本物の登録を触らせない。

    置き場の付け替えは CLAUDE_CONFIG_DIR（Claude Code 自身の環境変数）で行う。
    cwd には .mcp.json のある実ディレクトリが要るので、ここで一緒に用意する。
    """
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / ".mcp.json").write_text("{}\n", encoding="utf-8")
    return BridgeSessionFile(
        path=config_dir / "blender-bridge-session.json", project=project
    )


# パネルが register() で WindowManager へ載せるプロパティ（= 外から見える面）の既定値。
# 実物の Blender では器が全部揃っているのが普通の状態なので、外形テストはここから始める。
# プロパティが増えたらこの表に足す（名前は RNA 名で、実装の内側の並びは見ない）。
_PANEL_WM_DEFAULTS = {
    "claude_bridge_prompt": "",
    "claude_bridge_status": "IDLE",
    "claude_bridge_reply": "",
    "claude_bridge_usage": "",
    "claude_bridge_collapsed": False,
    "claude_bridge_generation": 0,
    "claude_bridge_ctx_selection": False,
    "claude_bridge_ctx_scene": False,
    "claude_bridge_ctx_doc": False,
    "claude_bridge_ctx_screenshot": False,
}


@pytest.fixture
def panel_window_manager(fake_window_manager):
    """パネルのプロパティが登録済みの window_manager を返す factory。"""

    def install(**overrides):
        return fake_window_manager(**{**_PANEL_WM_DEFAULTS, **overrides})

    return install
