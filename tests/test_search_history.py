import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mcp_server import server


LINE_RE = re.compile(
    r"^\[(?P<session>[^\s\]]{8}) (?P<date>\d\d/\d\d|--/--) (?P<time>\d\d:\d\d|--:--) "
    r"(?P<role>user|assistant)\] (?P<excerpt>.*)$"
)


def message(role, content, timestamp=None, **extra):
    record = {"type": role, "message": {"role": role, "content": content}}
    if timestamp is not None:
        record["timestamp"] = timestamp
    record.update(extra)
    return record


def parsed(output):
    entries = []
    for line in output.splitlines():
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


def test_missing_project_directory_returns_a_no_match_string(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(server, "_PROJECTS_ROOT", tmp_path / "projects")

    output = server.search_session_history("anything")

    assert isinstance(output, str)
    assert output.startswith("No match for:")


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
