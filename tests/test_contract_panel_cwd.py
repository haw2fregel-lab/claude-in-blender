"""契約テスト: パネルの CWD をユーザーの作業リポへ（作業票 C-1 / C-1b / C-3 / C-5）。

叩くのは票が interface として名指した4つの関数（C-1b）と、送信の入口だけ。
期待値は実装ではなく契約の行から起こしている。

- `_resolve_paths` / `_recent_cwds`: 登録 dict から cwd・repo・履歴を決める解決規則（C-1）
- `_build_mcp_config`: `--mcp-config` へ渡す JSON 文字列の組み立て（C-3）
- `push_recent`: 作業ディレクトリの履歴の積み方（C-1 / E-2）
- 送信の入口: 止めると決めた四条件（C-5）と、`.mcp.json` の無い作業リポでの送信（C-1）
- パネルの描画: 作業ディレクトリ選択が無効になる条件（C-4）
- 起動の argv: Skill Tool の入口と、許可するスキルの範囲
- 打ち切り（timeout）: 届いていた session_id と本文を捨てない

登録の cwd は「Claude を起動する作業ディレクトリ」、repo は「アドオンのソースリポ」。
この二つが別物になったのがこの案件で、テストもその二つを別のディレクトリで組む。
"""
import json
import re
import subprocess
from pathlib import Path

import bpy
import pytest

from claude_bridge import bridge_server
from claude_bridge import panel
from tools import bridge_register


# パネルが組む mcp config のサーバー名（C-3 の literal）。
_SERVER_NAME = "claude-in-blender"

# 一目でどちらのパスか分かる名前にしておく。C-5 の文言に載る `<path>` を、
# 区切り文字の作法に依らず見分けるため。
_WORK_NAME = "user-work-repo"
_REPO_NAME = "addon-source-repo"


def _posix(path):
    """登録は posix 区切りで書かれる（tools/bridge_register.py と同じ作法）。"""
    return str(path).replace("\\", "/")


def _work_path(tmp_path):
    """ユーザーが普段仕事するリポの登録値。作るかどうかは呼ぶ側が決める。"""
    return _posix(tmp_path / _WORK_NAME)


def _create_work_dir(tmp_path):
    """作業ディレクトリを実際に置く。`.mcp.json` は置かない（C-1: 要らない）。"""
    (tmp_path / _WORK_NAME).mkdir(parents=True, exist_ok=True)
    return _work_path(tmp_path)


def _repo_path(tmp_path):
    """アドオンのソースリポの登録値。"""
    return _posix(tmp_path / _REPO_NAME)


def _create_addon_repo(tmp_path, with_server=True):
    """アドオンのソースリポを置く。在り処の目印は `mcp_server/server.py`（C-1）。"""
    (tmp_path / _REPO_NAME / "mcp_server").mkdir(parents=True, exist_ok=True)
    if with_server:
        (tmp_path / _REPO_NAME / "mcp_server" / "server.py").write_text(
            "", encoding="utf-8"
        )
    return _repo_path(tmp_path)


# --- C-1 解決規則: 登録 dict から cwd と repo を決める ---
# 新しい登録は cwd（働く場所）と repo（アドオンの在り処）を別々に持つ。
# repo を持たない既存の登録は、cwd を repo とみなしてそのまま動く（後方互換）。


@pytest.mark.parametrize(
    ("bridge", "expected"),
    [
        pytest.param(
            {"cwd": "D:/user-work", "repo": "D:/addon-source"},
            ("D:/user-work", "D:/addon-source"),
            id="both-keys",
        ),
        pytest.param(
            {"cwd": "D:/addon-source"},
            ("D:/addon-source", "D:/addon-source"),
            id="repo-absent-falls-back-to-cwd",
        ),
        # キー欠落と null は等価（登録は get で読む）——既存の登録まわりと同じ読み方。
        pytest.param(
            {"cwd": "D:/addon-source", "repo": None},
            ("D:/addon-source", "D:/addon-source"),
            id="repo-null-is-the-same-as-absent",
        ),
        pytest.param({}, (None, None), id="neither-key"),
    ],
)
def test_resolve_paths_reads_cwd_and_repo_with_the_backward_compatible_fallback(
    bridge, expected
):
    assert panel._resolve_paths(bridge) == expected


def test_resolve_paths_reports_no_cwd_when_only_the_repo_is_registered():
    """C-5 の「cwd が無い」を、呼ぶ側が見分けられること。

    repo だけの登録で repo 側が何を返すか（そのまま返すか、cwd と一緒に落とすか）は
    票が決めていないので見ない。決まっているのは「cwd は決まらない」の方。
    """
    cwd, _repo = panel._resolve_paths({"repo": "D:/addon-source"})

    assert cwd is None


# --- C-1 履歴の読み: recent_cwds が無い登録でも一覧は空にしない ---


