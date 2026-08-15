"""claude-log-cost (#1252): measure what tool results actually cost.

The point of the op is a number that decides whether a result-trimming hook is
worth building. So every test here is about a number being *right* or an
absence being *disclosed* — never about the table being pretty.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _changelog_findable import assert_change_is_findable  # noqa: E402

PRESET_DIR = Path(__file__).resolve().parent.parent / "presets" / "claude-log"
sys.path.insert(0, str(PRESET_DIR))

from _preset_loader import load_preset_module  # noqa: E402

_common = load_preset_module("claude-log", "_common", prefix="claude_log_")
cost = load_preset_module("claude-log", "cost", prefix="claude_log_")


# ---------- helpers ----------------------------------------------------------


def _tool_use(uid: str, name: str, inp: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}],
        },
    }


def _tool_result(uid: str, content, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": uid, "content": content, "is_error": is_error}
            ],
        },
    }


def _write_jsonl(path: Path, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


# `\r?$` rather than `$`: `subprocess.run(text=True)` translates newlines, so a
# stray CR should not survive to here — but the one platform that would prove
# otherwise is the one this was not written on, and the tolerance costs nothing.
_MEASURED = re.compile(
    r"^Measured: (\d+) sessions?, (\d+) tool results?, (\d+) bytes\r?$", re.M)


def measured(stdout: str) -> tuple:
    """(sessions, results, bytes) read off the report's own `Measured:` line.

    #1731: the selection tests asserted a bare `"500" not in r.stdout`, meaning
    "session bbbb's 500-byte result was not counted". What that actually says
    is that the three characters `500` appear nowhere in the report — and the
    report prints byte totals, percentiles, shares and a filesystem path, any
    of which can carry them. So the assertion could not tell its finding from a
    coincidence: it failed under the full suite and passed in isolation on the
    same commit (13457 + 1 = 13458 — identical selection, order the only
    difference).

    This reads the field the report *attributes* to what it measured. It raises
    when the line is absent rather than returning a default, because every
    caller here is making a claim about a number, and a report that printed
    nothing must redden rather than satisfy a negative assertion for free.
    """
    m = _MEASURED.search(stdout)
    assert m is not None, (
        "the report has no `Measured:` line, so nothing can be concluded "
        "about what it counted:" + os.linesep + stdout)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


class FakeProject:
    def __init__(self, cwd: Path, home: Path, proj_dir: Path) -> None:
        self.cwd = cwd
        self.home = home
        self.proj_dir = proj_dir

    def add_session(self, uuid: str, events: list) -> Path:
        path = self.proj_dir / f"{uuid}.jsonl"
        _write_jsonl(path, events)
        return path

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        return subprocess.run(
            [sys.executable, str(PRESET_DIR / "cost.py"), *args],
            capture_output=True,
            text=True,
            cwd=self.cwd,
            env=env,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )


@pytest.fixture()
def proj(tmp_path: Path) -> FakeProject:
    cwd = tmp_path / "work" / "proj"
    cwd.mkdir(parents=True)
    home = tmp_path / "fake-home"
    proj_dir = home / ".claude" / "projects" / _common.encode_cwd(str(cwd))
    proj_dir.mkdir(parents=True)
    return FakeProject(cwd, home, proj_dir)


# ---------- byte accounting per tool -----------------------------------------


class TestPerTool:
    def test_bytes_and_quantiles_per_tool(self, proj: FakeProject) -> None:
        events = []
        for i, n in enumerate((100, 200, 900)):
            events.append(_tool_use(f"t{i}", "Bash", {"command": "x"}))
            events.append(_tool_result(f"t{i}", "b" * n))
        events.append(_tool_use("r0", "Read", {"file_path": "/a.py"}))
        events.append(_tool_result("r0", "c" * 50))
        proj.add_session("s1", events)

        r = proj.run()
        assert r.returncode == 0, r.stderr + r.stdout
        # 1250 bytes total; Bash is 1200 of them
        assert "1250" in r.stdout
        assert "Bash" in r.stdout and "1200" in r.stdout
        # max and p50 for Bash are real order statistics, not the mean
        bash_line = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("Bash")][0]
        assert "900" in bash_line
        assert "200" in bash_line

    def test_multibyte_counted_in_bytes_not_chars(self, proj: FakeProject) -> None:
        proj.add_session(
            "s1",
            [_tool_use("a", "Bash", {"command": "x"}), _tool_result("a", "é" * 10)],
        )
        r = proj.run()
        assert "20" in r.stdout, r.stdout


# ---------- what it could not measure is disclosed, never dropped ------------


class TestDisclosure:
    def test_result_without_matching_tool_use_is_named(self, proj: FakeProject) -> None:
        proj.add_session("s1", [_tool_result("orphan", "z" * 40)])
        r = proj.run()
        assert r.returncode == 0, r.stderr + r.stdout
        assert "no matching tool_use" in r.stdout
        # still in the total: an unattributable result is not a free one
        assert "40" in r.stdout

    def test_unparsable_session_is_skipped_with_a_reason(self, proj: FakeProject) -> None:
        proj.add_session("good", [_tool_use("a", "Bash", {}), _tool_result("a", "y" * 30)])
        (proj.proj_dir / "bad.jsonl").write_bytes(b"\xff\xfe not json at all\n")
        r = proj.run()
        assert r.returncode == 0, r.stderr + r.stdout
        assert "skipped" in r.stdout.lower()
        assert "bad" in r.stdout

    def test_malformed_lines_are_counted(self, proj: FakeProject) -> None:
        p = proj.add_session("s1", [_tool_use("a", "Bash", {}), _tool_result("a", "y" * 30)])
        with p.open("a", encoding="utf-8") as f:
            f.write("{not json\n")
            f.write("{also not json\n")
        r = proj.run()
        assert "2 malformed" in r.stdout, r.stdout

    def test_non_text_result_blocks_are_excluded_and_named(self, proj: FakeProject) -> None:
        proj.add_session(
            "s1",
            [
                _tool_use("a", "Bash", {}),
                _tool_result("a", "q" * 10),
                _tool_use("b", "Bash", {}),
                _tool_result(
                    "b",
                    [
                        {"type": "text", "text": "w" * 5},
                        {"type": "image", "source": {"data": "AAAA" * 500}},
                    ],
                ),
            ],
        )
        r = proj.run()
        assert "non-text" in r.stdout.lower()
        # Both tool results are counted (2), and only their text is: 10 + 5.
        # The image block's 2000 base64 bytes are excluded, not folded in.
        #
        # `"2000" not in r.stdout` and `"15" in r.stdout` were the same defect
        # as #1731's — the first passes on any report that happens not to print
        # those digits, the second on any report that happens to print `15`
        # anywhere, and `15` occurs in most of them. The total the report
        # attributes to what it measured settles both.
        assert measured(r.stdout) == (1, 2, 15), r.stdout

    def test_no_sessions_says_so_rather_than_printing_zeroes(self, proj: FakeProject) -> None:
        r = proj.run()
        assert "No sessions" in r.stdout or "no sessions" in r.stdout


# ---------- per-op attribution ----------------------------------------------


class TestPerOp:
    def test_sections_split_a_batched_supertool_result(self, proj: FakeProject) -> None:
        body = (
            "--- read:a.py ---\n"
            + "x" * 100
            + "\n--- grep:foo:b ---\n"
            + "y" * 20
            + "\n"
        )
        proj.add_session(
            "s1", [_tool_use("a", "Bash", {"command": "supertool 'read:a.py' 'grep:foo:b'"}),
                   _tool_result("a", body)]
        )
        r = proj.run()
        assert r.returncode == 0, r.stderr + r.stdout
        lines = r.stdout.splitlines()
        read_line = [ln for ln in lines if ln.strip().startswith("read")]
        grep_line = [ln for ln in lines if ln.strip().startswith("grep")]
        assert read_line and grep_line, r.stdout
        assert "101" in read_line[0]  # 100 body bytes + the trailing newline
        assert "21" in grep_line[0]

    def test_results_without_markers_are_reported_as_unattributable(self, proj: FakeProject) -> None:
        proj.add_session(
            "s1", [_tool_use("a", "Bash", {"command": "ls"}), _tool_result("a", "n" * 77)]
        )
        r = proj.run()
        assert "not attributable" in r.stdout.lower()
        assert "77" in r.stdout

    def test_echoed_separator_in_plain_shell_is_not_an_op(self, proj: FakeProject) -> None:
        # Measured on five live sessions: agents write `echo "--- branch ---"`
        # as a separator, which invented nine ops that do not exist and moved
        # real bytes onto them.
        proj.add_session(
            "s1",
            [
                _tool_use("a", "Bash", {"command": 'git log -3 && echo "--- branch ---" && git branch'}),
                _tool_result("a", "commits\n--- branch ---\nmaster\n"),
            ],
        )
        stats = cost.measure_sessions([proj.proj_dir / "s1.jsonl"])
        assert "branch" not in stats.per_op
        assert stats.per_op == {}
        assert stats.unattributed_calls == 1

    def test_subsection_bytes_go_to_the_op_that_rendered_them(self, proj: FakeProject) -> None:
        body = "--- git-status ---\n" + "a" * 10 + "\n--- files ---\n" + "b" * 10 + "\n"
        proj.add_session(
            "s1",
            [
                _tool_use("a", "Bash", {"command": "supertool 'git-status'"}),
                _tool_result("a", body),
            ],
        )
        stats = cost.measure_sessions([proj.proj_dir / "s1.jsonl"])
        assert list(stats.per_op) == ["git-status"]
        # 11 bytes of the op's own body, plus the sub-section header and its 11
        assert sum(stats.per_op["git-status"]) == 22 + len("--- files ---\n")

    def test_nested_op_named_in_the_payload_keeps_its_own_row(self, proj: FakeProject) -> None:
        # batch:@- renders one section per sub-op. Those really are edits, and
        # folding them into `batch` would hide the op whose render is fat.
        body = "--- batch:@- ---\nrun\n--- edit:@payload -> a.py ---\n" + "e" * 40 + "\n"
        proj.add_session(
            "s1",
            [
                _tool_use("a", "Bash", {"command": "supertool 'batch:@-' <<EOF\nop = \"edit\"\nEOF"}),
                _tool_result("a", body),
            ],
        )
        stats = cost.measure_sessions([proj.proj_dir / "s1.jsonl"])
        assert sorted(stats.per_op) == ["batch", "edit"]
        assert sum(stats.per_op["edit"]) == 41

    def test_command_mentions_ignores_the_echoed_marker_itself(self) -> None:
        assert cost.command_mentions('supertool "paste:@-"; echo "--- filing ---"', "filing") is False
        assert cost.command_mentions("supertool 'batch:@-' <<EOF\nop = \"edit\"\nEOF", "edit") is True

    def test_command_ops_reads_the_args_supertool_was_given(self) -> None:
        assert cost.command_ops("supertool 'read:a.py' 'grep:x:y'") == {"read", "grep"}
        assert cost.command_ops("python3 supertool.py 'gh-pr:12:status'") == {"gh-pr"}
        assert cost.command_ops('git log && echo "--- branch ---"') == set()
        # An echoed marker after the op list is not corroboration for itself.
        assert "filing" not in cost.command_ops(
            'supertool "paste:@-" <<EOF\nx\nEOF\necho "--- filing ---"'
        )

    def test_section_splitter_ignores_a_marker_shaped_line_mid_body(self) -> None:
        # A dashed line inside file content must not open a new op section.
        text = "--- read:a.py ---\nbefore\n--- not an op line\nafter\n"
        sections = cost.split_sections(text)
        assert [s[0] for s in sections] == ["read:a.py"]


# ---------- repeat reads ------------------------------------------------------


class TestRepeatReads:
    def test_identical_re_read_counted_as_count_and_byte_share(self, proj: FakeProject) -> None:
        same = "s" * 100
        proj.add_session(
            "s1",
            [
                _tool_use("a", "Read", {"file_path": "/x/a.py"}),
                _tool_result("a", same),
                _tool_use("b", "Read", {"file_path": "/x/a.py"}),
                _tool_result("b", same),
                _tool_use("c", "Read", {"file_path": "/x/b.py"}),
                _tool_result("c", "o" * 100),
            ],
        )
        r = proj.run()
        assert r.returncode == 0, r.stderr + r.stdout
        assert "Repeat reads" in r.stdout
        block = r.stdout.split("Repeat reads", 1)[1]
        assert "100" in block          # the re-paid bytes
        assert "%" in block            # stated as a share too

    def test_changed_file_re_read_is_not_counted_as_unchanged(self, proj: FakeProject) -> None:
        proj.add_session(
            "s1",
            [
                _tool_use("a", "Read", {"file_path": "/x/a.py"}),
                _tool_result("a", "1" * 100),
                _tool_use("b", "Read", {"file_path": "/x/a.py"}),
                _tool_result("b", "2" * 100),
            ],
        )
        r = proj.run()
        block = r.stdout.split("Repeat reads", 1)[1]
        stats = cost.measure_sessions([proj.proj_dir / "s1.jsonl"])
        assert stats.repeat_read_paths == 1
        assert stats.unchanged_repeat_results == 0
        assert stats.unchanged_repeat_bytes == 0
        assert "0" in block

    def test_relative_and_absolute_routes_to_one_file_are_one_path(self, proj: FakeProject) -> None:
        # Read passes an absolute file_path; `read:` is written relative to the
        # session cwd, which every event carries. Unjoined, one file lands under
        # two keys and the headline repeat-read figure reads low.
        body = "--- read:a.py ---\ncontent\n"
        events = [
            {**_tool_use("a", "Bash", {"command": "supertool 'read:a.py'"}), "cwd": "/x/proj"},
            _tool_result("a", body),
            {**_tool_use("b", "Read", {"file_path": "/x/proj/a.py"}), "cwd": "/x/proj"},
            _tool_result("b", "content\n"),
        ]
        proj.add_session("s1", events)
        stats = cost.measure_sessions([proj.proj_dir / "s1.jsonl"])
        assert stats.repeat_read_paths == 1
        assert [t for t, _n, _b in stats.top_repeat_paths] == ["/x/proj/a.py"]

    def test_windows_drive_letter_in_a_read_header_is_not_truncated(self, proj: FakeProject) -> None:
        # `read:PATH:OFF:LIM` is stripped of its numeric args by splitting on
        # ':' — which eats a Windows drive colon and keys every absolute path
        # under "C", merging unrelated files into one fabricated repeat read.
        body = "--- read:C:\\proj\\a.py ---\ncontent\n"
        events = [
            {**_tool_use("a", "Bash", {"command": "supertool 'read:C:\\proj\\a.py'"}),
             "cwd": "C:\\proj"},
            _tool_result("a", body),
        ]
        proj.add_session("s1", events)
        stats = cost.measure_sessions([proj.proj_dir / "s1.jsonl"])
        assert cost.read_target("C:\\proj\\a.py") == "C:\\proj\\a.py"
        assert cost.read_target("tests/a.py:1:70") == "tests/a.py"
        assert cost.read_target("tests/a.py:full") == "tests/a.py"
        assert cost.read_target("tests/a.py:1-70") == "tests/a.py"
        assert cost.read_target("tests/a.py") == "tests/a.py"

    def test_normalise_target_uses_the_transcripts_separators(self) -> None:
        assert cost.normalise_target("/x/proj", "a.py") == "/x/proj/a.py"
        assert cost.normalise_target("/x/proj", "/abs/a.py") == "/abs/a.py"
        assert cost.normalise_target("C:\\proj", "sub\\a.py") == "C:/proj/sub/a.py"
        assert cost.normalise_target("C:\\proj", "C:\\other\\a.py") == "C:/other/a.py"
        assert cost.normalise_target("", "a.py") == "a.py"

    def test_supertool_read_sections_count_as_reads(self, proj: FakeProject) -> None:
        body = "--- read:a.py ---\n" + "z" * 50 + "\n"
        proj.add_session(
            "s1",
            [
                _tool_use("a", "Bash", {"command": "supertool 'read:a.py'"}),
                _tool_result("a", body),
                _tool_use("b", "Bash", {"command": "supertool 'read:a.py'"}),
                _tool_result("b", body),
            ],
        )
        stats = cost.measure_sessions([proj.proj_dir / "s1.jsonl"])
        assert stats.repeat_read_paths == 1
        assert stats.unchanged_repeat_results == 1


# ---------- concentration, errors, empties -----------------------------------


class TestSkewAndFailures:
    def test_top_results_share_is_reported(self, proj: FakeProject) -> None:
        events = []
        for i in range(20):
            events.append(_tool_use(f"t{i}", "Bash", {}))
            events.append(_tool_result(f"t{i}", "x" * (1000 if i == 0 else 1)))
        proj.add_session("s1", events)
        r = proj.run()
        assert "Top 10" in r.stdout
        block = r.stdout.split("Top 10", 1)[1]
        assert "1009" in block  # the 1000-byte result plus nine 1-byte ones
        assert "99.0%" in block  # 1009 of 1019 total bytes

    def test_errors_and_empties_are_counted_with_their_bytes(self, proj: FakeProject) -> None:
        proj.add_session(
            "s1",
            [
                _tool_use("a", "Bash", {}),
                _tool_result("a", "boom" * 5, is_error=True),
                _tool_use("b", "Bash", {}),
                _tool_result("b", ""),
                _tool_use("c", "Bash", {}),
                _tool_result("c", "fine"),
            ],
        )
        stats = cost.measure_sessions([proj.proj_dir / "s1.jsonl"])
        assert stats.error_results == 1
        assert stats.error_bytes == 20
        assert stats.empty_results == 1
        r = proj.run()
        assert "Errors" in r.stdout and "Empty" in r.stdout


# ---------- selection ---------------------------------------------------------


class TestSelection:
    def test_single_uuid_argument_measures_only_that_session(self, proj: FakeProject) -> None:
        """`aaaa`'s 10 bytes counted, `bbbb`'s 500 not folded in (#1731).

        One equality carries both halves on purpose. `== (1, 1, 10)` is at once
        the must-fire case (the selected session *was* measured) and the
        must-not-fire one (the other session's 500 bytes are not in the total),
        and `measured` raises on a report that printed nothing — so there is no
        way for this to pass on an absence the harness produced.
        """
        proj.add_session("aaaa", [_tool_use("a", "Bash", {}), _tool_result("a", "1" * 10)])
        proj.add_session("bbbb", [_tool_use("b", "Bash", {}), _tool_result("b", "2" * 500)])
        r = proj.run("aaaa")
        assert "aaaa" in r.stdout
        assert measured(r.stdout) == (1, 1, 10), r.stdout

    def test_no_argument_measures_every_session_in_the_project(self, proj: FakeProject) -> None:
        """The positive control for the two selection tests, same fixture.

        Without it, `== (1, 1, 10)` above would still pass against a `cost.py`
        that had stopped seeing `bbbb` at all — a selection test whose evidence
        is indistinguishable from a broken reader. Here the same parse has to
        report both sessions and all 510 bytes, so the numbers those tests
        exclude are proven reachable in the run that should include them.
        """
        proj.add_session("aaaa", [_tool_use("a", "Bash", {}), _tool_result("a", "1" * 10)])
        proj.add_session("bbbb", [_tool_use("b", "Bash", {}), _tool_result("b", "2" * 500)])
        r = proj.run()
        assert measured(r.stdout) == (2, 2, 510), r.stdout

    def test_numeric_argument_limits_session_count(self, proj: FakeProject) -> None:
        """`1` selects the most recent session only — here `bbbb`.

        `"500" in r.stdout` was the same defect in the presence direction
        (#1731): a coincidental `500` anywhere in the report satisfied it, so a
        pass was not evidence the limit had selected anything in particular.
        """
        a = proj.add_session("aaaa", [_tool_use("a", "Bash", {}), _tool_result("a", "1" * 10)])
        b = proj.add_session("bbbb", [_tool_use("b", "Bash", {}), _tool_result("b", "2" * 500)])
        os.utime(a, (1_700_000_000, 1_700_000_000))
        os.utime(b, (1_700_000_100, 1_700_000_100))
        r = proj.run("1")
        assert "1 session" in r.stdout
        assert measured(r.stdout) == (1, 1, 500), r.stdout


def test_documented() -> None:
    assert_change_is_findable(1731)
