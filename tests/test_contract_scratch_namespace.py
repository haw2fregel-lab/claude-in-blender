"""外形契約: scratch は MCP server プロセスごとの namespace を持つ（レビュー#4）。

desktop session と panel fork が同じ名前の scratch を書き合っても、実行対象を
取り違えない——そのための置き場の分離だけを見る。

- `_SCRATCH_DIR` は `<temp>/claude-in-blender/scratch/` の配下にあり、
  同時に生きる 2 プロセスで交わらない（契約1・外形5）
- 交わらない結果として、一方の write が他方の既存ファイルの中身を変えない（外形5）
- 配下チェックは namespace 基準。隣のプロセスのファイルは execute_file が断る（契約3）

セグメントの名前（pid か uuid か、時刻を含むか）は決めどころなので見ない。
何段掘るかも同じ——`scratch/` の配下に居ることだけを確かめる。
掃除は契約に無い（契約4）ので、消えることも消えないことも期待せず、
このテストが作った分だけ片付ける。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from mcp_server import server


ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = ROOT / "mcp_server"

# 契約1 の親 path。ここから下に、プロセスごとのセグメントが増える。
SCRATCH_ROOT = Path(tempfile.gettempdir()) / "claude-in-blender" / "scratch"

# 実物の scratch へ置くので、既存の契約テストと同じく、人が使わない名前にする。
# 票の `scene.py` の役はこれ——2 プロセスが「同じ名前」で書く側。
_NAME = "test_contract_scratch_namespace.py"
# 同一プロセス内だけを見るテストが使う名前。壊れている時に、どちら側の観測が
# 落ちたのかを混ぜないため、プロセスを跨ぐ側と分けておく。
_LOCAL_NAME = "test_contract_scratch_namespace_local.py"

_WROTE_MESSAGE = re.compile(r"^Wrote (?P<path>.+) \(\d+ bytes\)$")
# 子プロセスの報告行の目印。import 時の警告等が混ざっても、この行だけ拾う。
_REPORT = "<<scratch-namespace-report>>"


def _returned_path(message):
    """write_scratch の返事から path を読む。Claude が読むのと同じ一行。"""
    matched = _WROTE_MESSAGE.match(message.strip())
    assert matched, f"契約2: write_scratch の返事から path を読めない: {message!r}"
    return matched.group("path")


def _comparable(path):
    """path 同士を比べる形へ均す。

    Windows では同じ場所が短縮名（HAW2F~1）や大文字小文字違いで綴られる。
    綴りの違いを「別の namespace」と読み違えないよう、比較の前にここを通す。
    """
    return Path(os.path.normcase(os.path.realpath(str(path))))


def _under_scratch_root(path):
    return _comparable(SCRATCH_ROOT) in _comparable(path).parents


class _RecordingTransport:
    """届いたかどうかだけを見る運び役。Blender へは行かない。"""

    def __init__(self):
        self.sent = []

    def send(self, command, params=None):
        self.sent.append((command, dict(params or {})))
        return {"ok": True, "data": {"result": None}, "elapsed_ms": 0}


@pytest.fixture
def scratch_files():
    """テストが作った scratch ファイルだけを後で片付ける。

    どこへ置くかを決めるのは write_scratch。テストはその答えを受け取るだけなので、
    掃除も「返ってきた path」に対して行う。
    """
    written = []
    yield written
    for path in written:
        try:
            Path(path).unlink()
        except OSError:
            pass


# --- 同一プロセス内（このテストプロセス自身が 1 つの server） ---


def test_scratch_dir_is_a_namespace_under_the_shared_scratch_root():
    """契約1: `_SCRATCH_DIR` は共有の scratch root そのものではなく、その配下。"""
    assert _under_scratch_root(server._SCRATCH_DIR), (
        f"契約1: {SCRATCH_ROOT} の配下に居ない: {server._SCRATCH_DIR}"
    )


def test_write_scratch_returns_a_path_inside_this_process_namespace(scratch_files):
    """契約2: 返り値の形は従来のまま、指す先がこのプロセスの namespace になる。"""
    path = _returned_path(server.write_scratch(_LOCAL_NAME, "value = 1\n"))
    scratch_files.append(path)

    assert _comparable(Path(path).parent) == _comparable(server._SCRATCH_DIR), (
        f"契約2: write_scratch が返す path は namespace の中: {path}"
    )
    assert Path(path).name == _LOCAL_NAME, "契約2: 渡した名前のまま置く"


def test_edit_scratch_reaches_the_file_write_scratch_returned(scratch_files):
    """契約2: 名前から置き場を引く経路は write と edit で同じ（同一プロセス）。"""
    path = _returned_path(server.write_scratch(_LOCAL_NAME, "value = 1\n"))
    scratch_files.append(path)

    updated = server.edit_scratch(_LOCAL_NAME, "value = 1", "value = 2")

    assert updated.startswith(f"Updated {path} "), (
        f"契約2: edit も write が返したのと同じ path を報告する: {updated!r}"
    )
    assert Path(path).read_text(encoding="utf-8") == "value = 2\n"


# --- 別プロセス（本物の server module を 2 つ、同時に生かして見る） ---

# 子プロセスの中身。server module を開いて `_SCRATCH_DIR` を報告し、親の合図で
# 同じ名前の scratch を書く。合図を待つ間、このプロセスは生きたまま——契約1 の
# 「同時に生きる 2 プロセス」を、本当に同時に生きた状態で観測するため。
# 親は encoding="utf-8" で読むので、子も環境の既定 encoding に任せず bytes で通す。
_CHILD_SOURCE = r'''
import json
import sys
from unittest.mock import MagicMock

# conftest と同じ影のモジュール。Blender の外で server module を開くため。
for module_name in ("bpy", "bmesh", "bpy_extras", "mathutils", "blf"):
    sys.modules.setdefault(module_name, MagicMock(name=module_name))

from mcp_server import server

marker, name, tag = sys.argv[1:4]


def report(**fields):
    sys.stdout.buffer.write((marker + json.dumps(fields) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


report(scratch_dir=str(server._SCRATCH_DIR))
sys.stdin.readline()  # 親の合図待ち。ここで生きている
content = "result = " + json.dumps(tag) + "\n"
report(write_message=server.write_scratch(name, content), content=content)
sys.stdin.readline()  # 親が stdin を閉じたら終わる
'''


@dataclass
class ServerProcessReport:
    """別プロセスの server が報告したもの。中は全部 path と中身の文字列。"""

    scratch_dir: str
    path: str
    content: str


@dataclass
class TwoServerProcesses:
    a: ServerProcessReport
    b: ServerProcessReport
    alive_together: bool


def _start_server_process(tag, name):
    """別プロセスで server module を開く。

    import の口は conftest と同じ 2 つ（リポ直下と mcp_server/）を通しておく。
    子は実物の temp を使う——契約1 の親 path を、そのまま観測したいので。
    """
    env = os.environ.copy()
    entries = [str(ROOT), str(MCP_SERVER)]
    if env.get("PYTHONPATH"):
        entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_SOURCE, _REPORT, name, tag],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


def _next_report(process, what):
    line = process.stdout.readline()
    if not line.startswith(_REPORT):
        process.kill()
        rest, stderr = process.communicate(timeout=10)
        pytest.fail(
            f"別プロセスの server が{what}を報告しない: {line!r}\n{rest}\n{stderr}"
        )
    return json.loads(line[len(_REPORT) :])


def _tell(process):
    process.stdin.write("go\n")
    process.stdin.flush()


def _finish(process):
    _, stderr = process.communicate(timeout=15)  # stdin を閉じて終わらせる
    assert process.returncode == 0, stderr


@pytest.fixture(scope="module")
def two_server_processes():
    """同時に生きる 2 つの server プロセスに、同じ名前の scratch を書かせる。

    A が書き終えてから B に書かせる。だから「A の path の中身が A のまま」は、
    「後から来た B の write が A へ届いていない」と読める。
    起動は 1 度だけ（module scope）——見るのは書いた後の跡なので、使い回せる。
    """
    processes = [
        _start_server_process("process A", _NAME),
        _start_server_process("process B", _NAME),
    ]
    try:
        a_process, b_process = processes
        directories = [
            _next_report(a_process, "_SCRATCH_DIR")["scratch_dir"],
            _next_report(b_process, "_SCRATCH_DIR")["scratch_dir"],
        ]
        # 両方の _SCRATCH_DIR が出揃った今、2 つとも生きている。
        alive_together = a_process.poll() is None and b_process.poll() is None

        writes = []
        for process in (a_process, b_process):  # A の write を先に終わらせる
            _tell(process)
            writes.append(_next_report(process, "write_scratch の返り"))

        reports = TwoServerProcesses(
            a=ServerProcessReport(
                scratch_dir=directories[0],
                path=_returned_path(writes[0]["write_message"]),
                content=writes[0]["content"],
            ),
            b=ServerProcessReport(
                scratch_dir=directories[1],
                path=_returned_path(writes[1]["write_message"]),
                content=writes[1]["content"],
            ),
            alive_together=alive_together,
        )
        for process in processes:
            _finish(process)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)

    yield reports

    for report in (reports.a, reports.b):
        try:
            Path(report.path).unlink()
        except OSError:
            pass
        # 空になった namespace だけ畳む。共有 root は掃除の対象外（契約4）。
        namespace = Path(report.path).parent
        if _under_scratch_root(namespace):
            try:
                namespace.rmdir()
            except OSError:
                pass


def test_two_live_server_processes_do_not_share_a_scratch_dir(two_server_processes):
    """契約1: 同時に生きる 2 プロセスの `_SCRATCH_DIR` は交わらない。"""
    assert two_server_processes.alive_together, (
        "契約1: 2 つの server が同時に生きている状態で観測する"
    )
    for role, report in (("A", two_server_processes.a), ("B", two_server_processes.b)):
        assert _under_scratch_root(report.scratch_dir), (
            f"契約1: {role} の _SCRATCH_DIR が {SCRATCH_ROOT} の配下に居ない: "
            f"{report.scratch_dir}"
        )
    assert _comparable(two_server_processes.a.scratch_dir) != _comparable(
        two_server_processes.b.scratch_dir
    ), (
        "契約1: プロセスごとに別の namespace: "
        f"{two_server_processes.a.scratch_dir!r} / {two_server_processes.b.scratch_dir!r}"
    )


def test_a_write_in_one_process_leaves_the_other_processes_file_alone(
    two_server_processes,
):
    """外形5: A が得た path の中身は、後から来た B の同名 write で変わらない。"""
    a, b = two_server_processes.a, two_server_processes.b
    assert a.content != b.content, "書いた中身が同じでは「変わらない」を見たことにならない"
    assert Path(a.path).name == Path(b.path).name == _NAME, (
        f"外形5: 同じ名前で書いた前提が崩れている: {a.path} / {b.path}"
    )

    assert _comparable(a.path) != _comparable(b.path), (
        f"外形5: 同じ名前でも path が交わらない: {a.path} / {b.path}"
    )
    assert Path(a.path).read_text(encoding="utf-8") == a.content, (
        f"外形5: B の write の後も、A の path は A が書いた中身のまま: {a.path}"
    )
    assert Path(b.path).read_text(encoding="utf-8") == b.content, (
        f"外形5: B の write は B の namespace に届いている: {b.path}"
    )


def test_execute_file_refuses_a_file_from_another_process_namespace(
    two_server_processes, monkeypatch
):
    """契約3: 配下チェックは namespace 基準。隣のプロセスの path は運ぶ手前で断る。"""
    transport = _RecordingTransport()
    monkeypatch.setattr(server, "bridge", transport)
    other = two_server_processes.a.path

    with pytest.raises(RuntimeError):
        server.execute_file(other)

    assert transport.sent == [], (
        "契約3: 自分の namespace の外は、実行の手前で止める（Blender へ届かせない）"
    )
    assert Path(other).read_text(encoding="utf-8") == two_server_processes.a.content, (
        "契約3: 断っただけ。相手のファイルには触らない"
    )
