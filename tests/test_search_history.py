import builtins
import errno
import inspect
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from mcp_server import server


LINE_RE = re.compile(
    r"^\[(?P<session>[^\s\]]{8}) (?P<date>\d\d/\d\d|--/--) (?P<time>\d\d:\d\d|--:--) "
    r"(?P<role>user|assistant)\] (?P<excerpt>.*)$"
)

# サマリ行の契約は「searched の数・skip の数・match の数が先頭1行から読める」ことで、
# 文面そのものは一字一句まで決まっていない。だから数と手掛かりの語だけを拾う。
SUMMARY_SEARCHED_RE = re.compile(r"searched\D{0,3}(\d+)\s*of\s*(\d+)", re.IGNORECASE)
SUMMARY_SKIPPED_RE = re.compile(r"skipped\D{0,3}(\d+)", re.IGNORECASE)
SUMMARY_MATCHES_RE = re.compile(
    r"(\d+)\s*match(?:es)?\b|match(?:es)?\D{0,3}(\d+)", re.IGNORECASE
)
SIZE_REASON_RE = re.compile(r"large|size|mib|mb", re.IGNORECASE)
READ_REASON_RE = re.compile(r"unread|read|parse|decode", re.IGNORECASE)

# 開示されるだけで変わらない上限（契約「変えないこと」）。
FILE_LIMIT = 10


@dataclass
class Summary:
    line: str
    searched: int
    total: int
    skipped: int
    matches: Optional[int]


def summary(output):
    assert output.strip(), "expected output, got an empty string"
    line = output.splitlines()[0]
    searched = SUMMARY_SEARCHED_RE.search(line)
    assert searched is not None, f"first line lacks 'searched N of M': {line!r}"
    skipped = SUMMARY_SKIPPED_RE.search(line)
    matches = SUMMARY_MATCHES_RE.search(line)
    return Summary(
        line=line,
        searched=int(searched[1]),
        total=int(searched[2]),
        skipped=int(skipped[1]) if skipped else 0,
        matches=int(matches[1] or matches[2]) if matches else None,
    )


def assert_searched_is_consistent(head):
    # searched が「窓に載せた数」か「読み切った数」かは契約が決めていないので、
    # どちらの読み方でも成り立つ幅だけを踏む（skip が無ければ両者は一致する）。
    window = min(FILE_LIMIT, head.total)
    assert window - head.skipped <= head.searched <= window, head.line


def message(role, content, timestamp=None, **extra):
    record = {"type": role, "message": {"role": role, "content": content}}
    if timestamp is not None:
        record["timestamp"] = timestamp
    record.update(extra)
    return record


def parsed(output):
    head, *body = output.splitlines()
    assert LINE_RE.match(head) is None, f"expected a summary line first, got: {head!r}"
    summary(output)
    entries = []
    for line in body:
        match = LINE_RE.match(line)
        assert match is not None, f"unexpected output line: {line!r}"
        entries.append(match)
    return entries


def excerpts(output):
    return [entry["excerpt"] for entry in parsed(output)]


@dataclass
class History:
    directory: Path

    def write(self, name, records, mtime=None):
        path = self.directory / name
        lines = [record if isinstance(record, str) else json.dumps(record) for record in records]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


@pytest.fixture
def history(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    projects = tmp_path / "projects"
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path.cwd()))
    directory = projects / slug
    directory.mkdir(parents=True)
    monkeypatch.setattr(server, "_PROJECTS_ROOT", projects)

    return History(directory)


@pytest.fixture
def unreadable(monkeypatch):
    """名前で指定したファイルだけ、開こうとすると失敗するようにする factory。

    権限ビットで読めなくする手は Windows では効かないので、読み取り経路
    （`builtins.open` と、pathlib が内側で呼ぶ `io.open`）を包んで、その1本だけ
    落とす。他のファイルは素通りする。
    """
    real_open = io.open
    failures = {}

    def guarded(file, *args, **kwargs):
        try:
            name = os.fsdecode(file)
        except TypeError:
            name = None
        if name is not None:
            error = failures.get(os.path.basename(name))
            if error is not None:
                raise error
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", guarded)
    monkeypatch.setattr(builtins, "open", guarded)

    def block(name, error=None):
        failures[name] = error or OSError(errno.EACCES, "unreadable session file", name)

    return block


def test_matches_are_case_insensitive_and_require_every_query_term(history):
    history.write(
        "session1-aaaa.jsonl",
        [
            message("user", "add a Bevel modifier to the Cube"),
            message("user", "bevel without the other term"),
            message("assistant", "the cube without the other term"),
        ],
    )

    assert excerpts(server.search_session_history("BEVEL cube")) == [
        "add a Bevel modifier to the Cube"
    ]
    assert len(excerpts(server.search_session_history("bevel"))) == 2


