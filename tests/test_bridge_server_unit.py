import os
import re
import socket
import threading
import time

import bpy
import pytest

from claude_bridge import bridge_server


# エラー応答の敷居（作業票の契約: message + traceback の合計 2048 bytes）。
MAX_ERROR_BYTES = 2048
ENVELOPE_METADATA_KEYS = ("request_id", "ack_required", "status", "blocker_request_id")
# message 側にだけ巨大文を作る。code 自体は短いので、ログ検査が code の写しで通ることはない。
OVERSIZED_ERROR_CODE = "raise RuntimeError('HUGE' + 'HEAD' + 'x' * 100000 + 'TAIL' + 'MARK')"
OVERSIZED_ERROR_BYTES = 100000
LOG_NOTICE_RE = re.compile(
    r" \.\.\. \(truncated; full error in claude_bridge_log, "
    r"original (?P<original>\d{1,3}(?:,\d{3})*) bytes\)$"
)


def _error_bytes(error):
    return sum(len((error.get(key) or "").encode("utf-8")) for key in ("message", "traceback"))


def _envelope_metadata(response):
    return {key: response[key] for key in ENVELOPE_METADATA_KEYS if key in response}


def _bridge_log_writes():
    written = []
    for call in bpy.data.texts.mock_calls:
        written.extend(argument for argument in call.args if isinstance(argument, str))
        written.extend(value for value in call.kwargs.values() if isinstance(value, str))
    return "".join(written)


def test_response_envelopes_have_the_expected_shape():
    ok = bridge_server._ok({"result": 1}, start=0)
    assert ok["ok"] is True
    assert ok["data"] == {"result": 1}
    assert isinstance(ok["elapsed_ms"], int)

    error = bridge_server._err("failed", tb="trace", start=0)
    assert error["ok"] is False
    assert error["error"] == {"message": "failed", "traceback": "trace"}
    assert isinstance(error["elapsed_ms"], int)


def test_large_successful_response_keeps_result_key_when_truncated(bridge_tcp):
    response = bridge_tcp.send(
        "execute_code",
        {"code": f"result = 'x' * {bridge_tcp.server._MAX_RESPONSE_BYTES + 1024}"},
    )

    assert response["ok"] is True
    assert response["data"]["output_truncated"] is True
    assert "result" in response["data"]
    assert response["data"]["original_bytes"] > bridge_tcp.server._MAX_RESPONSE_BYTES


