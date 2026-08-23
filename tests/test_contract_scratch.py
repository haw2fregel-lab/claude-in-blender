"""外形契約: scratch は MCP の公開ツールだけで往復する（票F 契約4）。

Claude がやる手順をそのままなぞる——write_scratch の返事から path を読み、
その path を execute_file へ渡す。置き場所をテストが知っている必要はない
（差し替え口も無い）ので、実物の scratch へ書き、自分が作った分だけ片付ける。

Blender 側は実際に動いている bridge。MCP から Blender への運び役
（`server.bridge` の公開面 = send(command, params)）だけを、その bridge へ
向け直す。だから「書いたコードが本当に走って値が返る」ところまで通しで見える。
"""
import json
import re
from pathlib import Path

import pytest

from mcp_server import server


# 実物の scratch へ置く。他と衝突しない、このテスト専用の名前にする。
_NAME = "test_contract_scratch_roundtrip.py"
_WROTE_MESSAGE = re.compile(r"^Wrote (?P<path>.+) \(\d+ bytes\)$")


def _returned_path(message):
    """write_scratch の返事から path を読む。Claude が読むのと同じ一行。"""
    matched = _WROTE_MESSAGE.match(message.strip())
    assert matched, f"契約4: write_scratch の返事から path を読めない: {message!r}"
    return matched.group("path")


def _only_text(returned):
    assert len(returned) == 1, f"capture_after 無しの返りは1つ: {returned}"
    assert returned[0].type == "text"
    return returned[0].text


class _TcpTransport:
    """MCP から Blender への運び役。動いている bridge へそのまま流す。"""

    def __init__(self, bridge_tcp):
        self._bridge_tcp = bridge_tcp
        self.sent = []

    def send(self, command, params=None):
        self.sent.append((command, dict(params or {})))
        return self._bridge_tcp.send(command, params or {})


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


def test_write_scratch_then_execute_file_round_trips_through_the_public_tools(
    bridge_tcp, monkeypatch, scratch_files
):
    """契約4: write_scratch → execute_file の往復が、公開ツールだけで通る。"""
    transport = _TcpTransport(bridge_tcp)
    monkeypatch.setattr(server, "bridge", transport)
    source = "result = [6 * 7, __file__]\n"

    path = _returned_path(server.write_scratch(_NAME, source))
    scratch_files.append(path)
    returned = server.execute_file(path)

    assert json.loads(_only_text(returned)) == [42, path], (
        "契約4: 返ってきた path をそのまま渡せば、書いたコードが走って値が戻る"
    )
    command, params = transport.sent[-1]
    assert command == "execute_code"
    assert params["code"] == source, "契約4: 運ばれるのはファイルの中身そのもの"
    assert params["filename"] == path, (
        "契約4: どのファイルを走らせたかを渡す（traceback と __file__ がここに乗る）"
    )


def test_a_rewritten_scratch_file_runs_as_the_new_content(
    bridge_tcp, monkeypatch, scratch_files
):
    """契約4: 往復は繰り返せる。2周目は書き直した後の中身が走る。"""
    monkeypatch.setattr(server, "bridge", _TcpTransport(bridge_tcp))

    path = _returned_path(server.write_scratch(_NAME, "result = 'first run'\n"))
    scratch_files.append(path)
    assert _only_text(server.execute_file(path)) == "first run"

    rewritten = _returned_path(server.write_scratch(_NAME, "result = 'second run'\n"))

    assert rewritten == path, "契約4: 同じ名前なら同じ path が返る"
    assert _only_text(server.execute_file(path)) == "second run", (
        "契約4: execute_file が走らせるのは、その時点のファイルの中身"
    )


def test_execute_file_refuses_a_path_write_scratch_never_returned(
    tmp_path, monkeypatch
):
    """契約4 の失敗時: scratch の外の path は、Blender まで運ばずに断る。"""
    transport = _RecordingTransport()
    monkeypatch.setattr(server, "bridge", transport)
    outside = tmp_path / "outside.py"
    outside.write_text("result = 'nope'\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        server.execute_file(str(outside))

    assert transport.sent == [], (
        "契約4: 受け付けない path は、実行の手前で止める（Blender へ届かせない）"
    )