def test_reads_string_content_and_text_blocks_from_both_roles(history):
    history.write(
        "session2-bbbb.jsonl",
        [
            message("user", "plain string carrot"),
            message("user", [{"type": "text", "text": "user text block carrot"}]),
            message("assistant", [{"type": "text", "text": "assistant text block carrot"}]),
            message(
                "assistant",
                [
                    {"type": "text", "text": "leading block"},
                    {"type": "text", "text": "trailing block potato"},
                ],
            ),
        ],
    )

    found = excerpts(server.search_session_history("carrot"))
    assert len(found) == 3
    assert set(found) == {
        "plain string carrot",
        "user text block carrot",
        "assistant text block carrot",
    }

    joined = excerpts(server.search_session_history("potato"))
    assert len(joined) == 1
    assert "potato" in joined[0]


def test_ignores_tool_blocks_meta_lines_and_other_record_types(history):
    history.write(
        "session3-cccc.jsonl",
        [
            message(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "needle inside a tool result",
                    }
                ],
            ),
            message(
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "needle inside a tool use"},
                    }
                ],
            ),
            message("user", "needle inside a meta line", isMeta=True),
            {
                "type": "system",
                "message": {"role": "system", "content": "needle inside a system record"},
            },
            message("assistant", "needle inside a normal reply", isMeta=False),
        ],
    )

    assert excerpts(server.search_session_history("needle")) == ["needle inside a normal reply"]


def test_newest_file_and_newest_line_come_first(history):
    history.write(
        "olderrrr-1111.jsonl",
        [
            message("user", "anchor older first"),
            message("user", "anchor older second"),
        ],
        mtime=1_600_000_000,
    )
    history.write(
        "newerrrr-2222.jsonl",
        [
            message("user", "anchor newer first"),
            message("user", "anchor newer second"),
        ],
        mtime=1_700_000_000,
    )

    assert excerpts(server.search_session_history("anchor")) == [
        "anchor newer second",
        "anchor newer first",
        "anchor older second",
        "anchor older first",
    ]


def test_only_the_ten_newest_files_are_searched(history):
    for index in range(12):
        history.write(
            f"cap{index:05d}-session.jsonl",
            [message("user", f"capneedle {index:02d}")],
            mtime=1_600_000_000 + index,
        )

    found = excerpts(server.search_session_history("capneedle", max_results=20))

    assert found == [f"capneedle {index:02d}" for index in range(11, 1, -1)]


def test_summary_line_leads_the_output_with_file_and_match_counts(history):
    for index in range(12):
        history.write(
            f"sum{index:05d}-session.jsonl",
            [message("user", f"summaryneedle {index:02d}")],
            mtime=1_600_000_000 + index,
        )

    output = server.search_session_history("summaryneedle", max_results=20)
    head = summary(output)

    assert head.searched == 10
    assert head.total == 12
    assert head.skipped == 0
    assert head.matches == 10
    assert len(excerpts(output)) == 10


def test_summary_match_count_follows_the_lines_that_are_shown(history):
    history.write(
        "tallyyyy-1111.jsonl",
        [message("user", f"tallyneedle {index:02d}") for index in range(12)],
    )

    for max_results, shown in ((3, 3), (20, 12)):
        output = server.search_session_history("tallyneedle", max_results=max_results)
        head = summary(output)

        assert head.matches == shown
        assert len(excerpts(output)) == shown


def test_files_without_the_jsonl_suffix_are_ignored(history):
    (history.directory / "transcript.json").write_text(
        json.dumps(message("user", "other suffix radish")) + "\n", encoding="utf-8"
    )
    (history.directory / "notes.txt").write_text("plain text radish\n", encoding="utf-8")
    history.write("session4-dddd.jsonl", [message("user", "indexed suffix radish")])

    assert excerpts(server.search_session_history("radish")) == ["indexed suffix radish"]


def test_max_results_is_clamped_between_one_and_twenty(history):
    history.write(
        "session5-eeee.jsonl",
        [message("user", f"counter {index:02d}") for index in range(25)],
    )

    assert excerpts(server.search_session_history("counter", max_results=0)) == ["counter 24"]
    assert excerpts(server.search_session_history("counter", max_results=-3)) == ["counter 24"]
    assert len(excerpts(server.search_session_history("counter", max_results=100))) == 20
    assert len(excerpts(server.search_session_history("counter", max_results=3))) == 3
    assert len(excerpts(server.search_session_history("counter"))) == 8


def test_query_without_hits_reports_the_query(history):
    history.write("session6-ffff.jsonl", [message("user", "only stored content")])

    output = server.search_session_history("absentneedle")

    assert isinstance(output, str)
    assert output.startswith("No match for:")
    assert "absentneedle" in output


