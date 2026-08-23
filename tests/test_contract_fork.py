"""外形契約: fork の指定は claude のコマンドラインに出る（票F 契約1）。

観測するのは安定境界だけ——パネルの operator（bl_idname で引く）、temp に置いた
bridge session ファイル、PATH に置いた claude の身代わりが受け取った argv。
誰がどう組み立てたかは見ない。「fork 元セッションを渡して起動したか」
「帰ってきた ID をどう書き戻したか」を、外から見えるものだけで確かめる。

同じ範囲の内側の検証は tests/test_panel_state.py が持っている。こちらは
責務分割や改名で落ちない側の網として足す（既存テストは触っていない）。
"""
import json

import bpy

from claude_bridge import panel


# Blender から見える送信ボタンの名前。クラス名ではなくこちらで引く。
_SEND_IDNAME = "claude.send_request"

_FORK_SOURCE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_STORED_SESSION = "11111111-1111-1111-1111-111111111111"
_NEW_SESSION = "22222222-2222-2222-2222-222222222222"
_PROMPT = "塔を生やして"


def _stream_json(session_id=_NEW_SESSION, text="done", is_error=False):
    """`--output-format stream-json` の出力（1行1イベント）。result だけ返す形。"""
    return (
        json.dumps(
            {
                "type": "result",
                "result": text,
                "is_error": is_error,
                "session_id": session_id,
            }
        )
        + "\n"
    )


def _send_operator():
    """送信ボタンの operator を bl_idname で引く。"""
    for registered in panel.classes:
        if getattr(registered, "bl_idname", "") == _SEND_IDNAME:
            return registered()
    raise AssertionError(f"{_SEND_IDNAME} が登録一覧に無い")


def _send(claude, timers):
    """パネルの送信ボタンを押し、結果が返るまで面倒を見る。

    Blender がやること（timer を回す）はここで肩代わりする。戻すのは
    身代わりが受け取った1回ぶんの起動記録——結果が届いた時点で、プロセスは
    もう終わっている。
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


def _resume_target(argv):
    """`-r` に続けて渡された継続元。渡していなければ None。"""
    return argv[argv.index("-r") + 1] if "-r" in argv else None


def test_a_fork_send_hands_the_fork_source_to_claude(
    bridge_tcp, bridge_session_file, panel_window_manager, fake_claude, blender_timers
):
    """契約1: fork 指定で起動される claude に、fork 元 session が渡る。"""
    bridge_session_file.write(session_id=None, fork_from=_FORK_SOURCE)
    claude = fake_claude(stdout=_stream_json())
    panel_window_manager(claude_bridge_prompt=_PROMPT)

    argv = _send(claude, blender_timers)["argv"]

    assert _resume_target(argv) == _FORK_SOURCE, (
        f"契約1: fork 元セッションを渡して起動する: {argv}"
    )
    assert "--fork-session" in argv, (
        f"契約1: 継続ではなく写しを作る指定で起動する: {argv}"
    )


def test_a_plain_resume_send_never_asks_for_a_copy(
    bridge_tcp, bridge_session_file, panel_window_manager, fake_claude, blender_timers
):
    """契約1 の対: fork 元が無ければ、自分のセッションをそのまま継続する。"""
    bridge_session_file.write(session_id=_STORED_SESSION)
    claude = fake_claude(stdout=_stream_json(session_id=_STORED_SESSION))
    panel_window_manager(claude_bridge_prompt=_PROMPT)

    argv = _send(claude, blender_timers)["argv"]

    assert _resume_target(argv) == _STORED_SESSION
    assert "--fork-session" not in argv, (
        f"契約1: 継続の送信で写しを作らせない（会話が分岐する）: {argv}"
    )


def test_a_finished_fork_becomes_the_panel_s_own_session(
    bridge_tcp, bridge_session_file, panel_window_manager, fake_claude, blender_timers
):
    """契約1: 写しが生まれたら、その ID をパネルの session として引き継ぐ。

    fork は一度きり——ここで fork_from が消えることが、次の送信をただの継続にする。
    """
    bridge_session_file.write(session_id=None, fork_from=_FORK_SOURCE)
    claude = fake_claude(stdout=_stream_json(session_id=_NEW_SESSION))
    panel_window_manager(claude_bridge_prompt=_PROMPT)

    _send(claude, blender_timers)

    stored = bridge_session_file.read()
    assert stored["session_id"] == _NEW_SESSION, (
        f"契約1: 生まれた写しの ID を引き継ぐ: {stored}"
    )
    assert stored.get("fork_from") is None, (
        f"契約1: 写しは一度だけ。fork の指定は使い切る: {stored}"
    )
    assert stored["cwd"] == bridge_session_file.cwd, "登録先の cwd は動かさない"


def test_a_failed_fork_keeps_the_fork_pending(
    bridge_tcp, bridge_session_file, panel_window_manager, fake_claude, blender_timers
):
    """契約1 の失敗時: 写しが生まれなかったのだから、fork の指定は残す。

    ここで消してしまうと、次の送信は fork 元と切れた新規セッションになる。
    """
    bridge_session_file.write(session_id=None, fork_from=_FORK_SOURCE)
    original = bridge_session_file.path.read_text(encoding="utf-8")
    claude = fake_claude(stdout=_stream_json(is_error=True), returncode=1)
    panel_window_manager(claude_bridge_prompt=_PROMPT)

    argv = _send(claude, blender_timers)["argv"]

    assert _resume_target(argv) == _FORK_SOURCE, "前提: fork として起動している"
    assert bridge_session_file.path.read_text(encoding="utf-8") == original, (
        "契約1: 失敗した送信は登録を書き換えない（fork は次の送信へ持ち越す）"
    )