@pytest.mark.parametrize(
    ("bridge", "expected"),
    [
        pytest.param(
            {"cwd": "D:/a", "recent_cwds": ["D:/a", "D:/b", "D:/c"]},
            ["D:/a", "D:/b", "D:/c"],
            id="history-as-registered",
        ),
        pytest.param({"cwd": "D:/a"}, ["D:/a"], id="no-history-means-just-the-cwd"),
        pytest.param(
            {"cwd": "D:/a", "recent_cwds": None},
            ["D:/a"],
            id="null-history-is-the-same-as-absent",
        ),
        pytest.param({}, [], id="no-cwd-no-history"),
    ],
)
def test_recent_cwds_falls_back_to_the_registered_cwd(bridge, expected):
    assert panel._recent_cwds(bridge) == expected


# --- C-3 mcp config: パネルが組む JSON 文字列 ---
# 一時ファイルは作らない。args は repo からの絶対パスにする——cwd がどこであっても、
# アドオンのサーバーは repo の下に居るため。


def test_build_mcp_config_returns_a_json_string_pointing_at_the_repo_server(tmp_path):
    repo = _repo_path(tmp_path)

    config = panel._build_mcp_config(repo, "D:/tools/python.exe")

    assert isinstance(config, str), "C-3: --mcp-config は JSON 文字列を受ける"
    parsed = json.loads(config)
    assert set(parsed["mcpServers"]) == {_SERVER_NAME}, (
        f"C-3: 立てるのはアドオンのサーバー一つ: {parsed}"
    )
    server = parsed["mcpServers"][_SERVER_NAME]
    assert server["command"] == "D:/tools/python.exe", (
        "C-3: command は _python_for_mcp の解決結果をそのまま使う"
    )
    assert len(server["args"]) == 1, f"渡すのは server.py 一つ: {server['args']}"
    argument = Path(server["args"][0])
    assert argument.is_absolute(), f"C-3: args は絶対パス: {server['args'][0]!r}"
    assert argument == Path(repo) / "mcp_server" / "server.py", (
        f"C-3: args は repo からの絶対パス: {server['args'][0]!r}"
    )


def test_build_mcp_config_defaults_the_command_to_python(tmp_path):
    """C-3: python が決まらない時は `python`（`.mcp.json` の既定展開と同じ）。"""
    config = json.loads(panel._build_mcp_config(_repo_path(tmp_path), None))

    assert config["mcpServers"][_SERVER_NAME]["command"] == "python"


# --- C-1 / E-2 履歴の積み方: 先頭が最新、上限 5 件、重複なし ---
# 書き換えは渡した data の中で起きる（in-place）。どのテストも呼び出しの後に
# data を読み直しており、戻り値は当てにしていない。


def test_push_recent_puts_the_newest_cwd_at_the_head():
    data = {"cwd": "D:/b", "recent_cwds": ["D:/b", "D:/c"]}

    bridge_register.push_recent(data, "D:/a")

    assert data["recent_cwds"] == ["D:/a", "D:/b", "D:/c"]


def test_push_recent_starts_a_history_for_a_registration_that_has_none():
    """後方互換: recent_cwds を持たない既存の登録にも積める（C-1 / 完了条件6）。"""
    data = {"cwd": "D:/a", "session_id": "11111111-1111-1111-1111-111111111111"}

    bridge_register.push_recent(data, "D:/a")

    assert data["recent_cwds"] == ["D:/a"]
    assert data["session_id"] == "11111111-1111-1111-1111-111111111111", (
        "積むのは履歴だけ。ほかのキーは触らない"
    )


def test_push_recent_promotes_a_repeated_cwd_instead_of_duplicating_it():
    data = {"recent_cwds": ["D:/a", "D:/b", "D:/c"]}

    bridge_register.push_recent(data, "D:/c")

    assert data["recent_cwds"] == ["D:/c", "D:/a", "D:/b"]


def test_push_recent_drops_the_oldest_beyond_the_default_limit_of_five():
    data = {"recent_cwds": ["D:/a", "D:/b", "D:/c", "D:/d", "D:/e"]}

    bridge_register.push_recent(data, "D:/f")

    assert data["recent_cwds"] == ["D:/f", "D:/a", "D:/b", "D:/c", "D:/d"]


def test_push_recent_honours_a_smaller_limit():
    data = {"recent_cwds": ["D:/a", "D:/b", "D:/c"]}

    bridge_register.push_recent(data, "D:/x", limit=2)

    assert data["recent_cwds"] == ["D:/x", "D:/a"]


# --- C-5 送信前に止まる四条件 ---
# 見るのは二つ——claude を起こさないことと、理由がユーザーへ届くこと。
# 止め方（operator が断るか、ワーカーがエラー表示にするか）は票が決めていないので、
# ユーザーの目に入る面（operator の report とパネルの表示）を合わせて見る。
# 文言は条件を見分けるところだけ照らす。言い回しの細部は窓口が調整する余地を残す。

