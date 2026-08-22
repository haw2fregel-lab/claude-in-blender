"""ファイル切替の通知 — 作業票 `_worksheet-file-switch-fail-close.md` の契約1〜4。

方針は「弾かない、知らせる」。だから **拒否しないこと** も契約として固定する。
検知は WM プロパティが .blend のロードで default へ戻る挙動に乗る
（実測: Blender 5.1.2 headless。保存では戻らない = 誤検知しない）。

呼び手はそれぞれ「前回自分が見た世代」を自分で持ち、採番だけを共有する。
先に見た側が相手の検知を食わないことが、この設計の要。

知らせる相手は2人。パネルの確認がユーザーへ、MCP の前置きが Claude へ届く。
だから bridge の層で終わらせず、Claude が読む出口まで通してこのファイルで見る。
"""
import json
from types import SimpleNamespace

import bpy
import pytest

from claude_bridge import bridge_server
from claude_bridge import panel
from mcp_server import server


_PROMPT = "塔を生やして"
_EVENT = SimpleNamespace(type="LEFTMOUSE", value="PRESS")
# 契約2 の文面。確認ダイアログの中に、この2文がこの順で出る。
CONFIRM_SENTENCES = (
    "The .blend file was switched since your last request.",
    "Send this request against the current file?",
)
# 契約3 の注意文。bridge が載せた印を MCP が言葉にして、Claude はここを読む。
FILE_SWITCH_NOTICE = (
    "The .blend file was switched since the previous operation. Re-check the scene "
    "state before assuming anything about object names, selection, or the file path."
)
# 封筒のメタ（tests/test_bridge_server_unit.py と同じ見方）。通知はここを動かさない。
ENVELOPE_METADATA_KEYS = ("request_id", "ack_required", "status", "blocker_request_id")


def _envelope_metadata(response):
    return {key: response[key] for key in ENVELOPE_METADATA_KEYS if key in response}


def _file_switched(response):
    """契約3 のキー。封筒の top-level に載る（窓口裁定）。無ければ None。"""
    return response.get("file_switched")


def _context():
    """オペレーターへ渡す context。window_manager だけ fake で、残りは影のまま。"""
    return bpy.context


def _confirm_calls(window_manager):
    """確認ダイアログが開いたか。

    契約が決めているのは「確認を出す」ところまでで、どの `invoke_*` を使うかは
    パネルの選択。だから名前で拾い、ダイアログの種類には縛りをかけない。
    """
    return [call for call in window_manager.calls if call.name.startswith("invoke_")]


class _FakeLayout:
    """`draw()` が書き込む layout。label へ渡った文字列を集める。"""

    def __init__(self):
        self.labels = []

    def label(self, text="", **_kwargs):
        self.labels.append(text)

    def __getattr__(self, name):
        # row() / column() / box() は自分を返す。どの入れ子に置かれた label も拾える。
        return lambda *_args, **_kwargs: self


def _dialog_text(operator, context):
    """確認ダイアログの中身。

    Blender の確認ダイアログは文面を引数で受け取らず、`draw()` が layout へ書く。
    だから「確認へ渡された文字列」ではなく、描かれた文字列を読む。
    """
    layout = _FakeLayout()
    operator.layout = layout
    operator.draw(context)
    return "\n".join(layout.labels)


def _settle():
    """送信はワーカースレッドで始まる。数えるのはスレッドが動き出してから。"""
    worker = getattr(panel, "_worker", None)
    if worker is not None:
        worker.join(timeout=2)


def _fake_worker(alive):
    """ワーカーの生死だけを模す。timer が見るのは `is_alive()` だけ。"""
    return SimpleNamespace(is_alive=lambda: alive)


def _break_window_manager(monkeypatch, shape):
    """契約1 のエラー時: bpy.context.window_manager が取れない状態を作る。"""
    if shape == "null":
        monkeypatch.setattr(bpy.context, "window_manager", None)
    else:
        monkeypatch.setattr(bpy, "context", SimpleNamespace())


@pytest.fixture
def window_manager(monkeypatch, fake_window_manager):
    """WM を値の残る fake にし、世代のグローバルを契約の初期値へ戻す。

    `_file_generation` / `_last_sent_generation` / `_last_exec_generation` は
    どれもプロセス内グローバルなので、テストごとに戻さないと前のテストを引きずる。
    """
    monkeypatch.setattr(bridge_server, "_file_generation", 0, raising=False)
    monkeypatch.setattr(bridge_server, "_last_exec_generation", None, raising=False)
    monkeypatch.setattr(panel, "_last_sent_generation", None, raising=False)
    return fake_window_manager(
        claude_bridge_generation=0,
        claude_bridge_prompt=_PROMPT,
        claude_bridge_status="IDLE",
        # パネルが読むコンテキストトグル。全部オフ = 送信文に指示が付かない状態。
        **{prop: False for prop, _label, _text in panel._CTX_TOGGLES},
    )


