"""外形契約: .blend の切替は execute_code の封筒に出る（票F 契約3）。

観測点は TCP の封筒だけ。世代を数える仕組みには触らず、Claude が実際に受け取る
応答へ `file_switched` が載るかどうかを外から見る。

状態の作り方も外形で揃える——起点は「1回実行した後」に置く。こうすると、
前のテストが残した世代を引きずっても、この一連の中の切替だけが観測できる。

知らせるだけで弾かない、が設計の方針。だから「実行は通る」ことも一緒に固定する。
（世代の採番そのものと、MCP 出口の文面は tests/test_file_switch.py が持つ。）
"""


def _switched(response):
    """封筒に載った切替の印。切替が無い時はキー自体が来ない。"""
    return response.get("file_switched")


def test_execute_code_reports_a_blend_switch_once_in_the_envelope(
    bridge_tcp, fake_window_manager
):
    """契約3: 切替を挟んだ最初の execute_code の応答に file_switched が載る。"""
    window_manager = fake_window_manager(claude_bridge_generation=0)
    bridge_tcp.send("execute_code", {"code": "result = 'warm-up'"})  # 起点を揃える
    steady = bridge_tcp.send("execute_code", {"code": "result = 'steady'"})

    window_manager.simulate_file_load()  # 別の .blend を開く

    after = bridge_tcp.send("execute_code", {"code": "result = 'after'"})
    again = bridge_tcp.send("execute_code", {"code": "result = 'again'"})

    assert "file_switched" not in steady, (
        f"契約3: 同じファイルで居る間は印を出さない: {steady}"
    )
    assert _switched(after) is True, f"契約3: 切替が挟まったら知らせる: {after}"
    assert after["ok"] is True and after["data"]["result"] == "after", (
        "契約3: 知らせるだけで弾かない。切替の後もコードは走る"
    )
    assert "file_switched" not in again, (
        f"契約3: 知らせるのは切替のたびに1回。次からは黙る: {again}"
    )


def test_a_failing_execute_after_a_switch_still_carries_the_mark(
    bridge_tcp, fake_window_manager
):
    """契約3 の失敗時: 失敗の封筒にも印を載せる。

    切替の直後こそ「名前が無い」類の失敗が起きる。理由の手がかりを落とさない。
    """
    window_manager = fake_window_manager(claude_bridge_generation=0)
    bridge_tcp.send("execute_code", {"code": "result = 'baseline'"})

    window_manager.simulate_file_load()

    failed = bridge_tcp.send("execute_code", {"code": "raise ValueError('boom')"})

    assert failed["ok"] is False
    assert _switched(failed) is True, (
        f"契約3: 失敗した応答にも切替の印を載せる: {failed}"
    )


def test_saving_the_same_file_is_not_reported_as_a_switch(
    bridge_tcp, fake_window_manager
):
    """契約3 の境界: 保存は切替ではない。ここで印が出ると、保存のたびに誤報が出る。"""
    window_manager = fake_window_manager(claude_bridge_generation=0)
    bridge_tcp.send("execute_code", {"code": "result = 'baseline'"})

    window_manager.simulate_file_save()

    after_save = bridge_tcp.send("execute_code", {"code": "result = 'after-save'"})

    assert "file_switched" not in after_save, (
        f"契約3: 保存では知らせない（同じファイルのまま）: {after_save}"
    )