_SEND_IDNAME = "claude.send_request"
_PROMPT = "塔を生やして"


def _result_stdout(text="やりました"):
    """`--output-format stream-json` の最小の出力。1行1イベント。"""
    return (
        json.dumps(
            {
                "type": "result",
                "result": text,
                "is_error": False,
                "session_id": "22222222-2222-2222-2222-222222222222",
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def _register(bridge_session_file, **fields):
    """登録ファイルを、この案件のスキーマ（C-1）でそのまま置く。

    conftest の `write` は cwd を必ず書くが、ここでは「cwd が無い登録」も作りたいので
    payload は丸ごとこちらで決める。置き場の付け替えは fixture のまま（CLAUDE_CONFIG_DIR）。
    """
    path = bridge_session_file.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _send_operator():
    """送信ボタンの operator を bl_idname で引く。"""
    for registered in panel.classes:
        if getattr(registered, "bl_idname", "") == _SEND_IDNAME:
            return registered()
    raise AssertionError(f"{_SEND_IDNAME} が登録一覧に無い")


def _press_send(timers):
    """送信ボタンを押し、登録された timer を Blender の代わりに終わりまで回す。

    断られた場合は timer が無いこともある。あってもなくても、残さず片付けてから返す。
    """
    operator = _send_operator()
    mark = timers.mark()

    operator.execute(bpy.context)

    for callback in timers.since(mark):
        assert timers.run(callback), "timer が結果を渡さないまま残った"
    return operator


def _user_visible(operator, window_manager):
    """ユーザーの目に入った文字。operator の report とパネルの表示を合わせて見る。"""
    return "\n".join(
        [message for _level, message in operator.reports]
        + [str(window_manager.claude_bridge_reply)]
    )


def test_a_send_without_a_registration_stops_before_claude_starts(
    bridge_tcp, bridge_session_file, panel_window_manager, fake_claude, blender_timers
):
    """C-5: 登録が無い。文言は今までどおり、setup へ案内する。"""
    assert not bridge_session_file.path.exists()
    claude = fake_claude(stdout=_result_stdout())
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    operator = _press_send(blender_timers)

    assert claude.calls() == [], "C-5: 登録が無ければ claude を起こさない"
    assert "Not set up" in _user_visible(operator, window_manager)


def test_a_registration_without_a_work_directory_stops_before_claude_starts(
    bridge_tcp,
    bridge_session_file,
    panel_window_manager,
    fake_claude,
    blender_timers,
    tmp_path,
):
    """C-5: cwd が無い。アドオンの在り処が分かっても、働く場所が無ければ送らない。"""
    _register(bridge_session_file, repo=_create_addon_repo(tmp_path))
    claude = fake_claude(stdout=_result_stdout())
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    operator = _press_send(blender_timers)

    assert claude.calls() == [], "C-5: 働く場所が決まらないまま送らない"
    assert "work directory not registered" in _user_visible(operator, window_manager)


@pytest.mark.parametrize(
    "repo_directory_exists",
    [True, False],
    ids=["server-file-missing", "repo-directory-gone"],
)
def test_a_repo_without_the_addon_server_stops_before_claude_starts(
    bridge_tcp,
    bridge_session_file,
    panel_window_manager,
    fake_claude,
    blender_timers,
    tmp_path,
    repo_directory_exists,
):
    """C-5: `repo/mcp_server/server.py` が無い。リポを移した後の登録がこれ。

    判定の基準は `.mcp.json` ではなくアドオンのサーバー本体（C-1 / D-4）。
    """
    repo = (
        _create_addon_repo(tmp_path, with_server=False)
        if repo_directory_exists
        else _repo_path(tmp_path)  # 作らない = リポごと動いた／消えた
    )
    _register(bridge_session_file, cwd=_create_work_dir(tmp_path), repo=repo)
    claude = fake_claude(stdout=_result_stdout())
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    operator = _press_send(blender_timers)

    message = _user_visible(operator, window_manager)
    assert claude.calls() == [], "C-5: サーバーが無い設定で claude を起こさない"
    assert "add-on source not found" in message
    assert _REPO_NAME in message, f"C-5: どこを見に行ったかを出す: {message!r}"


def test_a_missing_work_directory_stops_the_send_without_dropping_the_history(
    bridge_tcp,
    bridge_session_file,
    panel_window_manager,
    fake_claude,
    blender_timers,
    tmp_path,
):
    """C-5 / D-3: cwd が消えていたら送信前に止める。履歴からは消さない。

    外付けドライブが外れているだけかもしれないので、一覧からは落とさない。
    """
    gone = _work_path(tmp_path)  # 登録はあるが、ディレクトリは作らない
    kept = _posix(tmp_path / "another-work-repo")
    _register(
        bridge_session_file,
        cwd=gone,
        repo=_create_addon_repo(tmp_path),
        recent_cwds=[gone, kept],
    )
    claude = fake_claude(stdout=_result_stdout())
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    operator = _press_send(blender_timers)

    message = _user_visible(operator, window_manager)
    assert claude.calls() == [], "C-5: 無い場所では働けないので、送る前に止める"
    assert "work directory not found" in message
    assert _WORK_NAME in message, f"C-5: どのパスが無かったのかを出す: {message!r}"
    assert bridge_session_file.read()["recent_cwds"] == [gone, kept], (
        "D-3: 止めるだけ。履歴は残す"
    )


# --- C-1 / C-3 正常系: `.mcp.json` の無い作業リポで動く（完了条件 1・2）---


def _mcp_config(argv):
    """argv から `--mcp-config` へ渡された JSON を取り出す。

    別引数で渡すか `=` で繋ぐかは票が決めていないので、どちらでも拾う。
    """
    for index, argument in enumerate(argv):
        if argument == "--mcp-config":
            return json.loads(argv[index + 1])
        if argument.startswith("--mcp-config="):
            return json.loads(argument.split("=", 1)[1])
    raise AssertionError(f"C-3: --mcp-config が渡っていない: {argv}")


def test_a_send_runs_in_the_registered_work_directory_with_the_repo_server(
    bridge_tcp,
    bridge_session_file,
    panel_window_manager,
    fake_claude,
    blender_timers,
    tmp_path,
):
    """C-1 / C-3: `.mcp.json` の無い作業リポで立ち、MCP は repo の server.py を指す。"""
    work = _create_work_dir(tmp_path)
    repo = _create_addon_repo(tmp_path)
    assert not (Path(work) / ".mcp.json").exists(), "C-1: cwd に .mcp.json は要らない"
    _register(bridge_session_file, cwd=work, repo=repo, recent_cwds=[work])
    claude = fake_claude(stdout=_result_stdout())
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    operator = _press_send(blender_timers)

    calls = claude.calls()
    assert len(calls) == 1, (
        f"送信が claude まで届かなかった: {_user_visible(operator, window_manager)!r}"
    )
    assert Path(calls[0]["cwd"]).resolve() == Path(work).resolve(), (
        "C-1: セッションはユーザーの作業リポで動く"
    )
    server = _mcp_config(calls[0]["argv"])["mcpServers"][_SERVER_NAME]
    assert Path(server["args"][0]) == Path(repo) / "mcp_server" / "server.py", (
        f"C-3: MCP サーバーは repo の下から起動する: {server['args']}"
    )


# --- C-4 作業ディレクトリ選択の無効化 ---
# 切り替えはセッションを外す操作なので、接続中は選ばせない——会話を切る意思表示
# （Disconnect）を先に人へ通すため。送信処理中（WORKING）も従来どおり無効。
# 見るのは `claude.pick_cwd` を置いた行の enabled だけ。ほかの行の enabled は契約の外。

_PICK_CWD_IDNAME = "claude.pick_cwd"
_STORED_SESSION = "11111111-1111-1111-1111-111111111111"
_FORK_SOURCE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _FakeProps:
    """`layout.operator()` などの戻り。実装が何を読んでも代入しても受ける器。"""

    def __getattr__(self, name):
        return None


class _FakeUiElement:
    """`draw()` が書き込む layout / row / column / box の身代わり。

    Blender の UI は「行を作る → その行の enabled を決める → 行へ中身を置く」順で組む。
    無効化は行に立つので、行ごとに `enabled` を持ち、そこへ置かれた操作を覚える。
    入れ子は親まで辿って解く——無効な行の中に置いたものは Blender でも無効に見える。

    集めるのは `operator` / `operator_menu_enum` に渡った bl_idname だけ。
    ほかの呼び出し（label / prop / separator など）は受けるだけで数えない。
    """

    def __init__(self, parent=None, placements=None):
        self.enabled = True  # RNA と同じ既定。実装が代入した値をそのまま覚える
        self._parent = parent
        self._placements = [] if placements is None else placements

    # --- 実装が呼ぶ側 ---

    def row(self, *_args, **_kwargs):
        return _FakeUiElement(parent=self, placements=self._placements)

    column = row
    box = row
    split = row
    column_flow = row
    grid_flow = row

    def operator(self, operator, *_args, **_kwargs):
        self._placements.append((operator, self))
        return _FakeProps()

    def operator_menu_enum(self, operator, *_args, **_kwargs):
        self._placements.append((operator, self))
        return _FakeProps()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *_args, **_kwargs: _FakeProps()

    # --- テストが読む側 ---

    def _effective_enabled(self):
        own = bool(self.enabled)
        if self._parent is None:
            return own
        return own and self._parent._effective_enabled()


def _draw_panel(context=None):
    """パネルを一度描き、書き込まれた layout を返す。"""
    panel_class = panel.CLAUDE_PT_panel
    assert isinstance(panel_class, type), (
        "パネルの draw をテストから呼ぶには、bpy.types.Panel が本物のクラスであること。"
        "MagicMock を基底にしたクラス定義はクラスではなく mock になり、draw の中身が"
        "走らない（tests/conftest.py が bpy.types.Operator へしているのと同じ形が要る）"
    )
    layout = _FakeUiElement()
    drawn = panel_class()
    drawn.layout = layout
    drawn.draw(bpy.context if context is None else context)
    return layout


def _row_enabled(layout, idname):
    """`idname` を置いた行の enabled。親まで辿って解いた結果を返す。"""
    rows = [row for placed, row in layout._placements if placed == idname]
    assert len(rows) == 1, (
        f"{idname} を置いた行がちょうど1つではない: "
        f"{[placed for placed, _row in layout._placements]}"
    )
    return rows[0]._effective_enabled()


def test_the_layout_fake_records_enabled_per_row():
    """土台: 行ごとの enabled と入れ子の解け方。ここがズレると下の契約テストは嘘になる。"""
    layout = _FakeUiElement()
    layout.row().operator_menu_enum("claude.plain_row", "value")
    disabled = layout.row()
    disabled.enabled = False
    disabled.operator_menu_enum("claude.disabled_row", "value")
    disabled.column().row().operator_menu_enum("claude.inside_disabled_row", "value")

    assert _row_enabled(layout, "claude.plain_row") is True
    assert _row_enabled(layout, "claude.disabled_row") is False
    assert _row_enabled(layout, "claude.inside_disabled_row") is False


@pytest.fixture
def drawable_panel(monkeypatch, panel_window_manager, bridge_session_file, tmp_path):
    """パネルが普通に描かれる状態を組む factory。

    受け口が動いているのが、ユーザーがパネルを見ている普通の状態
    （tests/test_file_switch.py と同じく `is_running` だけ立てて描画に集中する）。
    履歴は2件入れておく——選ぶ先がある状態で、選べるかどうかを見たいので。
    """
    monkeypatch.setattr(bridge_server, "is_running", lambda: True)

    def install(status, **stored):
        _register(
            bridge_session_file,
            cwd=_create_work_dir(tmp_path),
            repo=_create_addon_repo(tmp_path),
            recent_cwds=[_work_path(tmp_path), _posix(tmp_path / "another-work-repo")],
            **stored,
        )
        return panel_window_manager(claude_bridge_status=status)

    return install


@pytest.mark.parametrize(
    ("stored", "status", "expected_enabled"),
    [
        pytest.param({}, "IDLE", True, id="disconnected-and-idle"),
        pytest.param(
            {"session_id": None, "fork_from": None},
            "IDLE",
            True,
            id="null-connection-keys-are-the-same-as-absent",
        ),
        pytest.param(
            {"session_id": _STORED_SESSION}, "IDLE", False, id="connected-by-session_id"
        ),
        pytest.param(
            {"fork_from": _FORK_SOURCE}, "IDLE", False, id="connected-by-fork_from"
        ),
        pytest.param({}, "WORKING", False, id="sending"),
        pytest.param(
            {"session_id": _STORED_SESSION},
            "WORKING",
            False,
            id="connected-and-sending",
        ),
        pytest.param({}, "DONE", True, id="a-finished-send-is-not-sending"),
        pytest.param({}, "ERROR", True, id="a-failed-send-is-not-sending"),
    ],
)
def test_the_work_directory_picker_is_disabled_while_connected_or_sending(
    drawable_panel, stored, status, expected_enabled
):
    drawable_panel(status, **stored)

    layout = _draw_panel()

    assert _row_enabled(layout, _PICK_CWD_IDNAME) is expected_enabled, (
        "C-4: 接続中（session_id か fork_from がある）と送信処理中（WORKING）は"
        "作業ディレクトリを選べない。どちらでもない時は選べる"
    )


# --- 起動の argv: Skill の入口と、許可するスキルの範囲 ---
# `--tools` の値が空文字だと組み込み Tool が全部落ちる（公式ヘルプ: "" で all tools を
# disable）。Skill は組み込みの `Skill` Tool から実行されるので、値が空のままだと
# リポに SKILL.md を置いても本文へ辿り着く経路が無い。
# `--allowedTools` は「許可する旗」であって「使える Tool を増やす旗」ではないので、
# そこへ書いても `--tools` で落とされた Tool は戻らない——だから両方を見る。
#
# 固定するのは入口が開いていることだけ。読むかどうかはモデルの判断で、確実に読ませる
# 指示は依頼文へ足さない（見えない追記をしない、の既存方針）。
# 並び順と渡し方は実装の自由。載っているかどうかだけを見る。

_MODELING_SKILL = "Skill(blender-modeling)"
_BLENDER_TOOLS = "mcp__claude-in-blender__*"
# 開発者向けスキル。パネルの利用者が、意図せず開発フロー（アドオンの焼き直し等）へ
# 入らないよう、パネルからの起動では許可しない。
_DEVELOPER_SKILLS = ("blender-setup", "blender-update", "blender-bridge")


def _flag_value(argv, flag):
    """`--flag value` / `--flag=value` のどちらでも値を取る。"""
    for index, argument in enumerate(argv):
        if argument == flag:
            value = argv[index + 1] if index + 1 < len(argv) else ""
            assert not value.startswith("-"), f"{flag} に値が付いていない: {argv}"
            return value
        if argument.startswith(flag + "="):
            return argument.split("=", 1)[1]
    raise AssertionError(f"{flag} が渡っていない: {argv}")


def _allowed_tools(argv):
    """`--allowedTools` へ渡った値を、ひと続きのテキストにして返す。

    渡し方（1つの文字列 / 複数の値 / 旗の繰り返し / `=` 連結）は実装の自由なので、
    どの形も同じテキストとして読む。
    """
    flag = "--allowedTools"
    collected = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == flag:
            index += 1
            while index < len(argv) and not argv[index].startswith("-"):
                collected.append(argv[index])
                index += 1
            continue
        if argument.startswith(flag + "="):
            collected.append(argument.split("=", 1)[1])
        index += 1
    assert collected, f"{flag} が渡っていない: {argv}"
    return "\n".join(collected)


def _names_the_tool(text, name):
    """Tool 名が単体の語として並びに出るか。区切り（カンマ / 空白）は問わない。"""
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def _allows_every_skill(text):
    """スキル名を伴わない `Skill` の許可。これがあると、どのスキルも通る。"""
    return re.search(r"(?<![\w-])Skill(?!\s*\()(?![\w-])", text) is not None


@pytest.fixture
def sent_argv(
    bridge_tcp,
    bridge_session_file,
    panel_window_manager,
    fake_claude,
    blender_timers,
    tmp_path,
):
    """一度だけ送信して、claude が受け取った argv を返す。

    身代わりの claude は起動のたびに argv を1件積む（conftest）。ここで見るのは
    その1件だけで、送信そのものの筋（cwd / mcp config）は上のテストが見ている。
    """
    _register(
        bridge_session_file,
        cwd=_create_work_dir(tmp_path),
        repo=_create_addon_repo(tmp_path),
    )
    claude = fake_claude(stdout=_result_stdout())
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    operator = _press_send(blender_timers)

    calls = claude.calls()
    assert len(calls) == 1, (
        f"送信が claude まで届かなかった: {_user_visible(operator, window_manager)!r}"
    )
    return calls[0]["argv"]


def test_the_panel_leaves_the_skill_tool_switched_on(sent_argv):
    """契約: `--tools` が組み込みの Skill Tool を名指す。"""
    tools = _flag_value(sent_argv, "--tools")

    assert tools != "", (
        "空文字は組み込み Tool を全部落とす。Skill Tool ごと消えると、"
        "SKILL.md を置いても本文へ辿り着く経路が無くなる"
    )
    assert _names_the_tool(tools, "Skill"), (
        f"契約: モデリングの作法を読む入口として Skill Tool を残す: {tools!r}"
    )


def test_the_panel_allows_the_modeling_skill_and_the_blender_tools(sent_argv):
    """契約: 残した Skill Tool を、モデリングのスキルへ通す。MCP tool は従来どおり。"""
    allowed = _allowed_tools(sent_argv)

    assert _MODELING_SKILL in allowed, (
        f"契約: モデリングの作法を書いたスキルを許可する: {allowed!r}"
    )
    assert _BLENDER_TOOLS in allowed, (
        f"従来どおり、Blender を触る MCP tool を許可する: {allowed!r}"
    )


def test_the_panel_keeps_the_developer_skills_out_of_the_allowed_tools(sent_argv):
    """契約: パネルの利用者が、意図せず開発フローへ入らないこと。

    許可を名指しの1件に留めるのも同じ理由——スキル名を伴わない `Skill` の許可は、
    リポにある開発者向けスキルまで一緒に通してしまう。
    """
    allowed = _allowed_tools(sent_argv)

    for skill in _DEVELOPER_SKILLS:
        assert skill not in allowed, (
            f"契約: 開発者向けスキル {skill} はパネルからは許可しない: {allowed!r}"
        )
    assert not _allows_every_skill(allowed), (
        f"許可はスキルを名指しで（{_MODELING_SKILL}）。裸の Skill は全部を通す: {allowed!r}"
    )


def test_the_panel_keeps_the_mcp_config_strict(sent_argv):
    """変えないこと: リポの `.mcp.json` を混ぜず、パネルが渡した config だけで立てる。"""
    assert "--strict-mcp-config" in sent_argv, sent_argv


# --- 打ち切り（timeout）: 届いていたものを捨てない ---
# 実測で起きたこと: モデリング依頼が `_CLAUDE_TIMEOUT_SEC` を超え、Blender 側の作業は
# 適用済みなのに、応答も session_id も消えて `No session connected` へ戻った。
# 育った会話が捨てられる。
#
# 拾える根拠が二つある。
#   - `session_id` は `result` だけでなく全イベント（1行目の `system`）に載る（実測）
#   - `subprocess.run(timeout=...)` は打ち切りの時、そこまでに読めた出力を
#     `TimeoutExpired.output` へ入れて上げる
# だから打ち切りでも、会話の続き先と届いていた本文は分かる。
#
# 秒数の正本は `_CLAUDE_TIMEOUT_SEC`。テストは数値そのものを固定せず、
# 表示がその正本から来ていることだけを見る（差し替えた値が表示に出るか）。

_NEW_SESSION = "22222222-2222-2222-2222-222222222222"
_ARRIVED_TEXT = "屋根まで組みました"
# 既定（300）と紛れない値。表示に出た数字が、差し替えたこの値だと分かるように。
_TEST_TIMEOUT_SEC = 123


def _event(**fields):
    return json.dumps(fields, ensure_ascii=False)


def _system_line(session_id):
    """stream-json の1行目。session_id はここから既に載っている。"""
    return _event(type="system", subtype="init", session_id=session_id)


def _assistant_line(text, session_id, cache_read=0, input_tokens=0, cache_creation=0):
    """assistant イベント1件。

    本文と usage の在り処は公式の stream-json（Anthropic の message 形）に合わせる——
    本文は `message.content` の text ブロック、usage は `message.usage`。
    usage の読み方は既存の表示と同じ（再利用 = cache_read、新規 = 通常入力 + 作成）。
    """
    return _event(
        type="assistant",
        session_id=session_id,
        message={
            "content": [{"type": "text", "text": text}],
            "usage": {
                "cache_read_input_tokens": cache_read,
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    )


def _truncated_stream(session_id=_NEW_SESSION, text=_ARRIVED_TEXT):
    """打ち切られた stream-json。最後の行は書きかけのまま切れている。"""
    return (
        _system_line(session_id) + "\n"
        + _assistant_line("下ごしらえします", session_id, 4, 1, 1) + "\n"
        + _assistant_line(text, session_id, 10, 2, 5) + "\n"
        + '{"type":"assistant","mess'  # 改行が来る前に切れた行
    )


# --- 打ち切りの単体: 届いた分から何が読めるか ---


def test_partial_from_stream_reads_the_session_the_text_and_the_last_usage():
    session_id, text, usage = panel._partial_from_stream(_truncated_stream())

    assert session_id == _NEW_SESSION, (
        "契約: session_id は result を待たずに拾える（1行目の system に載っている）"
    )
    assert "下ごしらえします" in text and _ARRIVED_TEXT in text, (
        f"契約: 届いていた本文は全部拾う: {text!r}"
    )
    # 最終コール単体の数字（既存の表示と同じ見方）。最初のコールなら 4 / 2 になる。
    assert panel._format_usage(usage) == "Cache reused 10 / new 7", (
        f"契約: usage は最終コールのもの: {usage!r}"
    )


def test_partial_from_stream_reports_the_session_before_any_assistant_text():
    """system だけ届いた段階でも、会話の続き先は分かる。"""
    session_id, text, usage = panel._partial_from_stream(_system_line(_NEW_SESSION) + "\n")

    assert session_id == _NEW_SESSION
    assert text == ""
    assert usage == {}


def test_partial_from_stream_takes_only_a_uuid_as_the_session_id():
    """自由形式の ID は掴まない（外から来た ID の扱いは既存の fork 契約と同じ見方）。"""
    outside_id = "environment-session-12345678"
    stream = (
        _system_line(outside_id) + "\n" + _assistant_line(_ARRIVED_TEXT, outside_id) + "\n"
    )

    session_id, text, _usage = panel._partial_from_stream(stream)

    assert session_id is None
    assert _ARRIVED_TEXT in text, "ID を採らないだけで、届いた本文は捨てない"


@pytest.mark.parametrize(
    "stdout",
    [None, "", "\n", "not json at all\n", '{"type":"assistant","mess'],
    ids=["none", "empty", "blank-line", "garbage", "cut-mid-line"],
)
def test_partial_from_stream_returns_nothing_when_no_event_arrived_whole(stdout):
    assert panel._partial_from_stream(stdout) == (None, "", {})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(_NEW_SESSION, True, id="uuid"),
        pytest.param("3f2504e0-4f89-11d3-9a0c-0305e82c3301", True, id="mixed-hex-uuid"),
        pytest.param(None, False, id="null"),
        pytest.param("", False, id="empty"),
        pytest.param("not-a-uuid", False, id="free-form"),
        pytest.param(
            "environment-session-12345678", False, id="externally-registered-id"
        ),
    ],
)
def test_is_session_id_accepts_only_the_uuid_shape(value, expected):
    assert panel._is_session_id(value) is expected


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        pytest.param({}, "", id="nothing-to-show"),
        # 再利用 = cache_read、新規 = 通常入力 + キャッシュ作成（2 + 5）。
        pytest.param(
            {
                "cache_read_input_tokens": 10,
                "input_tokens": 2,
                "cache_creation_input_tokens": 5,
            },
            "Cache reused 10 / new 7",
            id="reused-and-new",
        ),
    ],
)
def test_format_usage_shows_the_reused_and_the_new_input(usage, expected):
    assert panel._format_usage(usage) == expected


# --- 打ち切りの外形: 送信経路を打ち切って、登録と表示を見る ---
# 身代わりの claude（conftest）は起動されるとすぐ終わるので、実プロセスでは打ち切りを
# 起こせない。打ち切りが生まれる場所は `subprocess.run` なので、そこを既存の送信系と
# 同じ形（tests/test_panel_state.py の run 差し替え）で例外に替える。


def _stub_timeout(monkeypatch, stdout, seconds=_TEST_TIMEOUT_SEC):
    """claude の起動を打ち切りへ差し替える。渡された cmd 列を順に記録して返す。"""
    calls = []

    def fake_run(*args, **kwargs):
        cmd = list(args[0] if args else kwargs.get("args", []))
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, seconds, output=stdout)

    monkeypatch.setattr(panel, "_CLAUDE_TIMEOUT_SEC", seconds)
    monkeypatch.setattr(panel, "_find_claude", lambda _bridge: "claude")
    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    return calls


