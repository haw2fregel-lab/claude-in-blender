"""外形契約: パネルの依頼は Claude へ届き、返答はパネルに出る（票F 契約2）。

一本の線を、両端だけ見て確かめる。
入口はパネルの送信ボタン（operator を bl_idname で引く）、出口は WindowManager の
プロパティ（パネルが描く面）。途中は PATH に置いた claude の身代わりが受け取った
stdin で「本当にプロセスまで届いたか」を見る。

送信はワーカースレッドで走り、結果は timer がメインスレッドへ運ぶ。Blender が居ない
ので、timer を回す役はテストが代わりに務める（登録された callback を、None が返る
まで呼ぶ——Blender と同じ約束）。
"""
import json

import bpy

from claude_bridge import bridge_server
from claude_bridge import panel


# Blender から見える送信ボタンの名前。クラス名ではなくこちらで引く。
_SEND_IDNAME = "claude.send_request"
# 送信文の頭に付く出所ラベル。operator の bl_description が「これ以外足さない」と
# 名指している、ユーザーへの約束そのもの。
_SOURCE_LABEL = "[Sent from Blender]"

_PROMPT = "塔を生やして"
_REPLY = "塔を生やしました"


def _stream_json(*events):
    """`--output-format stream-json` の出力。1行1イベントで流れてくる。"""
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


def _assistant_event(cache_read, input_tokens, cache_creation):
    """1コールぶんの usage。パネルが出すのは最終コール単体の数字。"""
    return {
        "type": "assistant",
        "message": {
            "usage": {
                "cache_read_input_tokens": cache_read,
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
            }
        },
    }


def _result_event(text=_REPLY, is_error=False):
    return {
        "type": "result",
        "result": text,
        "is_error": is_error,
        "session_id": "22222222-2222-2222-2222-222222222222",
    }


def _prompt_text(call):
    """プロセスが受け取った依頼文。

    Windows の text mode パイプは書き込み時に改行を CRLF へ直す。行末の作法は
    OS のものなので均してから見る（見たいのは「何が届いたか」）。
    """
    return call["stdin"].replace("\r\n", "\n")


def _send_operator():
    """送信ボタンの operator を bl_idname で引く。"""
    for registered in panel.classes:
        if getattr(registered, "bl_idname", "") == _SEND_IDNAME:
            return registered()
    raise AssertionError(f"{_SEND_IDNAME} が登録一覧に無い")


def _send(claude, timers):
    """送信ボタンを押し、結果がパネルへ渡るまで面倒を見る。

    戻すのは claude の身代わりが受け取った1回ぶんの起動記録。
    結果が届いた時点で、プロセスはもう終わっている（記録も揃っている）。
    """
    operator = _send_operator()
    mark = timers.mark()

    returned = operator.execute(bpy.context)

    assert returned == {"FINISHED"}, f"送信が始まらなかった: {operator.reports}"
    timers.settle(mark)
    calls = claude.calls()
    assert len(calls) == 1, (
        f"claude が1回だけ起動されるはず: {calls} / "
        f"パネルの表示: {bpy.context.window_manager.claude_bridge_reply!r}"
    )
    return calls[-1]


def _refused_send(timers):
    """送信が断られた場合。理由をユーザーへ返し、待つ物を残さない。"""
    operator = _send_operator()
    mark = timers.mark()

    returned = operator.execute(bpy.context)

    assert returned == {"CANCELLED"}
    assert operator.reports, "断るなら理由をユーザーへ返す"
    assert operator.reports[-1][0] in ({"WARNING"}, {"ERROR"}), operator.reports
    assert timers.since(mark) == [], "送っていないのだから、待つ timer も残さない"
    return operator