def test_large_successful_response_keeps_envelope_metadata_when_truncated(bridge_tcp):
    small_id = "5a" * 16
    truncated_id = "5b" * 16

    small = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'small'"},
        request_id=small_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": small_id}, auto_ack=False)
    truncated = bridge_tcp.send(
        "execute_code",
        {"code": f"result = 'x' * {bridge_tcp.server._MAX_RESPONSE_BYTES + 1024}"},
        request_id=truncated_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": truncated_id}, auto_ack=False)

    assert truncated["ok"] is True
    assert truncated["data"]["output_truncated"] is True
    assert _envelope_metadata(truncated) == dict(
        _envelope_metadata(small), request_id=truncated_id
    )
    # 切り詰め形式は従来のまま——data 全体を json.dumps した文字列の頭が入る
    # （JSON の包み紙が混ざる形。是正するなら封筒契約の変更で、このテストが対象）。
    # 警告行を足すのは MCP 側の仕事で、封筒には混ぜない。
    assert truncated["data"]["result"].startswith('{"result": "x')
    assert "[output truncated" not in truncated["data"]["result"]
    assert len(truncated["data"]["result"]) < truncated["data"]["original_bytes"]


def test_error_within_the_size_limit_is_returned_unchanged(bridge_tcp):
    operation_id = "6a" * 16

    response = bridge_tcp.send(
        "execute_code",
        {"code": "raise RuntimeError('boom')"},
        request_id=operation_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": operation_id}, auto_ack=False)

    assert response["ok"] is False
    assert set(response["error"]) == {"message", "traceback"}
    assert "RuntimeError: boom" in response["error"]["message"]
    assert "RuntimeError" in response["error"]["traceback"]
    assert _error_bytes(response["error"]) <= MAX_ERROR_BYTES
    assert "truncated" not in response["error"]["message"]
    assert "claude_bridge_log" not in response["error"]["message"]
    assert response["request_id"] == operation_id


def test_oversized_error_is_capped_and_points_at_the_bridge_log(bridge_tcp):
    operation_id = "6b" * 16

    response = bridge_tcp.send(
        "execute_code",
        {"code": OVERSIZED_ERROR_CODE},
        request_id=operation_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": operation_id}, auto_ack=False)

    assert response["ok"] is False
    assert _error_bytes(response["error"]) <= MAX_ERROR_BYTES
    message = response["error"]["message"]
    # エラー型と message の頭は残る。
    assert "RuntimeError" in message
    assert "HUGEHEAD" in message
    notice = LOG_NOTICE_RE.search(message)
    assert notice is not None, message[-200:]
    assert int(notice.group("original").replace(",", "")) >= OVERSIZED_ERROR_BYTES


def test_oversized_error_keeps_the_full_text_in_the_bridge_log(bridge_tcp):
    operation_id = "6c" * 16
    bpy.data.texts.reset_mock()

    response = bridge_tcp.send(
        "execute_code",
        {"code": OVERSIZED_ERROR_CODE},
        request_id=operation_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": operation_id}, auto_ack=False)

    assert response["ok"] is False
    logged = _bridge_log_writes()
    assert "claude_bridge_log" in logged
    assert "HUGEHEAD" in logged
    assert "TAILMARK" in logged
    assert len(logged) >= OVERSIZED_ERROR_BYTES


def test_oversized_error_keeps_the_metadata_a_small_error_carries(bridge_tcp):
    small_id = "6d" * 16
    oversized_id = "6e" * 16

    small = bridge_tcp.send(
        "execute_code",
        {"code": "raise RuntimeError('boom')"},
        request_id=small_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": small_id}, auto_ack=False)
    oversized = bridge_tcp.send(
        "execute_code",
        {"code": OVERSIZED_ERROR_CODE},
        request_id=oversized_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": oversized_id}, auto_ack=False)

    assert small["ok"] is False
    assert oversized["ok"] is False
    assert oversized["request_id"] == oversized_id
    assert _envelope_metadata(oversized) == dict(
        _envelope_metadata(small), request_id=oversized_id
    )


def test_next_mutation_runs_after_acking_an_oversized_error(bridge_tcp):
    oversized_id = "6f" * 16
    recovered_id = "70" * 16

    oversized = bridge_tcp.send(
        "execute_code",
        {"code": OVERSIZED_ERROR_CODE},
        request_id=oversized_id,
        auto_ack=False,
    )
    assert oversized["ok"] is False
    assert oversized["request_id"] == oversized_id

    acknowledged = bridge_tcp.send(
        "ack_request_result",
        {"operation_id": oversized["request_id"]},
        auto_ack=False,
    )
    assert acknowledged["ok"] is True

    recovered = bridge_tcp.send(
        "execute_code",
        {"code": "result = 'after-oversized-error'"},
        request_id=recovered_id,
        auto_ack=False,
    )
    bridge_tcp.send("ack_request_result", {"operation_id": recovered_id}, auto_ack=False)

    assert recovered["ok"] is True
    assert recovered["data"]["result"] == "after-oversized-error"


def test_token_publish_failure_stops_the_server(tmp_path, monkeypatch):
    token_file = tmp_path / "bridge-temp" / "blender-session-token"
    monkeypatch.setattr(bridge_server, "_TMP_DIR", str(token_file.parent))
    monkeypatch.setattr(bridge_server, "_TOKEN_FILE", str(token_file))
    real_open = os.open
    publish_attempted = threading.Event()

    def fail_token_publish(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(token_file):
            publish_attempted.set()
            raise OSError("publish denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_token_publish)

    bridge_server.start_server(port=0)
    assert publish_attempted.wait(timeout=2)
    bridge_server._server_thread.join(timeout=2)

    try:
        assert bridge_server._server_thread.is_alive() is False
        assert bridge_server._running is False
        assert bridge_server.is_running() is False
    finally:
        bridge_server.stop_server()


def test_running_status_requires_listener_readiness(monkeypatch):
    monkeypatch.setattr(bridge_server, "_running", True)
    monkeypatch.setattr(bridge_server, "_ready", False)
    assert bridge_server.is_running() is False

    monkeypatch.setattr(bridge_server, "_ready", True)
    assert bridge_server.is_running() is True


# --- 契約: 起動が失敗した理由を外から読める（票B2 契約5 / 完了条件 (e)） ---
# 起動に失敗した理由を module が保ち、panel が読める公開関数で取れること。
# 名前は実装側の決めどころ。別名になったら、この 2 行を直せば残りは通る。
STARTUP_ERROR_GETTER = "get_startup_error"
STARTUP_ERROR_ATTR = "_startup_error"


def _startup_error():
    getter = getattr(bridge_server, STARTUP_ERROR_GETTER, None)
    assert callable(getter), (
        f"起動失敗の理由を返す公開関数が無い: bridge_server.{STARTUP_ERROR_GETTER}"
    )
    return getter()


@pytest.fixture
def startup_error_stays_here(monkeypatch):
    """記録された理由を、この test の外へ持ち出さない。

    monkeypatch は今の値を控えて teardown で戻すので、名前が合っていればテスト中に
    付いた理由は片付く（別名なら掃除が空振りするだけで、契約の検証は変わらない）。
    """
    if hasattr(bridge_server, STARTUP_ERROR_ATTR):
        monkeypatch.setattr(
            bridge_server, STARTUP_ERROR_ATTR, getattr(bridge_server, STARTUP_ERROR_ATTR)
        )


def test_a_busy_port_leaves_a_readable_startup_reason(
    tmp_path, monkeypatch, startup_error_stays_here
):
    token_file = tmp_path / "bridge-temp" / "blender-session-token"
    monkeypatch.setattr(bridge_server, "_TMP_DIR", str(token_file.parent))
    monkeypatch.setattr(bridge_server, "_TOKEN_FILE", str(token_file))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        # Windows は SO_REUSEADDR を付けた bind に port を横取りさせる。
        # 「使用中」を確実に作るため、排他で押さえてから渡す。
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]

        try:
            bridge_server.start_server(port=busy_port)
        except OSError:
            # bind の例外をそのまま投げる実装でも、理由が取れることが契約。
            pass
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _startup_error() is None:
                time.sleep(0.01)

            reason = _startup_error()
            assert isinstance(reason, str) and reason.strip(), (
                f"port {busy_port} を塞いだまま起動したのに理由が取れない: {reason!r}"
            )
            assert bridge_server.is_running() is False
        finally:
            bridge_server.stop_server()


def test_a_healthy_bridge_reports_no_startup_reason(bridge_tcp):
    # 起動が通っている間は None。前の失敗が残り続けると、パネルは動いている橋に
    # 停止理由を出してしまう。
    assert _startup_error() is None


# --- 契約: 大きいシーンでも tool 応答の構造が保たれる（票E 契約1–3 / 完了条件 (a)–(d)） ---
# 上の generic 安全弁（data 全体を JSON 文字列化して切る）は最後の砦として残る。
# ここで見るのはその手前——tool ごとに件数・bytes で切り、有効な JSON 構造のまま返すこと。

MAX_LIST_ITEMS = 200  # objects / selection の上限（票E 契約1・2）
MAX_DOC_BYTES = 8_000  # doc 本文の上限（票E 契約3、2026-08-23 ユーザー裁定で 8KB）
FAKE_SCENE_NAME = "TruncationScene"
# 選択一覧を運ぶ field 名と get_doc の params 名は既存 schema 側の決めどころ。
# 票E が縛るのは件数・bytes と total_* / *_truncated / original_length の付き方なので、
# 名前には踏み込まない。別名なら、下の定数を直せば残りは通る。
SELECTION_LIST_KEYS = ("selected_objects", "objects", "selected", "selection")
DOC_TARGET = "bpy.ops.mesh.primitive_cube_add"
DOC_PARAMS = {"identifier": DOC_TARGET}
# doc 本文の埋め草は ASCII。JSON へ入れた時の膨らみ（\uXXXX 化）で 50KB 安全弁を踏むと、
# 見たい切り詰めの手前で別の仕組みが働いてしまう。マルチバイトは切り口へ狙って置く。
DOC_FILL_CHAR = "あ"  # UTF-8 で 3 bytes
DOC_HEAD = "HEAD:"
DOC_TAIL = ":TAIL"
UTF8_REPLACEMENT = chr(0xFFFD)  # 壊れた UTF-8 を decode した時に出る印


class FakeVector(list):
    """`mathutils.Vector` の身代わり。list として JSON に入り、`.x/.y/.z` でも読める。"""

    @property
    def x(self):
        return self[0]

    @property
    def y(self):
        return self[1]

    @property
    def z(self):
        return self[2]


class FakeSceneObject:
    """`bpy.types.Object` の身代わり——応答に載る field だけを実物型で持つ。

    conftest の bpy は MagicMock なので、素の object は mock を返して応答を JSON にできない
    （test_bridge_server_tcp.py の fake_blend_file と同じ寄せ方）。
    """

    def __init__(self, name, obj_type="MESH", visible=True, offset=0.0):
        self.name = name
        self.type = obj_type
        self.location = FakeVector((offset, 0.0, 0.0))
        self.rotation_euler = FakeVector((0.0, 0.0, 0.0))
        self.scale = FakeVector((1.0, 1.0, 1.0))
        self.mode = "OBJECT"  # active_object になった時に get_selection が読む
        self._visible = visible

    def visible_get(self):
        return self._visible


def _entry_names(entries):
    """一覧の各要素から名前を取る。要素の形（dict か name の str か）は問わない。"""
    return [entry if isinstance(entry, str) else entry["name"] for entry in entries]


def _selection_entries(data):
    """選択オブジェクトの一覧を持つ field を data から引く。"""
    found = [key for key in SELECTION_LIST_KEYS if isinstance(data.get(key), list)]
    assert len(found) == 1, (
        f"選択一覧の field を特定できない: keys={sorted(data)} / 候補={SELECTION_LIST_KEYS}"
    )
    return data[found[0]]


def _doc_text(data):
    """doc 本文を data から引く——一番長い文字列 field を本文とみなす。

    fixture は本文だけを飛び抜けて長く作るので、field 名を知らなくても特定できる。
    """
    bodies = [value for value in data.values() if isinstance(value, str)]
    assert bodies, f"doc 応答に文字列 field が無い: keys={sorted(data)}"
    return max(bodies, key=len)


def _doc_body_of(size_bytes, multibyte_at):
    """`multibyte_at` byte 目から 3 bytes 文字が始まる、指定 bytes ちょうどの doc 本文。"""
    filler = size_bytes - len(DOC_HEAD) - len(DOC_TAIL) - len(DOC_FILL_CHAR.encode("utf-8"))
    before = multibyte_at - len(DOC_HEAD)
    after = filler - before
    assert before >= 0 and after >= 0, f"印が入らない: {size_bytes} bytes / offset {multibyte_at}"
    body = DOC_HEAD + "x" * before + DOC_FILL_CHAR + "x" * after + DOC_TAIL
    assert len(body.encode("utf-8")) == size_bytes
    return body


def _cuts_mid_character(text, limit_bytes):
    """`limit_bytes` で素朴に byte 切りすると、文字の途中へ落ちるか。"""
    try:
        text.encode("utf-8")[:limit_bytes].decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _install_json_safe_scene(monkeypatch):
    """scene の見出し（name / frame_*）を JSON にできる実物型へ寄せる。"""
    monkeypatch.setattr(bpy.context.scene, "name", FAKE_SCENE_NAME)
    monkeypatch.setattr(bpy.context.scene, "frame_current", 1)
    monkeypatch.setattr(bpy.context.scene, "frame_start", 1)
    monkeypatch.setattr(bpy.context.scene, "frame_end", 250)


@pytest.fixture
def fake_scene_objects(monkeypatch):
    """`bpy.context.scene.objects` を、指定した数の軽量オブジェクトへ寄せる factory。

    件数は案件ごとに違うので、呼ぶ側が渡す。並べた順がそのまま「先頭 N 件」の順。
    """

    def install(count):
        objects = [
            FakeSceneObject(f"Obj.{index:04d}", offset=float(index)) for index in range(count)
        ]
        _install_json_safe_scene(monkeypatch)
        monkeypatch.setattr(bpy.context.scene, "objects", objects)
        return objects

    return install


@pytest.fixture
def fake_selection(monkeypatch):
    """選択オブジェクトの一覧を、指定した数の軽量オブジェクトへ寄せる factory。"""

    def install(count):
        objects = [
            FakeSceneObject(f"Sel.{index:04d}", offset=float(index)) for index in range(count)
        ]
        active = objects[0] if objects else None
        _install_json_safe_scene(monkeypatch)
        monkeypatch.setattr(bpy.context, "selected_objects", objects)
        monkeypatch.setattr(bpy.context, "active_object", active)
        # view_layer 経由で選択を読む実装でも同じ答えになるように。
        monkeypatch.setattr(bpy.context.view_layer.objects, "selected", objects)
        monkeypatch.setattr(bpy.context.view_layer.objects, "active", active)
        return objects

    return install


@pytest.fixture
def fake_doc(monkeypatch):
    """`lookup_doc` を、本文だけこちらが決める応答へ差し替える factory。

    実際の bpy から doc を集める経路へ依存させないための seam。切り詰めが lookup_doc の
    内側にある実装では、この差し替えが切り詰めごと外す——その時は本文が切れないので、
    下のテストが「切った印が無い」と鳴る（seam を下げる合図）。
    """
    from claude_bridge import doc_lookup

    def install(body):
        calls = []

        def lookup_doc(*args, **kwargs):
            calls.append((args, kwargs))
            # 実物 lookup_doc の返り形に合わせる（本文の切り詰めと案内は handler 側の仕事）
            return {"identifier": DOC_TARGET, "doc": body}

        monkeypatch.setattr(doc_lookup, "lookup_doc", lookup_doc)
        # `from .doc_lookup import lookup_doc` で取り込む実装でも差し替わるように。
        if hasattr(bridge_server, "lookup_doc"):
            monkeypatch.setattr(bridge_server, "lookup_doc", lookup_doc)
        return calls

    return install


def test_a_scene_over_the_cap_returns_a_capped_list_with_the_real_total(
    bridge_tcp, fake_scene_objects
):
    fake_scene_objects(MAX_LIST_ITEMS)
    small = bridge_tcp.send("get_scene_info", {})
    objects = fake_scene_objects(MAX_LIST_ITEMS + 1)
    truncated = bridge_tcp.send("get_scene_info", {})

    assert small["ok"] is True, small.get("error")
    assert truncated["ok"] is True, truncated.get("error")
    data = truncated["data"]
    assert {"objects", "total_objects"} <= set(data), (
        f"scene_info の schema が壊れている（data ごと文字列化された疑い）: keys={sorted(data)}"
    )
    assert len(data["objects"]) == MAX_LIST_ITEMS
    assert _entry_names(data["objects"]) == [obj.name for obj in objects[:MAX_LIST_ITEMS]]
    assert data["total_objects"] == MAX_LIST_ITEMS + 1
    assert data.get("objects_truncated") is True, f"切った印が無い: keys={sorted(data)}"
    # 他の keys は不変——増えるのは切った印だけ。
    assert set(data) == set(small["data"]) | {"objects_truncated"}
    assert set(truncated) == set(small), "封筒の key が増減している"
    assert data["scene_name"] == FAKE_SCENE_NAME
    assert any(key.startswith("frame_") for key in data), f"frame_* が無い: keys={sorted(data)}"


@pytest.mark.parametrize("count", [0, MAX_LIST_ITEMS])
def test_a_scene_within_the_cap_keeps_the_total_without_a_truncated_flag(
    bridge_tcp, fake_scene_objects, count
):
    objects = fake_scene_objects(count)

    response = bridge_tcp.send("get_scene_info", {})

    assert response["ok"] is True, response.get("error")
    data = response["data"]
    assert _entry_names(data["objects"]) == [obj.name for obj in objects]
    # 切らない時も総数は常に載る（票E 契約1）。
    assert data["total_objects"] == count
    assert "objects_truncated" not in data, (
        f"上限内なのに切った印が付いている: {data.get('objects_truncated')!r}"
    )


def test_a_selection_over_the_cap_returns_a_capped_list_with_the_real_total(
    bridge_tcp, fake_selection
):
    fake_selection(MAX_LIST_ITEMS)
    small = bridge_tcp.send("get_selection", {})
    objects = fake_selection(MAX_LIST_ITEMS + 1)
    truncated = bridge_tcp.send("get_selection", {})

    assert small["ok"] is True, small.get("error")
    assert truncated["ok"] is True, truncated.get("error")
    data = truncated["data"]
    entries = _selection_entries(data)
    assert len(entries) == MAX_LIST_ITEMS
    assert _entry_names(entries) == [obj.name for obj in objects[:MAX_LIST_ITEMS]]
    assert data.get("total_selected") == MAX_LIST_ITEMS + 1, f"keys={sorted(data)}"
    assert data.get("selection_truncated") is True, f"切った印が無い: keys={sorted(data)}"
    assert set(data) == set(small["data"]) | {"selection_truncated"}
    assert set(truncated) == set(small), "封筒の key が増減している"


@pytest.mark.parametrize("count", [0, MAX_LIST_ITEMS])
def test_a_selection_within_the_cap_keeps_the_total_without_a_truncated_flag(
    bridge_tcp, fake_selection, count
):
    objects = fake_selection(count)

    response = bridge_tcp.send("get_selection", {})

    assert response["ok"] is True, response.get("error")
    data = response["data"]
    assert _entry_names(_selection_entries(data)) == [obj.name for obj in objects]
    # 切らない時も総数は常に載る（票E 契約2）。
    assert data.get("total_selected") == count, f"keys={sorted(data)}"
    assert "selection_truncated" not in data, (
        f"上限内なのに切った印が付いている: {data.get('selection_truncated')!r}"
    )


@pytest.mark.parametrize("size_bytes", [MAX_DOC_BYTES + 16, MAX_DOC_BYTES * 3])
def test_an_oversized_doc_is_capped_without_breaking_a_multibyte_character(
    bridge_tcp, fake_doc, size_bytes
):
    original = _doc_body_of(size_bytes, multibyte_at=MAX_DOC_BYTES - 1)
    assert _cuts_mid_character(original, MAX_DOC_BYTES), "切り口が文字の途中に無い本文"
    calls = fake_doc(original)

    response = bridge_tcp.send("get_doc", DOC_PARAMS)

    assert response["ok"] is True, response.get("error")
    assert calls, f"lookup_doc まで届いていない（params 名が違う疑い）: {DOC_PARAMS}"
    data = response["data"]
    body = _doc_text(data)
    assert data.get("doc_truncated") is True, f"切った印が無い: keys={sorted(data)}"
    # original_length は切る前の文字数（bytes ではない）。
    assert data.get("original_length") == len(original), f"keys={sorted(data)}"
    # 案内が無いと切られた doc は実質取得不可（2026-08-23 ユーザー裁定）。
    hint = data.get("full_doc_hint", "")
    assert DOC_TARGET in hint and "__doc__" in hint, f"全文への案内が無い: {hint!r}"
    body_bytes = len(body.encode("utf-8"))
    assert body_bytes <= MAX_DOC_BYTES
    assert body_bytes >= MAX_DOC_BYTES - len(DOC_FILL_CHAR.encode("utf-8")), (
        f"上限 {MAX_DOC_BYTES} bytes に対して切りすぎ: {body_bytes} bytes"
    )
    assert UTF8_REPLACEMENT not in body, "UTF-8 の途中で切れて置換文字が出ている"
    assert body.startswith(DOC_HEAD)
    assert DOC_TAIL not in body
    assert original.startswith(body), (
        "本文が頭からの切り出しになっていない（末尾へ印を足す実装なら契約側の追記が要る）"
    )


@pytest.mark.parametrize("size_bytes", [MAX_DOC_BYTES // 4, MAX_DOC_BYTES])
def test_a_doc_within_the_byte_limit_comes_back_whole(bridge_tcp, fake_doc, size_bytes):
    # ちょうど 40,000 bytes は「超」ではない——完了条件 (d) は「40KB 超で本文が切れ」。
    original = _doc_body_of(size_bytes, multibyte_at=len(DOC_HEAD))
    fake_doc(original)

    response = bridge_tcp.send("get_doc", DOC_PARAMS)

    assert response["ok"] is True, response.get("error")
    data = response["data"]
    assert _doc_text(data) == original
    # 切った時だけ足す（票E 契約3）。
    assert "doc_truncated" not in data
    assert "original_length" not in data
    assert "full_doc_hint" not in data