def _register_for_send(bridge_session_file, tmp_path, **fields):
    return _register(
        bridge_session_file,
        cwd=_create_work_dir(tmp_path),
        repo=_create_addon_repo(tmp_path),
        **fields,
    )


def test_a_timed_out_send_keeps_the_session_and_the_text_that_arrived(
    bridge_tcp,
    bridge_session_file,
    panel_window_manager,
    blender_timers,
    monkeypatch,
    tmp_path,
):
    _register_for_send(bridge_session_file, tmp_path, session_id=None)
    _stub_timeout(monkeypatch, _truncated_stream())
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    _press_send(blender_timers)

    assert bridge_session_file.read()["session_id"] == _NEW_SESSION, (
        "契約1: 打ち切られても、届いていた session_id を保存して会話を繋ぎ直せるようにする"
    )
    reply = window_manager.claude_bridge_reply
    assert _ARRIVED_TEXT in reply, f"契約3: 届いていた本文はパネルに出す: {reply!r}"
    assert str(_TEST_TIMEOUT_SEC) in reply, (
        f"契約3: 打ち切りだと分かる表示は残す。秒数は _CLAUDE_TIMEOUT_SEC が正本: {reply!r}"
    )
    assert window_manager.claude_bridge_status == "ERROR", (
        "契約4: 途中で切れた仕事なので、エラー扱いのまま"
    )
    assert window_manager.claude_bridge_usage == "Cache reused 10 / new 7", (
        "契約5: 届いていた usage はコスト表示へ反映する"
    )


