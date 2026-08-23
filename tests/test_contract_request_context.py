"""外形契約: 依頼の始まりを基準にした file switch の報せ（作業票 `_worksheet-request-context.md`）。

基準は「この依頼が始まった時のファイル」で、「前回 execute が見たファイル」ではない。
だから read-only だけで終わる依頼でも、bridge 起動後の一件目の依頼でも、切替が届く。

観測点は2つだけ——TCP の封筒（bridge）と、tool が Claude へ返すテキスト（MCP）。
context は契約の API で置く（`set_request_context` / `clear_request_context`）。
パネルがやるのと同じ順で置くだけで、context の持ち方にも世代の数え方にも触らない。

知らせるだけで弾かない、が設計の方針。だから「実行は通る」ことも一緒に固定する。
（世代の採番そのものと、context を持たない時の全体像は tests/test_file_switch.py が持つ。）
"""
import dataclasses
import inspect
import socket
import time

import bpy
import pytest

from claude_bridge import bridge_server
from mcp_server import server


# 契約4 の注意文。bridge が載せた印を MCP が言葉にする（文面は tests/test_file_switch.py と同じ）。
FILE_SWITCH_NOTICE = (
    "The .blend file was switched since the previous operation. Re-check the scene "
    "state before assuming anything about object names, selection, or the file path."
)
# 契約2 の対象コマンド。params の key は schema 側の決めどころなので、名前が違っても
# 答えが変わらない見方をする——ここで見るのは封筒の印だけで、中身も ok も問わない。
SWITCH_AWARE_COMMANDS = (
    ("get_scene_info", {}),
    ("get_selection", {}),
    ("get_object_info", {"name": "Cube"}),
    ("get_viewport_screenshot", {}),
    ("execute_code", {"code": "result = 'ran'"}),
)
# 契約4 の tool のうち、封筒の data をそのままテキストにして返す側。
DATA_TOOLS = ("get_scene_info", "get_selection", "get_object_info", "execute_code")
# 引数を要る tool へ渡す埋め草。bridge は差し替え済みなので、中身は応答を変えない。
PLACEHOLDER_ARGUMENT = "Cube"
SCENE_NAME = "RequestContextScene"


def _switched(response):
    """封筒に載った切替の印。切替が無い時はキー自体が来ない。"""
    return response.get("file_switched")


