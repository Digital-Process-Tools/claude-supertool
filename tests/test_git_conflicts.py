"""Unit tests for presets/git/conflicts.py — block extraction across markers."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock


PRESET = Path(__file__).parent.parent / "presets" / "git" / "conflicts.py"
_spec = importlib.util.spec_from_file_location("git_conflicts", PRESET)
assert _spec is not None and _spec.loader is not None
conflicts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conflicts)


def test_extracts_all_blocks(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text(
        "before\n"
        "<<<<<<< HEAD\n"
        "ours_a\n"
        "=======\n"
        "theirs_a\n"
        ">>>>>>> branch\n"
        "middle\n"
        "<<<<<<< HEAD\n"
        "ours_b\n"
        "=======\n"
        "theirs_b\n"
        ">>>>>>> branch\n"
    )
    out = conflicts._all_conflict_blocks(str(f), max_lines_per_block=20)
    assert "block 1" in out and "block 2" in out
    assert "ours_a" in out and "theirs_a" in out
    assert "ours_b" in out and "theirs_b" in out


def test_truncates_long_block(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    body = "\n".join(f"line{i}" for i in range(50))
    f.write_text(f"<<<<<<< HEAD\n{body}\n=======\nx\n>>>>>>> branch\n")
    out = conflicts._all_conflict_blocks(str(f), max_lines_per_block=5)
    assert "truncated at 5" in out


def test_no_markers_message(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("clean file\n")
    out = conflicts._all_conflict_blocks(str(f), max_lines_per_block=10)
    assert "no <<<<<<< marker" in out


def _git_result(stdout: str = "", returncode: int = 0):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr="")


def test_incoming_info_skipped_outside_merge_states() -> None:
    assert conflicts._incoming_info("foo.py", "rebase") == []
    assert conflicts._incoming_info("foo.py", "") == []


def test_incoming_info_merge_includes_author_branch_and_mr() -> None:
    log_line = "a1b2c3d Florian DAVID 2 hours ago :: Rename underscore properties"
    fake_git = mock.Mock(side_effect=[
        _git_result(stdout=log_line + "\n"),
        _git_result(stdout="remotes/origin/feature/rename-props\n"),
    ])
    fake_which = mock.Mock(side_effect=lambda cmd: "/usr/bin/glab" if cmd == "glab" else None)
    fake_run = mock.Mock(return_value=_git_result(
        stdout='[{"iid": 21803, "title": "Rename underscore properties"}]\n'
    ))
    with mock.patch.object(conflicts, "_git", fake_git), \
         mock.patch.object(conflicts.shutil, "which", fake_which), \
         mock.patch.object(conflicts.subprocess, "run", fake_run):
        lines = conflicts._incoming_info("foo.py", "merge")
    assert lines == [
        f"  Last touched (theirs): {log_line}",
        "  Incoming branch: feature/rename-props",
        "  MR: !21803 Rename underscore properties",
    ]


def test_incoming_info_cherry_pick_only_shows_log() -> None:
    log_line = "a1b2c3d Author 1 hour ago :: pick subject"
    fake_git = mock.Mock(return_value=_git_result(stdout=log_line + "\n"))
    with mock.patch.object(conflicts, "_git", fake_git):
        lines = conflicts._incoming_info("foo.py", "cherry-pick")
    assert lines == [f"  Last touched (theirs): {log_line}"]


def test_incoming_info_no_mr_tool_available() -> None:
    log_line = "abc Author 5m ago :: subject"
    fake_git = mock.Mock(side_effect=[
        _git_result(stdout=log_line + "\n"),
        _git_result(stdout="feature/x\n"),
    ])
    with mock.patch.object(conflicts, "_git", fake_git), \
         mock.patch.object(conflicts.shutil, "which", return_value=None):
        lines = conflicts._incoming_info("foo.py", "merge")
    assert lines == [
        f"  Last touched (theirs): {log_line}",
        "  Incoming branch: feature/x",
    ]