def test_the_next_send_after_a_timeout_resumes_the_recovered_session(
    bridge_tcp,
    bridge_session_file,
    panel_window_manager,
    blender_timers,
    monkeypatch,
    tmp_path,
):
    """契約1 の狙い: 拾った ID で、次の送信がその会話の続きになる。"""
    _register_for_send(bridge_session_file, tmp_path, session_id=None)
    calls = _stub_timeout(monkeypatch, _truncated_stream())
    window_manager = panel_window_manager(claude_bridge_prompt=_PROMPT)

    _press_send(blender_timers)
    window_manager.claude_bridge_prompt = "続きをお願い"
    _press_send(blender_timers)

    assert len(calls) == 2, f"2回目の送信が claude まで届かなかった: {calls}"
    resumed = calls[-1]
    assert "-r" in resumed and resumed[resumed.index("-r") + 1] == _NEW_SESSION, (
        f"契約1: 打ち切りで拾った会話の続きとして送る: {resumed}"
    )


@pytest.mark.parametrize(
    "stdout",
    [None, "", _system_line(_STORED_SESSION) + "\n"],
    ids=["no-output", "nothing-arrived", "same-session"],
)
def test_a_timed_out_send_leaves_the_registration_alone_when_there_is_nothing_new(
    bridge_tcp,
    bridge_session_file,
    panel_window_manager,
    blender_timers,
    monkeypatch,
    tmp_path,
    stdout,
):
    path = _register_for_send(bridge_session_file, tmp_path, session_id=_STORED_SESSION)
    original = path.read_text(encoding="utf-8")
    _stub_timeout(monkeypatch, stdout)
    panel_window_manager(claude_bridge_prompt=_PROMPT)

    _press_send(blender_timers)

    assert bridge_session_file.read()["session_id"] == _STORED_SESSION, (
        "契約2: 育っていた会話の ID を、打ち切りで消したり書き換えたりしない"
    )
    assert path.read_text(encoding="utf-8") == original, (
        "契約2: 拾えた ID が既存と同じ、または一行も届いていない時は保存しに行かない"
    )