def test_a_panel_send_reaches_claude_and_the_reply_lands_in_the_panel(
    bridge_tcp, bridge_session_file, panel_window_manager, fake_claude, blender_timers
):
    """契約2: 依頼は Claude のプロセスまで届き、返答はパネルの表示へ入る。"""
    bridge_session_file.write(session_id="11111111-1111-1111-1111-111111111111")
    claude = fake_claude(
        stdout=_stream_json(_assistant_event(10, 2, 5), _result_event())
    )
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    call = _send(claude, blender_timers)

    assert _prompt_text(call) == f"{_SOURCE_LABEL}\n\n{_PROMPT}", (
        "契約2: 依頼はプロセスの stdin まで届く。"
        "足すのは出所ラベルだけで、見えない指示は混ぜない"
    )
    assert window_manager.claude_bridge_status == "DONE"
    assert window_manager.claude_bridge_reply == _REPLY, (
        "契約2: Claude の返答がパネルの表示用プロパティへ入る"
    )
    # 再利用 = cache_read、新規 = 通常入力 + キャッシュ作成（2 + 5）。
    assert window_manager.claude_bridge_usage == "Cache reused 10 / new 7"


def test_a_context_toggle_adds_one_visible_line_and_nothing_else(
    bridge_tcp, bridge_session_file, panel_window_manager, fake_claude, blender_timers
):
    """契約2: トグルで足されるのは、ユーザーが見て分かる1行だけ。"""
    bridge_session_file.write(session_id="11111111-1111-1111-1111-111111111111")
    claude = fake_claude(stdout=_stream_json(_result_event()))
    panel_window_manager(
        claude_bridge_prompt="色を塗って", claude_bridge_ctx_selection=True
    )

    sent = _prompt_text(_send(claude, blender_timers))

    head, separator, body = sent.partition("\n\n")
    assert separator, f"出所ラベル・指示と依頼文の間は空行で切る: {sent!r}"
    assert body == "色を塗って", "契約2: ユーザーの文はそのまま届く"
    label, *directives = head.split("\n")
    assert label == _SOURCE_LABEL
    assert len(directives) == 1, f"入れたトグルは1つ: {directives}"
    assert directives[0].startswith("- "), f"指示は箇条書きで見える形: {directives[0]!r}"
    assert "get_selection" in directives[0], (
        f"Selection のトグルは選択を見る指示を足す: {directives[0]!r}"
    )


def test_a_failing_claude_run_shows_the_error_in_the_panel(
    bridge_tcp, bridge_session_file, panel_window_manager, fake_claude, blender_timers
):
    """契約2 の失敗時: 失敗も表示へ届く。黙って IDLE に戻らない。"""
    bridge_session_file.write(session_id="11111111-1111-1111-1111-111111111111")
    claude = fake_claude(
        stdout=_stream_json(_result_event(text="boom", is_error=True)), returncode=1
    )
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    _send(claude, blender_timers)

    assert window_manager.claude_bridge_status == "ERROR", (
        "契約2: 失敗した送信は ERROR として表示に残る"
    )
    assert "boom" in window_manager.claude_bridge_reply, (
        f"契約2: 失敗の中身もユーザーへ見せる: {window_manager.claude_bridge_reply!r}"
    )


def test_an_empty_request_is_refused_before_claude_is_started(
    bridge_tcp, bridge_session_file, panel_window_manager, fake_claude, blender_timers
):
    """契約2 の失敗時: 中身の無い依頼で Claude を起こさない。"""
    bridge_session_file.write(session_id="11111111-1111-1111-1111-111111111111")
    claude = fake_claude(stdout=_stream_json(_result_event()))
    panel_window_manager(claude_bridge_prompt="   ")

    _refused_send(blender_timers)

    assert claude.calls() == [], "契約2: 空の依頼はプロセスまで運ばない"


def test_a_send_without_a_running_bridge_never_starts_claude(
    bridge_session_file, panel_window_manager, fake_claude, blender_timers
):
    """契約2 の失敗時: 受け口が止まっている間は送らない。

    送っても Claude から Blender を触れないので、待たせるより先に断る。
    """
    bridge_server.stop_server()  # 受け口が止まっている状態を、公開 API で作る
    bridge_session_file.write(session_id="11111111-1111-1111-1111-111111111111")
    claude = fake_claude(stdout=_stream_json(_result_event()))
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    _refused_send(blender_timers)

    assert claude.calls() == [], "契約2: 受け口が無い時はプロセスを起こさない"
    assert window_manager.claude_bridge_status == "IDLE", (
        "送っていないのだから、処理中の顔にはしない"
    )