@pytest.mark.parametrize("query", ["", "   "])
def test_blank_query_returns_a_no_match_string(history, query):
    history.write("session6-gggg.jsonl", [message("user", "only stored content")])

    output = server.search_session_history(query)

    assert isinstance(output, str)
    assert output.startswith("No match for:")


def test_no_match_reports_the_files_that_were_searched(history):
    for index in range(3):
        history.write(
            f"nomatch{index}-session.jsonl",
            [message("user", f"stored content {index:02d}")],
            mtime=1_600_000_000 + index,
        )

    output = server.search_session_history("absentneedle")
    head = summary(output)

    assert output.startswith("No match for:")
    assert "absentneedle" in output
    assert head.searched == 3
    assert head.total == 3
    assert head.skipped == 0
    assert head.matches in (None, 0)


def test_no_match_still_reports_the_skipped_file(history, unreadable):
    history.write("nomkeep0-1111.jsonl", [message("user", "stored content")], mtime=1_700_000_000)
    history.write("nombrok0-2222.jsonl", [message("user", "stored content")], mtime=1_600_000_000)
    unreadable("nombrok0-2222.jsonl")

    output = server.search_session_history("absentneedle")
    head = summary(output)

    assert output.startswith("No match for:")
    assert head.total == 2
    assert head.skipped == 1
    assert READ_REASON_RE.search(head.line), head.line
    assert_searched_is_consistent(head)


def test_missing_project_directory_reports_the_history_as_unavailable(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(server, "_PROJECTS_ROOT", tmp_path / "projects")

    output = server.search_session_history("anything")

    assert isinstance(output, str)
    assert output.startswith("Session history unavailable:")
    assert "No match for:" not in output
    assert output.split(":", 1)[1].strip()


def test_history_that_cannot_be_read_at_all_is_unavailable(history, unreadable):
    history.write("onlyfile-1111.jsonl", [message("user", "stored blockedneedle")])
    unreadable("onlyfile-1111.jsonl")

    output = server.search_session_history("blockedneedle")

    assert isinstance(output, str)
    assert output.startswith("Session history unavailable:")
    assert "No match for:" not in output


def test_unexpected_read_failure_is_unavailable_without_a_traceback(history, unreadable):
    history.write("boomfile-1111.jsonl", [message("user", "stored boomneedle")])
    unreadable("boomfile-1111.jsonl", error=RuntimeError("synthetic read failure"))

    output = server.search_session_history("boomneedle")

    assert isinstance(output, str)
    assert output.startswith("Session history unavailable:")
    assert "No match for:" not in output
    assert "Traceback" not in output


def test_lines_that_are_not_json_are_skipped(history):
    history.write(
        "session7-hhhh.jsonl",
        [
            "{ not json at all",
            "",
            message("user", "survivor lettuce"),
            "]]] broken tail",
        ],
    )

    assert excerpts(server.search_session_history("lettuce")) == ["survivor lettuce"]


def test_files_larger_than_ten_megabytes_are_skipped(history):
    oversized = history.write(
        "bigfile0-1111.jsonl",
        [message("user", "oversized needle " + "x" * (11 * 1024 * 1024))],
        mtime=1_700_000_000,
    )
    history.write(
        "smallfil-2222.jsonl",
        [message("user", "small needle")],
        mtime=1_600_000_000,
    )

    assert oversized.stat().st_size > 10 * 1024 * 1024
    assert excerpts(server.search_session_history("needle")) == ["small needle"]


def test_summary_counts_the_file_skipped_for_size(history):
    history.write(
        "bigskip0-1111.jsonl",
        [message("user", "oversized sizeneedle " + "x" * (11 * 1024 * 1024))],
        mtime=1_700_000_000,
    )
    history.write("smallone-2222.jsonl", [message("user", "kept sizeneedle")], mtime=1_600_000_000)
    history.write("quietone-3333.jsonl", [message("user", "unrelated")], mtime=1_500_000_000)

    output = server.search_session_history("sizeneedle")
    head = summary(output)

    assert head.total == 3
    assert head.skipped == 1
    assert head.matches == 1
    assert SIZE_REASON_RE.search(head.line), head.line
    assert_searched_is_consistent(head)
    assert excerpts(output) == ["kept sizeneedle"]


def test_summary_counts_the_file_that_cannot_be_read(history, unreadable):
    history.write(
        "readable-1111.jsonl",
        [message("user", "kept faultneedle")],
        mtime=1_700_000_000,
    )
    history.write(
        "brokennn-2222.jsonl",
        [message("user", "lost faultneedle")],
        mtime=1_600_000_000,
    )
    unreadable("brokennn-2222.jsonl")

    output = server.search_session_history("faultneedle")
    head = summary(output)

    assert head.total == 2
    assert head.skipped == 1
    assert head.matches == 1
    assert READ_REASON_RE.search(head.line), head.line
    assert_searched_is_consistent(head)
    assert excerpts(output) == ["kept faultneedle"]


def test_summary_counts_both_skip_reasons_together(history, unreadable):
    history.write(
        "bigskip1-1111.jsonl",
        [message("user", "oversized bothneedle " + "x" * (11 * 1024 * 1024))],
        mtime=1_700_000_000,
    )
    history.write("brokentw-2222.jsonl", [message("user", "lost bothneedle")], mtime=1_600_000_000)
    history.write("readabtw-3333.jsonl", [message("user", "kept bothneedle")], mtime=1_500_000_000)
    unreadable("brokentw-2222.jsonl")

    output = server.search_session_history("bothneedle")
    head = summary(output)

    assert head.total == 3
    assert head.skipped == 2
    assert head.matches == 1
    assert SIZE_REASON_RE.search(head.line), head.line
    assert READ_REASON_RE.search(head.line), head.line
    assert_searched_is_consistent(head)
    assert excerpts(output) == ["kept bothneedle"]


def test_excerpt_total_over_four_thousand_characters_is_truncated(history):
    term = "truncationneedle" + "z" * 44
    padding = "p" * 200
    history.write(
        "session8-iiii.jsonl",
        [message("user", f"{padding} {term} {index:02d} {padding}") for index in range(20)],
    )

    output = server.search_session_history(term, max_results=20)

    assert output.rstrip().endswith("... (truncated)")
    body = [line for line in output.splitlines() if LINE_RE.match(line)]
    assert 0 < len(body) <= 20


def test_line_format_uses_session_prefix_local_time_and_role(history):
    history.write(
        "12ab34cd-jjjj.jsonl",
        [
            message("user", "stamped kumquat", timestamp="2026-03-04T05:06:07.000Z"),
            message("assistant", "unstamped kumquat"),
        ],
    )

    entries = parsed(server.search_session_history("kumquat"))
    assert len(entries) == 2
    newest, oldest = entries

    assert newest["session"] == "12ab34cd"
    assert newest["role"] == "assistant"
    assert newest["date"] == "--/--"
    assert newest["time"] == "--:--"

    local = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc).astimezone()
    assert oldest["session"] == "12ab34cd"
    assert oldest["role"] == "user"
    assert oldest["date"] == local.strftime("%m/%d")
    assert oldest["time"] == local.strftime("%H:%M")


