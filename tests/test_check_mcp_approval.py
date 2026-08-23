"""check_mcp_approval — CLI 契約（キー突き合わせ・status・exit code・--fix の書き換え）だけを踏むテスト。"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_mcp_approval.py"
SERVER = "claude-in-blender"
BACKUP_PREFIX = ".claude.json.bak-"
OTHER_PROJECT = "D:/some/other/project"


def _forms(path):
    # 契約: resolve した絶対パスを、フォワード形とバック形の両方でキーに突き合わせる。
    resolved = str(Path(path).resolve())
    return resolved.replace("\\", "/"), resolved.replace("/", "\\")


def _repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    return repo


def _entry(trust=None, servers=None):
    # 実物の .claude.json のエントリは他のキーも持つ。--fix はそれらに触らない契約。
    entry = {"allowedTools": []}
    if trust is not None:
        entry["hasTrustDialogAccepted"] = trust
    if servers is not None:
        entry["enabledMcpjsonServers"] = list(servers)
    return entry


def _write(tmp_path, payload):
    # 本物の ~/.claude.json には触らない。テストデータは必ず tmp_path 下。
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return claude_json


def _run(claude_json, repo=None, fix=False, cwd=None):
    args = [sys.executable, str(SCRIPT), "--claude-json", str(claude_json)]
    if repo is not None:
        args += ["--repo", str(repo)]
    if fix:
        args.append("--fix")
    completed = subprocess.run(
        args,
        cwd=str(cwd if cwd is not None else ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.stdout, (
        f"stdout が空（契約は JSON 一発）: exit={completed.returncode} stderr={completed.stderr}"
    )
    return completed, json.loads(completed.stdout)


def _backups(tmp_path):
    # 契約のバックアップ名 .claude.json.bak-<timestamp>（衝突回避の連番等が付くことがある）。
    return sorted(str(path) for path in tmp_path.rglob(BACKUP_PREFIX + "*"))


def test_ok_environment_reports_ok_with_the_five_contract_keys(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {"projects": {forward: _entry(trust=True, servers=[SERVER])}},
    )

    completed, payload = _run(claude_json, repo=repo)

    assert set(payload) == {"status", "claude_json", "entries", "fixed", "backup"}
    assert payload["status"] == "ok"
    assert completed.returncode == 0
    assert Path(payload["claude_json"]) == claude_json.resolve()
    assert payload["entries"] == {
        forward: {"hasTrustDialogAccepted": True, "serverEnabled": True}
    }
    assert payload["fixed"] is False
    assert payload["backup"] is None


# 冪等: 両条件が揃っている環境では --fix でも書き換えず、バックアップも作らない。
def test_ok_environment_is_untouched_by_fix(tmp_path):
    repo = _repo(tmp_path)
    forward, back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {
            "projects": {
                forward: _entry(trust=True, servers=[SERVER]),
                back: _entry(trust=True, servers=[SERVER, "other-server"]),
            }
        },
    )
    before = claude_json.read_text(encoding="utf-8")

    completed, payload = _run(claude_json, repo=repo, fix=True)

    assert payload["status"] == "ok"
    assert completed.returncode == 0
    assert payload["fixed"] is False
    assert payload["backup"] is None
    assert claude_json.read_text(encoding="utf-8") == before
    assert _backups(tmp_path) == []


@pytest.mark.parametrize(
    "entry, expected",
    [
        (
            _entry(trust=False, servers=[SERVER]),
            {"hasTrustDialogAccepted": False, "serverEnabled": True},
        ),
        (
            _entry(trust=True),
            {"hasTrustDialogAccepted": True, "serverEnabled": False},
        ),
        (
            _entry(trust=True, servers=["other-server"]),
            {"hasTrustDialogAccepted": True, "serverEnabled": False},
        ),
        (
            _entry(),
            {"hasTrustDialogAccepted": False, "serverEnabled": False},
        ),
    ],
    ids=[
        "trust-false",
        "servers-key-missing",
        "server-name-missing",
        "both-missing",
    ],
)
def test_missing_is_detected_for_each_gap(tmp_path, entry, expected):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(tmp_path, {"projects": {forward: entry}})

    completed, payload = _run(claude_json, repo=repo)

    assert payload["status"] == "missing"
    assert completed.returncode == 0
    assert payload["entries"] == {forward: expected}
    assert payload["fixed"] is False
    assert payload["backup"] is None


# --fix 無しは read-only 検知。missing でもファイルには一切触らない。
def test_missing_without_fix_never_writes(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(tmp_path, {"projects": {forward: _entry()}})
    before = claude_json.read_text(encoding="utf-8")

    completed, payload = _run(claude_json, repo=repo)

    assert payload["status"] == "missing"
    assert completed.returncode == 0
    assert payload["fixed"] is False
    assert payload["backup"] is None
    assert claude_json.read_text(encoding="utf-8") == before
    assert _backups(tmp_path) == []


# 両スラッシュ形のキーが並んでいる時は、見つかった全該当エントリに書く。
def test_fix_writes_every_matched_entry_in_both_slash_forms(tmp_path):
    repo = _repo(tmp_path)
    forward, back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {
            "installMethod": "native",
            "projects": {
                OTHER_PROJECT: _entry(trust=False, servers=[]),
                forward: _entry(trust=False, servers=[]),
                back: _entry(trust=True),
            },
        },
    )

    completed, payload = _run(claude_json, repo=repo, fix=True)
    data = json.loads(claude_json.read_text(encoding="utf-8"))

    assert completed.returncode == 0
    # --fix 成功後は書き換え後を再検査した状態を返す（窓口裁定 2026-08-17）。
    assert payload["status"] == "ok"
    assert set(payload["entries"]) == {forward, back}
    assert payload["fixed"] is True
    assert payload["backup"] is not None
    for key in (forward, back):
        assert data["projects"][key]["hasTrustDialogAccepted"] is True
        assert SERVER in data["projects"][key]["enabledMcpjsonServers"]
        assert data["projects"][key]["allowedTools"] == []
    # 他プロジェクトのエントリ・他のトップレベルキーには触らない。
    assert data["projects"][OTHER_PROJECT] == _entry(trust=False, servers=[])
    assert set(data["projects"]) == {OTHER_PROJECT, forward, back}
    assert data["installMethod"] == "native"


# append: 既に居る他サーバーは消さず、末尾に "claude-in-blender" を足す。
def test_fix_appends_the_server_without_dropping_existing_ones(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {
            "projects": {
                forward: _entry(trust=False, servers=["other-server", "another-server"])
            }
        },
    )

    _completed, payload = _run(claude_json, repo=repo, fix=True)
    data = json.loads(claude_json.read_text(encoding="utf-8"))

    assert payload["fixed"] is True
    assert data["projects"][forward]["enabledMcpjsonServers"] == [
        "other-server",
        "another-server",
        SERVER,
    ]
    assert data["projects"][forward]["hasTrustDialogAccepted"] is True


# 既に居れば触らない——重複追加しない。
def test_fix_does_not_duplicate_a_server_name_already_present(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {"projects": {forward: _entry(trust=False, servers=[SERVER])}},
    )

    _completed, payload = _run(claude_json, repo=repo, fix=True)
    data = json.loads(claude_json.read_text(encoding="utf-8"))

    assert payload["fixed"] is True
    assert data["projects"][forward]["enabledMcpjsonServers"] == [SERVER]
    assert data["projects"][forward]["hasTrustDialogAccepted"] is True


# no-entry: projects エントリの新設はしない（Claude Code 未使用環境を汚さない）。
@pytest.mark.parametrize("fix", [False, True], ids=["read-only", "with-fix"])
def test_no_entry_creates_nothing(tmp_path, fix):
    repo = _repo(tmp_path)
    claude_json = _write(
        tmp_path,
        {"projects": {OTHER_PROJECT: _entry(trust=True, servers=[SERVER])}},
    )
    before = claude_json.read_text(encoding="utf-8")

    completed, payload = _run(claude_json, repo=repo, fix=fix)

    assert payload["status"] == "no-entry"
    assert completed.returncode == 0
    assert payload["entries"] == {}
    assert payload["fixed"] is False
    assert payload["backup"] is None
    assert claude_json.read_text(encoding="utf-8") == before
    assert _backups(tmp_path) == []


# no-file: .claude.json が無い環境にファイルを作らない。
@pytest.mark.parametrize("fix", [False, True], ids=["read-only", "with-fix"])
def test_no_file_creates_nothing(tmp_path, fix):
    repo = _repo(tmp_path)
    claude_json = tmp_path / ".claude.json"

    completed, payload = _run(claude_json, repo=repo, fix=fix)

    assert payload["status"] == "no-file"
    assert completed.returncode == 0
    assert Path(payload["claude_json"]) == claude_json.resolve()
    assert payload["entries"] == {}
    assert payload["fixed"] is False
    assert payload["backup"] is None
    assert not claude_json.exists()
    assert _backups(tmp_path) == []


# broken: parse 不能なら直そうとせず報告のみ。exit code は broken だけ 2。
@pytest.mark.parametrize("fix", [False, True], ids=["read-only", "with-fix"])
def test_broken_json_reports_broken_with_exit_2(tmp_path, fix):
    repo = _repo(tmp_path)
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text('{"projects": {"a": }\n', encoding="utf-8")
    before = claude_json.read_text(encoding="utf-8")

    completed, payload = _run(claude_json, repo=repo, fix=fix)

    assert payload["status"] == "broken"
    assert completed.returncode == 2
    assert payload["entries"] == {}
    assert payload["fixed"] is False
    assert payload["backup"] is None
    assert claude_json.read_text(encoding="utf-8") == before
    assert _backups(tmp_path) == []


# 修復時は必ずバックアップが先——中身は書き換え前の元内容。
def test_backup_holds_the_content_from_before_the_fix(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {"projects": {forward: _entry(trust=False, servers=[])}},
    )
    before = claude_json.read_text(encoding="utf-8")

    _completed, payload = _run(claude_json, repo=repo, fix=True)

    backup = Path(payload["backup"])
    assert payload["fixed"] is True
    assert re.match(r"^\.claude\.json\.bak-\d{8}-\d{6}", backup.name)
    assert backup.parent == claude_json.parent
    assert backup.read_text(encoding="utf-8") == before
    assert claude_json.read_text(encoding="utf-8") != before


# 同名衝突は回避される——二度目の --fix が一度目のバックアップを潰さない。
def test_a_second_fix_does_not_clobber_the_first_backup(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {"projects": {forward: _entry(trust=False, servers=[])}},
    )
    first_source = claude_json.read_text(encoding="utf-8")

    _completed, first = _run(claude_json, repo=repo, fix=True)

    claude_json.write_text(
        json.dumps(
            {"projects": {forward: _entry(trust=False, servers=["other-server"])}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    second_source = claude_json.read_text(encoding="utf-8")

    _completed, second = _run(claude_json, repo=repo, fix=True)

    assert first["fixed"] is True
    assert second["fixed"] is True
    assert first["backup"] != second["backup"]
    assert Path(first["backup"]).read_text(encoding="utf-8") == first_source
    assert Path(second["backup"]).read_text(encoding="utf-8") == second_source
    assert len(_backups(tmp_path)) == 2


# 完全一致が無い時は、大文字小文字無視の一致で拾う。
def test_case_only_difference_in_the_key_is_still_matched(tmp_path):
    repo = _repo(tmp_path, name="RepoCaseDir")
    forward, back = _forms(repo)
    odd_key = forward.upper()
    assert odd_key not in {forward, back}

    claude_json = _write(
        tmp_path,
        {"projects": {odd_key: _entry(trust=False, servers=[SERVER])}},
    )

    completed, payload = _run(claude_json, repo=repo)

    assert payload["status"] == "missing"
    assert completed.returncode == 0
    assert payload["entries"] == {
        odd_key: {"hasTrustDialogAccepted": False, "serverEnabled": True}
    }
    assert payload["fixed"] is False


def test_fix_writes_the_case_insensitive_match(tmp_path):
    repo = _repo(tmp_path, name="RepoCaseDir")
    forward, _back = _forms(repo)
    odd_key = forward.upper()
    claude_json = _write(
        tmp_path,
        {"projects": {odd_key: _entry(trust=False, servers=["other-server"])}},
    )

    _completed, payload = _run(claude_json, repo=repo, fix=True)
    data = json.loads(claude_json.read_text(encoding="utf-8"))

    assert payload["fixed"] is True
    # キーの綴りは変えない（エントリ新設もしない）。
    assert set(data["projects"]) == {odd_key}
    assert data["projects"][odd_key]["hasTrustDialogAccepted"] is True
    assert data["projects"][odd_key]["enabledMcpjsonServers"] == ["other-server", SERVER]


# 完全一致優先——完全一致がある時は case 違いのキーを拾わない。
def test_exact_key_wins_over_a_case_variant(tmp_path):
    repo = _repo(tmp_path, name="RepoCaseDir")
    forward, _back = _forms(repo)
    odd_key = forward.upper()
    claude_json = _write(
        tmp_path,
        {
            "projects": {
                forward: _entry(trust=True, servers=[SERVER]),
                odd_key: _entry(trust=False, servers=[]),
            }
        },
    )

    completed, payload = _run(claude_json, repo=repo)

    assert set(payload["entries"]) == {forward}
    assert payload["status"] == "ok"
    assert completed.returncode == 0


# --repo 省略時は cwd を対象にする。
def test_repo_defaults_to_the_current_directory(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {"projects": {forward: _entry(trust=True, servers=[SERVER])}},
    )

    completed, payload = _run(claude_json, cwd=repo)

    assert payload["status"] == "ok"
    assert completed.returncode == 0
    assert set(payload["entries"]) == {forward}


# --- 安全化の契約: 不明・競合では自動修復せず止まる／差し替えは temp + os.replace ---
#
# ここから下は、ユーザー共通の ~/.claude.json を壊さない側の契約を踏む。
# 割り込みや差し替えの瞬間を作るために、CLI を薄い皮（python -c）ごしに走らせる。
# 皮は stdout へ何も書かない——契約の「JSON 一行」を汚さない。


# 対象ファイルを読み終えた直後——契約の「読み込み後〜書き込み前」の入口——に、
# 別プロセスの書き換えを一度だけ差し込む皮。
# 前提: CLI は対象ファイルの解析を json 経由で行う（json.load も内部で json.loads を呼ぶ）。
INTRUDE_AFTER_READ = '''\
import json
import runpy
import sys
from pathlib import Path

script, target, intrusion = sys.argv[1:4]
sys.argv = [script] + sys.argv[4:]
target_path = Path(target)
pending = [Path(intrusion).read_text(encoding="utf-8")]
real_loads = json.loads


def loads(*args, **kwargs):
    parsed = real_loads(*args, **kwargs)
    if pending and isinstance(parsed, dict) and "projects" in parsed:
        target_path.write_text(pending.pop(), encoding="utf-8")
    return parsed


json.loads = loads
runpy.run_path(script, run_name="__main__")
'''


# os.replace の呼ばれ方を記録する皮。mode="crash" なら、対象ファイルへの差し替えの瞬間に落ちる。
WATCH_REPLACE = '''\
import json
import os
import runpy
import sys
from pathlib import Path

script, mode, record, target = sys.argv[1:5]
sys.argv = [script] + sys.argv[5:]
record_path = Path(record)
target_path = Path(target).resolve()
calls = []
real_replace = os.replace


def replace(src, dst, **kwargs):
    calls.append({"src": str(src), "dst": str(dst)})
    record_path.write_text(json.dumps(calls), encoding="utf-8")
    if mode == "crash" and Path(dst).resolve() == target_path:
        raise OSError("simulated crash at the swap")
    return real_replace(src, dst, **kwargs)


os.replace = replace
record_path.write_text("[]", encoding="utf-8")
runpy.run_path(script, run_name="__main__")
'''


def _raw_servers_entry(value):
    # _entry は list() に通すので、未知 schema の生値はここで直に置く。
    entry = _entry(trust=False)
    entry["enabledMcpjsonServers"] = value
    return entry


def _run_hooked(hook, hook_args, claude_json, repo, fix=True):
    # 皮の argv 規約: [script, *皮の引数, *CLI の引数]。皮が script を runpy で起動する。
    args = [
        sys.executable,
        "-c",
        hook,
        str(SCRIPT),
        *hook_args,
        "--claude-json",
        str(claude_json),
        "--repo",
        str(repo),
    ]
    if fix:
        args.append("--fix")
    return subprocess.run(
        args, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8"
    )


def _payload(completed):
    assert completed.stdout, (
        f"stdout が空（契約は JSON 一発）: exit={completed.returncode} stderr={completed.stderr}"
    )
    return json.loads(completed.stdout)


# 未知 schema は「なんとなく動く」のが一番危ない: 文字列と dict は `in` が素通りし、
# 数値は `in` が TypeError で落ちる。どれも直そうとせず、書かずに止まるのが契約。
@pytest.mark.parametrize(
    "servers",
    [SERVER, {SERVER: True}, 7],
    ids=["string", "dict", "int"],
)
def test_unsupported_servers_type_stops_the_fix_without_writing(tmp_path, servers):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(tmp_path, {"projects": {forward: _raw_servers_entry(servers)}})
    before = claude_json.read_text(encoding="utf-8")

    completed, payload = _run(claude_json, repo=repo, fix=True)

    assert payload["status"] == "unsupported-schema"
    assert completed.returncode == 0
    assert set(payload) == {"status", "claude_json", "entries", "fixed", "backup"}
    assert payload["fixed"] is False
    assert payload["backup"] is None
    assert claude_json.read_text(encoding="utf-8") == before
    assert _backups(tmp_path) == []


# --fix 無しは元から read-only。未知 schema でもそこは変わらない。
# read-only 時の status は契約に無いので、ここでは固定しない（窓口の裁定待ち）。
def test_unsupported_servers_type_without_fix_leaves_the_file_alone(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(tmp_path, {"projects": {forward: _raw_servers_entry(SERVER)}})
    before = claude_json.read_text(encoding="utf-8")

    completed, payload = _run(claude_json, repo=repo)

    assert completed.returncode == 0
    # 裁定（2026-08-23）: 未知 schema は --fix の有無によらず入力状態の札として返す。
    assert payload["status"] == "unsupported-schema"
    assert payload["fixed"] is False
    assert payload["backup"] is None
    assert claude_json.read_text(encoding="utf-8") == before
    assert _backups(tmp_path) == []


# 境界: 止める対象は「list でも None でもない」値。null はその外側にいる。
# null をどう直すかは契約に無いので、未知 schema 扱いしないことだけを見る。
def test_a_null_servers_value_is_not_an_unsupported_schema(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(tmp_path, {"projects": {forward: _raw_servers_entry(None)}})

    completed, payload = _run(claude_json, repo=repo, fix=True)

    assert completed.returncode == 0
    assert payload["status"] != "unsupported-schema"


# 読み込みと書き込みの間に別プロセスが書いていたら、その上に被せず conflict で降りる。
def test_a_change_between_read_and_write_is_refused_as_conflict(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {"projects": {forward: _entry(trust=False, servers=[])}},
    )
    # 別プロセスが割り込んで書く内容。CLI が書かなければ、これがそのまま残る。
    intruded_text = (
        json.dumps(
            {
                "projects": {
                    forward: _entry(trust=False, servers=[]),
                    OTHER_PROJECT: _entry(trust=True, servers=[SERVER]),
                }
            },
            indent=2,
        )
        + "\n"
    )
    intrusion = tmp_path / "intrusion.json"
    intrusion.write_text(intruded_text, encoding="utf-8")

    completed = _run_hooked(
        INTRUDE_AFTER_READ, [str(claude_json), str(intrusion)], claude_json, repo
    )
    payload = _payload(completed)

    assert payload["status"] == "conflict", (
        f"読み込み後に変わっていたら書かずに降りる契約: {payload}"
    )
    assert completed.returncode == 0
    assert set(payload) == {"status", "claude_json", "entries", "fixed", "backup"}
    assert payload["fixed"] is False
    # public な観測: 対象ファイルは割り込んだ側の内容のまま。CLI は一文字も書いていない。
    assert claude_json.read_text(encoding="utf-8") == intruded_text


# 書き込みは同一ディレクトリの一時ファイル → os.replace。対象ファイルを直接開いて書かない。
def test_fix_swaps_the_file_in_with_os_replace_from_the_same_directory(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {
            "installMethod": "native",
            "projects": {
                OTHER_PROJECT: _entry(trust=True, servers=[SERVER]),
                forward: _entry(trust=False, servers=["other-server"]),
            },
        },
    )
    record = tmp_path / "replace-calls.json"

    completed = _run_hooked(
        WATCH_REPLACE, ["record", str(record), str(claude_json)], claude_json, repo
    )
    payload = _payload(completed)
    calls = json.loads(record.read_text(encoding="utf-8"))
    swaps = [
        call for call in calls if Path(call["dst"]).resolve() == claude_json.resolve()
    ]
    data = json.loads(claude_json.read_text(encoding="utf-8"))

    assert completed.returncode == 0
    assert payload["fixed"] is True
    assert swaps, f"対象ファイルへの os.replace が無い（直書きの合図）: {calls}"
    for call in swaps:
        assert Path(call["src"]).resolve().parent == claude_json.resolve().parent
    # 結果整合: 差し替わったファイルは有効な JSON で、文書が丸ごとそろっている。
    assert data["installMethod"] == "native"
    assert data["projects"][OTHER_PROJECT] == _entry(trust=True, servers=[SERVER])
    assert data["projects"][forward]["hasTrustDialogAccepted"] is True
    assert data["projects"][forward]["enabledMcpjsonServers"] == ["other-server", SERVER]


# 電源断の代役: 差し替えの瞬間に落ちても、対象ファイルは元のまま丸ごと残る。
def test_a_crash_at_the_swap_leaves_the_original_file_whole(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {
            "installMethod": "native",
            "projects": {forward: _entry(trust=False, servers=["other-server"])},
        },
    )
    before = claude_json.read_text(encoding="utf-8")
    record = tmp_path / "replace-calls.json"

    _completed = _run_hooked(
        WATCH_REPLACE, ["crash", str(record), str(claude_json)], claude_json, repo
    )
    after = claude_json.read_text(encoding="utf-8")

    # 落ちた後の姿だけを見る（落ち方の報告は契約の外）。
    assert after == before, (
        "差し替えで落ちても対象ファイルは元のまま——temp + os.replace ならこれは自明。"
        " 崩れるなら、対象ファイルを直接開いて書いている合図。"
    )
    assert json.loads(after)["installMethod"] == "native"


# 元ファイルの permission mode を差し替え後も保つ。
# 0o640 は mkstemp の既定（0o600）とも umask 022 の既定（0o644）とも違う——写している証拠になる。
@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX の permission mode を持たない"
)
def test_fix_keeps_the_permission_mode_of_the_target_file(tmp_path):
    repo = _repo(tmp_path)
    forward, _back = _forms(repo)
    claude_json = _write(
        tmp_path,
        {"projects": {forward: _entry(trust=False, servers=[])}},
    )
    claude_json.chmod(0o640)

    completed, payload = _run(claude_json, repo=repo, fix=True)

    assert completed.returncode == 0
    assert payload["fixed"] is True
    assert claude_json.stat().st_mode & 0o777 == 0o640