@pytest.fixture
def waiting(monkeypatch):
    """まだ結果が返っていない状態。`_result_box` はプロセス内グローバル。"""
    monkeypatch.setattr(
        panel, "_result_box", {"ready": False, "text": "", "error": False}
    )


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """送信が始まったかを、渡された prompt で拾う。claude は起動しない。

    確認が invoke と execute のどちらに載っても、送信の入口は `_run_claude`。
    どちらから呼ばれても同じ形で数えられるので、ここを観測点にする。
    送信は bridge が動いていること・prompt が空でないこと・前のワーカーが終わって
    いることを確かめてから始まるので、そこまでの前提をここで揃える。
    BRIDGE_FILE も tmp へ逃がす（ユーザー本物の session ファイルを触らせない）。
    """
    prompts = []
    bridge_file = tmp_path / "claude" / "blender-bridge-session.json"
    bridge_file.parent.mkdir(parents=True)
    bridge_file.write_text(
        json.dumps({"cwd": str(tmp_path).replace("\\", "/"), "session_id": None}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(panel, "BRIDGE_FILE", bridge_file)
    monkeypatch.setattr(bridge_server, "is_running", lambda: True)
    monkeypatch.setattr(
        panel,
        "_run_claude",
        lambda prompt, *_args, **_kwargs: prompts.append(prompt),
    )
    monkeypatch.setattr(panel, "_worker", None)
    yield prompts
    _settle()


# --- 土台: fake が実測どおりか ---
# ここが実測とズレると、以下の契約テストは全部嘘になる。


def test_the_window_manager_fake_matches_the_measured_blender_behavior(window_manager):
    window_manager.claude_bridge_generation = 7

    window_manager.simulate_file_save()
    assert window_manager.claude_bridge_generation == 7

    window_manager.simulate_file_load()
    # 登録は生き残り、値だけ default へ戻る（AttributeError にはならない）。
    assert window_manager.claude_bridge_generation == 0


# --- 契約1: 世代の同定 ---


def test_the_generation_marker_is_registered_as_a_window_manager_property():
    assert "claude_bridge_generation" in panel._WM_PROPS, (
        "契約1: claude_bridge_generation を _WM_PROPS へ載せる。"
        "ロードで default へ戻る器が無ければ切替は検知できない"
    )


def test_the_first_call_starts_the_generation_at_one(window_manager):
    assert bridge_server.current_generation() == 1
    # 書き戻しておかないと、次のロードで default へ戻ったことが分からない。
    assert window_manager.claude_bridge_generation == 1


def test_the_generation_stays_put_while_the_same_file_is_open(window_manager):
    first = bridge_server.current_generation()

    assert bridge_server.current_generation() == first
    assert bridge_server.current_generation() == first


def test_saving_the_file_does_not_move_the_generation(window_manager):
    first = bridge_server.current_generation()

    window_manager.simulate_file_save()

    assert bridge_server.current_generation() == first, (
        "契約1: 保存では世代を進めない。進めると保存のたびに誤検知する"
    )


def test_each_file_load_moves_the_generation_once(window_manager):
    assert bridge_server.current_generation() == 1

    window_manager.simulate_file_load()
    assert bridge_server.current_generation() == 2

    window_manager.simulate_file_load()
    assert bridge_server.current_generation() == 3


def test_the_switch_is_not_consumed_by_whoever_looks_first(window_manager):
    """採番だけを共有する。世代を見たこと自体は、誰の検知も消費しない。"""
    bridge_server.current_generation()
    window_manager.simulate_file_load()

    assert bridge_server.current_generation() == 2
    assert bridge_server.current_generation() == 2


def test_the_generation_restarts_after_the_server_stops(
    window_manager, monkeypatch, tmp_path
):
    monkeypatch.setattr(bridge_server, "_TMP_DIR", str(tmp_path / "bridge-temp"))
    monkeypatch.setattr(
        bridge_server,
        "_TOKEN_FILE",
        str(tmp_path / "bridge-temp" / "blender-session-token"),
    )
    bridge_server.current_generation()
    window_manager.simulate_file_load()
    assert bridge_server.current_generation() == 2

    bridge_server.stop_server()

    assert bridge_server.current_generation() == 1, (
        "契約1: stop_server で _file_generation を 0 へ戻し、次の start で採番し直す"
    )


# --- 契約1 のエラー時: window_manager が取れない ---


@pytest.mark.parametrize("shape", ["null", "absent"])
def test_a_missing_window_manager_never_raises(window_manager, monkeypatch, shape):
    _break_window_manager(monkeypatch, shape)

    # 未初期化なら 1。検知の失敗で送信や実行を止めない。
    assert bridge_server.current_generation() == 1
    assert bridge_server.current_generation() == 1


@pytest.mark.parametrize("shape", ["null", "absent"])
def test_a_missing_window_manager_keeps_the_last_generation(
    window_manager, monkeypatch, shape
):
    bridge_server.current_generation()
    window_manager.simulate_file_load()
    assert bridge_server.current_generation() == 2

    _break_window_manager(monkeypatch, shape)

    # 読めない = default の 0 に見える、ではない。読めない時は直前の世代を返す。
    assert bridge_server.current_generation() == 2
    assert bridge_server.current_generation() == 2


# --- 契約2: パネルの確認 ---


def test_the_first_send_does_not_ask_for_confirmation(window_manager, sent):
    panel.CLAUDE_OT_send().invoke(_context(), _EVENT)
    _settle()

    assert _confirm_calls(window_manager) == [], (
        "契約2: 初回送信は比較対象が無いので確認を出さない"
    )
    assert sent, "契約2: 確認が要らない時は従来どおり送信する"


def test_a_send_after_a_file_switch_asks_before_running(
    window_manager, sent, monkeypatch
):
    monkeypatch.setattr(
        panel, "_last_sent_generation", bridge_server.current_generation()
    )

    window_manager.simulate_file_load()
    panel.CLAUDE_OT_send().invoke(_context(), _EVENT)
    _settle()

    assert _confirm_calls(window_manager), "契約2: 切替後の送信では確認を出す"
    assert sent == [], "契約2: 確認より先に送信しない"


def test_the_confirmation_says_the_blend_file_was_switched(
    window_manager, sent, monkeypatch
):
    monkeypatch.setattr(
        panel, "_last_sent_generation", bridge_server.current_generation()
    )
    operator = panel.CLAUDE_OT_send()

    window_manager.simulate_file_load()
    operator.invoke(_context(), _EVENT)

    # 確認が開いた時にだけ描かれる文面。開かないなら文面だけあっても意味がない。
    assert _confirm_calls(window_manager), "契約2: 切替後の送信では確認を出す"
    text = _dialog_text(operator, _context())
    for sentence in CONFIRM_SENTENCES:
        assert sentence in text, f"契約2 の文面が確認に出ていない: {sentence!r}"


def test_a_confirmed_send_does_not_ask_again_until_the_next_file_load(
    window_manager, sent, monkeypatch
):
    """OK を押した後 = 送信が実行され、marker が今の世代になった後の状態。"""
    bridge_server.current_generation()
    window_manager.simulate_file_load()
    monkeypatch.setattr(
        panel, "_last_sent_generation", bridge_server.current_generation()
    )

    panel.CLAUDE_OT_send().invoke(_context(), _EVENT)
    _settle()
    assert _confirm_calls(window_manager) == [], (
        "契約2: 一度 OK を押したら、同じファイルで居る限り再度出さない"
    )

    window_manager.simulate_file_load()
    panel.CLAUDE_OT_send().invoke(_context(), _EVENT)

    assert _confirm_calls(window_manager), "契約2: 次のロードでは再び確認を出す"


def test_a_sent_request_records_the_generation_it_was_sent_against(
    window_manager, sent
):
    """契約2: 送信を実行したら _last_sent_generation を現在の世代で更新する。

    ここが無いと「OK を押しても毎回確認が出る」になる。
    """
    panel.CLAUDE_OT_send().invoke(_context(), _EVENT)
    _settle()

    assert sent, "前提: 初回送信は確認なしで送られる"
    assert panel._last_sent_generation == bridge_server.current_generation()


# --- 契約3: bridge の通知 ---


def test_execute_code_carries_no_file_switched_key_while_the_file_stays(
    bridge_tcp, window_manager
):
    first = bridge_tcp.send("execute_code", {"code": "result = 'first'"})
    second = bridge_tcp.send("execute_code", {"code": "result = 'second'"})

    assert first["ok"] is True
    assert second["ok"] is True
    # 切替が無い時はキー自体を出さない（既存の応答形を変えない）。
    assert "file_switched" not in first
    assert "file_switched" not in second


def test_execute_code_reports_the_switch_once(bridge_tcp, window_manager):
    before = bridge_tcp.send("execute_code", {"code": "result = 'before'"})
    window_manager.simulate_file_load()
    after = bridge_tcp.send("execute_code", {"code": "result = 'after'"})
    again = bridge_tcp.send("execute_code", {"code": "result = 'again'"})

    assert _file_switched(before) is None
    assert _file_switched(after) is True, "契約3: 切替が挟まったら file_switched を載せる"
    # 決めどころ A: bridge は依頼の境界を知らないので、通知は最初の1回だけ。
    assert _file_switched(again) is None


def test_a_switched_file_still_runs_the_code_and_keeps_the_response_shape(
    bridge_tcp, window_manager, tmp_path
):
    """通知は拒否ではない。journal も ACK 障壁も動かさない。"""
    marker = tmp_path / "ran-after-switch"
    before_id = "9a" * 16
    after_id = "9b" * 16
    before = bridge_tcp.send(
        "execute_code", {"code": "result = 'before'"}, request_id=before_id
    )
    window_manager.simulate_file_load()

    after = bridge_tcp.send(
        "execute_code",
        {"code": (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
            "result = 'after-switch'"
        )},
        request_id=after_id,
    )

    assert _file_switched(after) is True
    assert after["ok"] is True
    assert after["data"]["result"] == "after-switch"
    assert marker.read_text(encoding="utf-8") == "ran", (
        "契約3: 弾かない。切替が挟まってもコードは実際に走る"
    )
    assert _envelope_metadata(after) == dict(
        _envelope_metadata(before), request_id=after_id
    )
    status = bridge_tcp.send("get_request_status", {"operation_id": after_id})
    assert status["ok"] is True
    assert status["data"]["status"] == "succeeded"


def test_a_read_command_neither_carries_nor_eats_the_switch(
    bridge_tcp, window_manager, monkeypatch
):
    """読みは常に通す。通知は execute_code の応答にだけ載る。"""
    # ping は応答に scene 名を載せる。影のままでは MagicMock で JSON に載らないので、
    # 読み系コマンドが成立する最低限として名前だけ実物にする。
    monkeypatch.setattr(bpy.context.scene, "name", "Scene")

    bridge_tcp.send("execute_code", {"code": "result = 'before'"})
    window_manager.simulate_file_load()

    read = bridge_tcp.send("ping", {})
    assert read["ok"] is True
    assert _file_switched(read) is None

    after = bridge_tcp.send("execute_code", {"code": "result = 'after'"})
    assert _file_switched(after) is True, "読みが execute_code の検知を食ってはいけない"


# --- 複数の呼び手（契約1 の設計の要点: 採番だけを共有する） ---


@pytest.mark.parametrize(
    "order",
    [("panel", "bridge"), ("bridge", "panel")],
    ids=["panel-first", "bridge-first"],
)
def test_the_panel_and_the_bridge_both_notice_one_switch(
    bridge_tcp, window_manager, sent, monkeypatch, order
):
    seen = bridge_server.current_generation()
    monkeypatch.setattr(panel, "_last_sent_generation", seen)
    monkeypatch.setattr(bridge_server, "_last_exec_generation", seen)
    window_manager.simulate_file_load()

    executed = {}
    for caller in order:
        if caller == "panel":
            panel.CLAUDE_OT_send().invoke(_context(), _EVENT)
        else:
            executed["bridge"] = bridge_tcp.send(
                "execute_code", {"code": "result = 'after'"}
            )
    _settle()

    assert _confirm_calls(window_manager), (
        "契約1: bridge が先に世代を見てもパネルの確認は消えない"
    )
    assert _file_switched(executed["bridge"]) is True, (
        "契約1: パネルが先に世代を見ても bridge の通知は消えない"
    )
    assert sent == [], "確認が出ている間は送信しない"


# --- 契約4: パネル表示のズレの修復 ---
# ロードで WM プロパティは default へ戻るが、_worker はプロセス内グローバルなので
# 生き残る。表示だけ「送れる顔」に戻ると、押した先の execute() で弾かれる。
# 実態へ戻すのは timer 側の仕事（draw の中で RNA を書き換えない）。


def test_polling_restores_the_working_status_after_a_file_load(
    window_manager, waiting, monkeypatch
):
    monkeypatch.setattr(panel, "_worker", _fake_worker(alive=True))
    window_manager.claude_bridge_status = "WORKING"

    window_manager.simulate_file_load()
    assert window_manager.claude_bridge_status == "IDLE", (
        "前提: ロードで status は default へ戻り、パネルは送れる顔になる"
    )

    still_polling = panel._poll_result()

    assert window_manager.claude_bridge_status == "WORKING", (
        "契約4: ワーカーが生きている限り、表示は実態へ戻す"
    )
    assert isinstance(still_polling, float), "結果が来るまで timer は回り続ける"


@pytest.mark.parametrize(
    "worker",
    [None, _fake_worker(alive=False)],
    ids=["no-worker", "finished-worker"],
)
def test_polling_leaves_the_status_alone_without_a_live_worker(
    window_manager, waiting, monkeypatch, worker
):
    monkeypatch.setattr(panel, "_worker", worker)
    window_manager.claude_bridge_status = "DONE"

    panel._poll_result()

    assert window_manager.claude_bridge_status == "DONE", (
        "契約4: 戻すのはワーカーが生きている時だけ。"
        "終わった表示を WORKING へ貼り直さない"
    )


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [(False, "DONE"), (True, "ERROR")],
    ids=["success", "error"],
)
def test_polling_still_hands_a_ready_result_to_the_panel(
    window_manager, monkeypatch, error, expected_status
):
    """契約4 を足しても、timer 本来の役割は変わらない。

    ワーカーを生かしたまま結果を置く。復元が結果を WORKING で上書きしたら赤になる。
    """
    monkeypatch.setattr(panel, "_worker", _fake_worker(alive=True))
    monkeypatch.setattr(panel, "_result_box", {
        "ready": True,
        "text": "塔を生やしました",
        "error": error,
        "usage": "Cache reused 10 / new 7",
    })

    finished = panel._poll_result()

    assert window_manager.claude_bridge_status == expected_status
    assert window_manager.claude_bridge_reply == "塔を生やしました"
    assert window_manager.claude_bridge_usage == "Cache reused 10 / new 7"
    assert finished is None, "結果を渡したら timer は終わる"


# --- 契約3 の出口: MCP が Claude へ前置きする ---
# bridge が載せた file_switched は、ここで言葉になって初めて Claude に届く。
# 印を載せるところ（上の契約3）と、言葉にするところ（ここ）で経路が1本になる。


@pytest.fixture
def claude_reply(monkeypatch, tmp_path):
    """bridge の封筒を差し替えて、MCP が Claude へ返すテキストを取り出す。

    止め方は tests/test_execute_output.py と同じ（`BlenderBridge.send` の差し替え）。
    封筒が `ok: False` の時は、返す代わりに RuntimeError が上がる。
    """
    import bridge as bridge_client

    token_file = tmp_path / "blender-session-token"
    token_file.write_text("test-token\n", encoding="utf-8")
    monkeypatch.setattr(bridge_client, "_TOKEN_FILE", str(token_file))

    envelope = {}

    def fake_send(self, command, params=None, *args, **kwargs):
        return envelope["value"]

    monkeypatch.setattr(bridge_client.BlenderBridge, "send", fake_send)

    def run(response):
        envelope["value"] = response
        returned = server.execute_code("result = 1")
        assert len(returned) == 1 and returned[0].type == "text"
        return returned[0].text

    return run


def test_a_switch_puts_the_notice_in_front_without_touching_the_body(claude_reply):
    body = claude_reply({"ok": True, "data": {"result": "done"}})

    switched = claude_reply(
        {"ok": True, "file_switched": True, "data": {"result": "done"}}
    )

    assert switched.startswith(FILE_SWITCH_NOTICE + "\n"), (
        "契約3: 注意文は返答文の先頭に、本文と混ざらない形で立つ"
    )
    assert switched.endswith(body), "契約3: 前置きするだけ。本文は変えない"


def test_a_switch_puts_the_notice_in_front_of_the_error_too(claude_reply):
    with pytest.raises(RuntimeError) as raised:
        claude_reply(
            {
                "ok": False,
                "file_switched": True,
                "error": {"message": "bridge failed"},
            }
        )

    # 失敗の時こそ「どのファイルに対して失敗したのか」が要る。
    assert str(raised.value).startswith(FILE_SWITCH_NOTICE + "\n")
    assert str(raised.value).endswith("bridge failed")


def test_a_reply_without_a_switch_keeps_its_previous_shape(claude_reply):
    assert claude_reply({"ok": True, "data": {"result": "plain body"}}) == "plain body"


def test_an_error_without_a_switch_keeps_its_previous_wording(claude_reply):
    with pytest.raises(RuntimeError) as raised:
        claude_reply({"ok": False, "error": {"message": "bridge failed"}})

    # tests/test_scratch_tools.py が完全一致で見ている文面。前置きの導入で動かさない。
    assert str(raised.value) == "bridge failed"