def test_excerpt_windows_around_the_first_term_and_flattens_newlines(history):
    history.write(
        "session9-kkkk.jsonl",
        [
            message("user", "alpha\nwindowterm\nbravo"),
            message("user", "othertoken " + "c" * 400 + " maintoken tail"),
        ],
    )

    assert excerpts(server.search_session_history("windowterm")) == ["alpha windowterm bravo"]

    windowed = excerpts(server.search_session_history("maintoken othertoken"))
    assert len(windowed) == 1
    assert "maintoken" in windowed[0]
    assert "othertoken" not in windowed[0]
    assert len(windowed[0]) <= 200


def test_docstring_states_the_search_limits():
    doc = inspect.getdoc(server.search_session_history) or ""

    # 上限の書き方（並び・記号）は契約が決めていないので、数と単位が同じ行に
    # 並んでいるかだけを見る。
    def states(number, unit):
        return re.search(
            rf"\b{number}\b[^\n]*{unit}|{unit}[^\n]*\b{number}\b", doc, re.IGNORECASE
        )

    assert states(r"10", r"\bfiles?\b"), doc
    assert re.search(r"\b10\s*(?:MiB|MB)\b", doc, re.IGNORECASE), doc
    assert states(r"20", r"\b(?:results?|matches|excerpts?)\b"), doc
    assert states(r"4[,_ ]?000", r"\bchar"), doc


@pytest.mark.parametrize("configured_value", [None, ""])
def test_history_root_falls_back_to_home_claude_when_config_is_unset_or_empty(
    tmp_path, configured_value
):
    env = os.environ.copy()
    if configured_value is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = configured_value
    env["TEST_CLAUDE_HOME"] = str(tmp_path / "home")
    code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path.cwd() / 'mcp_server'))\n"
        "Path.home = classmethod(lambda cls: Path(os.environ['TEST_CLAUDE_HOME']))\n"
        "from mcp_server import server\n"
        "print(server._PROJECTS_ROOT)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    assert Path(completed.stdout.strip()) == tmp_path / "home" / ".claude" / "projects"


def test_history_root_uses_nonempty_claude_config_dir(tmp_path):
    env = os.environ.copy()
    config_root = tmp_path / "custom-claude"
    env["CLAUDE_CONFIG_DIR"] = str(config_root)
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path.cwd() / 'mcp_server'))\n"
        "from mcp_server import server\n"
        "print(server._PROJECTS_ROOT)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    assert Path(completed.stdout.strip()) == config_root / "projects"
