"""_build_prompt — 送信文は出所ラベル + トグル指示 + ユーザーの文のみ。

完全一致で assert する: ユーザーの書いた文へ見えない追記が混ざらないことが
このプロダクトの信頼ポイントなので、「これ以外何も無い」を形で固定する。
"""
from claude_bridge import panel


def test_prompt_is_label_plus_user_text_only():
    out = panel._build_prompt("塔を生やして")
    assert out == "[Sent from Blender]\n\n塔を生やして"


def test_directives_sit_between_label_and_user_text():
    out = panel._build_prompt("色を塗って", ["まず選択を確認", "シーンを見てから"])
    assert out == (
        "[Sent from Blender]\n"
        "- まず選択を確認\n"
        "- シーンを見てから"
        "\n\n色を塗って"
    )