def _start_request():
    """依頼の始まり。パネルが送信時にやるのと同じ——今のファイルを context へ置く。"""
    generation = bridge_server.current_generation()
    bridge_server.set_request_context(generation)
    return generation


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

    conftest の bpy は MagicMock なので、素の object は mock を返して応答を JSON に
    できない（tests/test_bridge_server_unit.py と同じ寄せ方）。
    """

    def __init__(self, name, obj_type="MESH", offset=0.0):
        self.name = name
        self.type = obj_type
        self.location = FakeVector((offset, 0.0, 0.0))
        self.rotation_euler = FakeVector((0.0, 0.0, 0.0))
        self.scale = FakeVector((1.0, 1.0, 1.0))
        self.mode = "OBJECT"  # active_object になった時に get_selection が読む
        self._visible = True

    def visible_get(self):
        return self._visible


def _restarted(bridge_tcp):
    """bridge を止めて、新しい port で起動し直した口を返す。

    停止で何が落ちるかは、起動し直した先へ依頼を投げて初めて外から見える。
    Blender の代わりに回している pump は port に紐づかないので、次の server を
    そのまま回し続ける。準備できたかは ping が通ったかで見る（token の書き直しを
    待たずに投げると、古い token のまま断られる）。
    """
    bridge_tcp.server.stop_server()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    bridge_tcp.server.start_server(port=port)

    restarted = dataclasses.replace(bridge_tcp, port=port)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if restarted.send("ping", {}, timeout=1).get("ok") is True:
                return restarted
        except (OSError, ValueError, KeyError):
            pass  # まだ立ち上がりきっていない。次の観測で拾う
        time.sleep(0.05)
    raise AssertionError("bridge が起動し直さなかった")


@pytest.fixture
def window_manager(monkeypatch, fake_window_manager):
    """WM を値の残る fake にし、世代と request context を契約の初期値へ戻す。

    `_file_generation` / `_last_exec_generation` はプロセス内グローバルなので、
    戻さないと前のテストの世代を引きずる（tests/test_file_switch.py と同じ戻し方）。
    context は契約の API で閉じる。前後の両方で閉じるのは、依頼の途中で落ちた
    テストの context を次へ持ち越さないため。
    """
    monkeypatch.setattr(bridge_server, "_file_generation", 0, raising=False)
    monkeypatch.setattr(bridge_server, "_last_exec_generation", None, raising=False)
    bridge_server.clear_request_context()
    yield fake_window_manager(claude_bridge_generation=0)
    bridge_server.clear_request_context()


@pytest.fixture
def readable_scene(monkeypatch):
    """読みのコマンドが JSON にできる scene へ寄せる。

    見出し（name / frame_*）と一覧（objects / 選択）だけを実物型にする。
    どの field が応答に載るかは schema 側の決めどころなので、そこは見ない。
    """
    objects = [
        FakeSceneObject("Cube"),
        FakeSceneObject("Light", obj_type="LIGHT", offset=2.0),
    ]
    monkeypatch.setattr(bpy.context.scene, "name", SCENE_NAME)
    monkeypatch.setattr(bpy.context.scene, "frame_current", 1)
    monkeypatch.setattr(bpy.context.scene, "frame_start", 1)
    monkeypatch.setattr(bpy.context.scene, "frame_end", 250)
    monkeypatch.setattr(bpy.context.scene, "objects", objects)
    monkeypatch.setattr(bpy.context, "selected_objects", objects[:1])
    monkeypatch.setattr(bpy.context, "active_object", objects[0])
    # view_layer 経由で選択を読む実装でも同じ答えになるように。
    monkeypatch.setattr(bpy.context.view_layer.objects, "selected", objects[:1])
    monkeypatch.setattr(bpy.context.view_layer.objects, "active", objects[0])
    return objects


# --- 契約1: request context の置き方 ---


def test_the_request_context_takes_a_generation_and_answers_nothing(window_manager):
    """契約1: 渡すのは `current_generation()` の値（int、1以上）。返り値は無い。"""
    generation = bridge_server.current_generation()

    assert isinstance(generation, int) and generation >= 1, (
        f"前提: 世代は 1 以上の int（tests/test_file_switch.py 契約1）: {generation!r}"
    )
    assert bridge_server.set_request_context(generation) is None, (
        "契約1: set_request_context は置くだけ。返り値は無い"
    )
    assert bridge_server.clear_request_context() is None, (
        "契約1: clear_request_context は引数も返り値も無い"
    )


# --- 契約2: 依頼の間、封筒に印が載る ---
# 起点は「依頼が始まった時のファイル」。だから状態もユーザーの手順で作る——
# 依頼を出す（context を置く）、別の .blend を開く、Claude がコマンドを投げる。


def test_a_request_that_only_reads_still_reports_the_switch(
    bridge_tcp, window_manager, readable_scene
):
    """完了条件1: `get_scene_info` だけで終わる依頼でも、切替は封筒に出る。

    context を持たない時の照合は execute_code しか見ないので、読みの封筒に載った
    この印は request context からしか来ない。
    """
    _start_request()  # 依頼は file A の上で始まった

    window_manager.simulate_file_load()  # 依頼の最中に file B が開いた

    read = bridge_tcp.send("get_scene_info", {})

    assert _switched(read) is True, f"契約2: 読みだけの依頼でも切替を知らせる: {read}"
    assert read["ok"] is True, "契約2: 知らせるだけで弾かない。読みはそのまま通る"


def test_the_first_execute_of_a_switched_request_reports_it(bridge_tcp, window_manager):
    """完了条件2: bridge 新規起動後の一件目でも、最初の execute_code で知らせる。

    ここは前回 execute がまだ無い場面。context を持たない時は黙るところ
    （下の `..._without_a_context_...` と対）で、依頼の基準があるから知らせられる。
    """
    _start_request()

    window_manager.simulate_file_load()

    executed = bridge_tcp.send("execute_code", {"code": "result = 'first'"})

    assert _switched(executed) is True, (
        f"契約2: 依頼の一発目の execute でも切替を知らせる: {executed}"
    )
    assert executed["ok"] is True and executed["data"]["result"] == "first", (
        "契約2: 知らせるだけで弾かない。切替の後もコードは走る"
    )


def test_every_command_of_a_switched_request_carries_the_mark(
    bridge_tcp, window_manager, readable_scene
):
    """契約2: 印は依頼の間ずっと出る。誰かが先に見ても消費されない。

    2度目の execute_code がここの要——前回 execute との比較なら一度で黙る。
    印が残っていることが「基準は依頼が始まった時のファイル」である証拠になる。
    """
    _start_request()

    window_manager.simulate_file_load()

    responses = [
        bridge_tcp.send("get_scene_info", {}),
        bridge_tcp.send("execute_code", {"code": "result = 'one'"}),
        bridge_tcp.send("get_selection", {}),
        bridge_tcp.send("execute_code", {"code": "result = 'two'"}),
    ]

    assert [_switched(response) for response in responses] == [True, True, True, True], (
        f"契約2: 依頼の間は毎回載せる: {responses}"
    )


@pytest.mark.parametrize(
    ("command", "params"),
    SWITCH_AWARE_COMMANDS,
    ids=[command for command, _params in SWITCH_AWARE_COMMANDS],
)
def test_each_listed_command_carries_the_mark_during_a_switched_request(
    bridge_tcp, window_manager, readable_scene, command, params
):
    """契約2: 対象コマンドはどれも、依頼の間の切替を封筒で知らせる。

    見るのは印だけで、コマンドが成功したかは問わない。影の Blender では
    完走できないコマンドもあるが、失敗の封筒にも印は載る（弾かない設計の裏返し）。
    """
    _start_request()

    window_manager.simulate_file_load()

    response = bridge_tcp.send(command, params)

    assert _switched(response) is True, (
        f"契約2: {command} の封筒にも切替の印を載せる: {response}"
    )


def test_a_request_on_the_unchanged_file_carries_no_mark(
    bridge_tcp, window_manager, readable_scene
):
    """完了条件3: 切替が無い依頼では、キー自体が封筒に現れない。"""
    _start_request()

    read = bridge_tcp.send("get_scene_info", {})
    executed = bridge_tcp.send("execute_code", {"code": "result = 'same file'"})
    selection = bridge_tcp.send("get_selection", {})

    for response in (read, executed, selection):
        assert "file_switched" not in response, (
            f"契約2: 同じファイルで居る間はキーを出さない: {response}"
        )


def test_a_request_that_starts_after_the_switch_has_nothing_to_report(
    bridge_tcp, window_manager, readable_scene
):
    """契約2 の境界: 依頼より前の切替は、この依頼の報せではない。

    基準は依頼が始まった時のファイル。開き直してから頼んだのだから、
    ユーザーはもう新しいファイルを見ている。
    """
    bridge_server.current_generation()
    window_manager.simulate_file_load()  # 依頼を出す前に別の .blend を開いた

    _start_request()  # 依頼は開いた後のファイルの上で始まる

    read = bridge_tcp.send("get_scene_info", {})
    executed = bridge_tcp.send("execute_code", {"code": "result = 'fresh'"})

    assert "file_switched" not in read, f"契約2: 依頼の中では切り替わっていない: {read}"
    assert "file_switched" not in executed, (
        f"契約2: 依頼より前の切替を蒸し返さない: {executed}"
    )


# --- 契約3: context を持たない時（desktop 直用）は現行のまま ---
# ここは「変えないこと」。request context を足したせいで desktop 直用の見え方が
# 動いていないかを、同じ観測点で確かめる。


def test_a_read_only_command_without_a_context_stays_quiet(
    bridge_tcp, window_manager, readable_scene
):
    """契約3: context が無い間、読みのコマンドは今までどおり黙る。"""
    bridge_server.current_generation()  # 起点を揃える

    window_manager.simulate_file_load()

    read = bridge_tcp.send("get_scene_info", {})

    assert read["ok"] is True
    assert "file_switched" not in read, (
        f"契約3: context の外では、印が載るのは execute_code だけ: {read}"
    )


def test_the_first_execute_without_a_context_stays_quiet(bridge_tcp, window_manager):
    """契約3: bridge 起動後の最初の execute は黙ったまま。

    ここが request context の塞ぐ穴。塞ぎ方は「依頼の基準を置く」ことで、
    context を持たない側の見え方は動かさない。
    """
    bridge_server.current_generation()  # 起点を揃える

    window_manager.simulate_file_load()

    first = bridge_tcp.send("execute_code", {"code": "result = 'first'"})

    assert first["ok"] is True
    assert "file_switched" not in first, (
        f"契約3: 比較する前回 execute がまだ無い。ここは黙る: {first}"
    )

    window_manager.simulate_file_load()
    after = bridge_tcp.send("execute_code", {"code": "result = 'after'"})

    assert _switched(after) is True, (
        f"契約3: 前回 execute ができた後の切替は、今までどおり知らせる: {after}"
    )


def test_execute_keeps_feeding_the_fallback_while_a_context_is_set(
    bridge_tcp, window_manager
):
    """契約2: context がある間も、execute は「前回 execute が見た世代」を更新し続ける。

    更新を止めると、依頼を閉じた後の照合が古い世代と比べて、同じ切替をもう一度
    知らせてしまう。desktop 直用の見え方はそこで壊れる。
    """
    _start_request()
    window_manager.simulate_file_load()
    during = bridge_tcp.send("execute_code", {"code": "result = 'during'"})
    assert _switched(during) is True, "前提: 依頼の中では切替を知らせる"

    bridge_server.clear_request_context()  # 依頼が終わる

    after = bridge_tcp.send("execute_code", {"code": "result = 'after'"})

    assert "file_switched" not in after, (
        f"契約2: 依頼の中の execute も前回世代を更新する。"
        f"更新していないと、依頼を閉じた後に同じ切替を蒸し返す: {after}"
    )


def test_clearing_the_context_hands_the_watch_back_to_the_fallback(
    bridge_tcp, window_manager, readable_scene
):
    """契約: `clear_request_context()` の後は、context を持たない時の挙動へ戻る。"""
    _start_request()
    bridge_tcp.send("execute_code", {"code": "result = 'in request'"})  # 起点を揃える

    bridge_server.clear_request_context()
    window_manager.simulate_file_load()

    read = bridge_tcp.send("get_scene_info", {})
    first = bridge_tcp.send("execute_code", {"code": "result = 'first'"})
    again = bridge_tcp.send("execute_code", {"code": "result = 'again'"})

    assert "file_switched" not in read, f"契約3: 依頼の外では読みは黙る: {read}"
    assert _switched(first) is True, f"契約3: 切替後の最初の execute では知らせる: {first}"
    assert "file_switched" not in again, (
        f"契約3: 知らせるのは切替のたびに1回。次からは黙る: {again}"
    )


def test_stopping_the_server_drops_the_request_context(
    bridge_tcp, window_manager, readable_scene
):
    """契約1: `stop_server()` は request context も片付ける。

    依頼の途中で bridge が止まると、`clear_request_context()` を呼ぶはずの
    終わりが来ない。持ち越した context は、次の起動で無関係な切替を知らせ続ける。
    """
    bridge_server.current_generation()
    window_manager.simulate_file_load()
    bridge_server.current_generation()
    window_manager.simulate_file_load()
    stale = _start_request()
    assert stale > 1, "前提: 起動し直した後の世代と重ならない世代を context へ置く"

    restarted = _restarted(bridge_tcp)

    read = restarted.send("get_scene_info", {})

    assert read["ok"] is True, f"前提: 起動し直した bridge が応えている: {read}"
    assert "file_switched" not in read, (
        f"契約1: 止めた時点で context は消える。持ち越した世代と比べない: {read}"
    )


# --- 契約5 のエラー時: 世代が読めなくても止めない ---


def test_a_generation_that_cannot_be_read_does_not_stop_the_command(
    bridge_tcp, window_manager, readable_scene, fake_window_manager
):
    """契約5: 照合の材料が無くても、実行も読みもそのまま通す。

    世代の器（`claude_bridge_generation`）が登録されていない WM へ差し替える。
    Blender では add-on の register 前後や、器を失った状態がこれに当たる。
    """
    _start_request()

    fake_window_manager()  # 世代の器を持たない window_manager へ差し替える

    executed = bridge_tcp.send("execute_code", {"code": "result = 'ran anyway'"})
    read = bridge_tcp.send("get_scene_info", {})

    assert executed["ok"] is True and executed["data"]["result"] == "ran anyway", (
        f"契約5: 世代が読めなくても実行は止めない: {executed}"
    )
    assert read["ok"] is True, f"契約5: 読みも通常どおり応える: {read}"


# --- 契約4: MCP の出口 ---
# 封筒の印は、ここで言葉になって初めて Claude に届く。見るのは「本文の前に notice が
# 立つか」だけなので、封筒の data は埋め草でいい。
# get_viewport_screenshot は画像を運ぶ tool で、その封筒の形はこの票の契約に無い。
# 印が載るところは上の contract2 の parametrize が押さえ、前置きの確認は
# data をテキストにして返す tool で行う。


class _CannedTransport:
    """MCP から Blender への運び役の身代わり。決めた封筒を、どのコマンドにも返す。

    差し替え口は tests/test_contract_scratch.py と同じ `server.bridge` の公開面。
    """

    def __init__(self):
        self.envelope = {}
        self.sent = []

    def send(self, command, params=None, *_args, **_kwargs):
        self.sent.append((command, dict(params or {})))
        return self.envelope


def _text(returned):
    """tool の返りを Claude が読む文として取り出す。

    返りが list[TextContent | ImageContent] でも str でも同じ形で読む
    （文字の側だけを繋ぐのは tests/test_capture_after.py と同じ）。
    """
    if isinstance(returned, str):
        return returned
    return "\n".join(
        item.text for item in returned if getattr(item, "type", None) == "text"
    )


def _placeholder_arguments(tool):
    """署名の必須引数だけを埋め草で埋める。

    引数の名前と数は tool ごとの決めどころで、この票の契約は縛っていない。
    契約が縛るのは「封筒の file_switched が応答テキストの頭に出るか」だけなので、
    値は何でもいい——bridge は差し替え済みで、封筒は引数で変わらない。
    """
    return {
        name: PLACEHOLDER_ARGUMENT
        for name, parameter in inspect.signature(tool).parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
    }


@pytest.fixture
def mcp_reply(monkeypatch):
    """封筒を1つ決めて、tool が Claude へ返すテキストを取り出す factory。

    差し替えるのは運び役だけで、tool の中身には触らない。
    """
    transport = _CannedTransport()
    monkeypatch.setattr(server, "bridge", transport)

    def reply(tool_name, envelope):
        transport.envelope = envelope
        transport.sent.clear()
        tool = getattr(server, tool_name)
        returned = tool(**_placeholder_arguments(tool))
        assert transport.sent, f"{tool_name} が bridge を呼んでいない"
        return _text(returned)

    return reply


@pytest.mark.parametrize("tool_name", DATA_TOOLS)
def test_a_switched_envelope_puts_the_notice_in_front_of_every_tool(
    mcp_reply, tool_name
):
    """契約4: read-only tool を含めて、封筒の印は notice になって本文の前に立つ。"""
    plain = mcp_reply(tool_name, {"ok": True, "data": {"result": "plain body"}})
    switched = mcp_reply(
        tool_name, {"ok": True, "file_switched": True, "data": {"result": "plain body"}}
    )

    assert plain, f"前提: {tool_name} が本文を返している"
    assert switched.startswith(FILE_SWITCH_NOTICE + "\n"), (
        f"契約4: {tool_name} でも注意文は先頭に、本文と混ざらない形で立つ: {switched!r}"
    )
    assert switched.endswith(plain), (
        f"契約4: 前置きするだけ。本文は変えない: {switched!r}"
    )


@pytest.mark.parametrize("tool_name", DATA_TOOLS)
def test_a_plain_envelope_leaves_every_tool_reply_untouched(mcp_reply, tool_name):
    """契約4: 封筒に印が無ければ前置きしない。今までの応答の形のまま。"""
    plain = mcp_reply(tool_name, {"ok": True, "data": {"result": "plain body"}})

    assert FILE_SWITCH_NOTICE not in plain, (
        f"契約4: 切替が無いのに注意文を足さない: {plain!r}"
    )
    assert "plain body" in plain, f"前提: {tool_name} が本文を返している: {plain!r}"
