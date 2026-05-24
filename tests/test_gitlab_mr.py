"""Unit tests for presets/gitlab/mr.py conflict-parsing helpers.

These exercise the two helpers added to gl-mr that turn raw `git merge-tree`
output into actionable info (file list + per-file hunk preview) so the LLM
sees the conflict shape in one round-trip without opening files.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "mr.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)


def _fake_run(stdout: str, returncode: int = 1) -> Any:
    """Build a fake subprocess.run result."""
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=""
    )


# ---------------------------------------------------------------------------
# _get_conflicting_files
# ---------------------------------------------------------------------------

def test_get_conflicting_files_filters_noise(monkeypatch) -> None:
    """git merge-tree --name-only mixes file names with status messages.

    Real-world output for one conflicted file: 1 path + Auto-merging line +
    CONFLICT line. The helper must dedupe to the actual paths.
    """
    raw = (
        "abc123def456abc123def456abc123def456abcd\n"
        ".claude/findings.md\n"
        "Auto-merging .claude/findings.md\n"
        "CONFLICT (content): Merge conflict in .claude/findings.md\n"
    )
    monkeypatch.setattr(
        mr.subprocess, "run", lambda *a, **kw: _fake_run(raw, returncode=1)
    )
    files = mr._get_conflicting_files("source", "master")
    assert files == [".claude/findings.md"]


def test_get_conflicting_files_multiple_files(monkeypatch) -> None:
    raw = (
        "abc123def456abc123def456abc123def456abcd\n"
        "src/foo.py\n"
        "Auto-merging src/foo.py\n"
        "CONFLICT (content): Merge conflict in src/foo.py\n"
        "src/bar.py\n"
        "Auto-merging src/bar.py\n"
        "CONFLICT (content): Merge conflict in src/bar.py\n"
    )
    monkeypatch.setattr(
        mr.subprocess, "run", lambda *a, **kw: _fake_run(raw, returncode=1)
    )
    files = mr._get_conflicting_files("source", "master")
    assert files == ["src/foo.py", "src/bar.py"]


def test_get_conflicting_files_clean_merge_returns_empty(monkeypatch) -> None:
    """Exit code 0 means no conflicts — output is just the merged tree hash."""
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run("abc123def456abc123def456abc123def456abcd\n", returncode=0),
    )
    assert mr._get_conflicting_files("source", "master") == []


def test_get_conflicting_files_handles_subprocess_error(monkeypatch) -> None:
    """Not a git repo / git not installed / refs missing — return empty."""
    def boom(*a: Any, **kw: Any) -> Any:
        raise FileNotFoundError("git: not found")
    monkeypatch.setattr(mr.subprocess, "run", boom)
    assert mr._get_conflicting_files("source", "master") == []


def test_get_conflicting_files_handles_timeout(monkeypatch) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)
    monkeypatch.setattr(mr.subprocess, "run", boom)
    assert mr._get_conflicting_files("source", "master") == []


def test_get_conflicting_files_skips_warning_hint_error_lines(monkeypatch) -> None:
    raw = (
        "abc123def456abc123def456abc123def456abcd\n"
        "warning: something might be wrong\n"
        "hint: try this instead\n"
        "error: minor non-fatal\n"
        "real/path.txt\n"
        "Auto-merging real/path.txt\n"
    )
    monkeypatch.setattr(
        mr.subprocess, "run", lambda *a, **kw: _fake_run(raw, returncode=1)
    )
    assert mr._get_conflicting_files("source", "master") == ["real/path.txt"]


# ---------------------------------------------------------------------------
# _get_conflict_hunks
# ---------------------------------------------------------------------------

def test_get_conflict_hunks_parses_real_output(monkeypatch) -> None:
    """Two-call shape: merge-base then merge-tree (old syntax).

    The merge-tree output groups per file with a section header
    ('changed in both' / 'added in remote' / etc.) followed by 1-3
    `  base/our/their <mode> <oid> <path>` lines, then the diff body.
    """
    base_out = "deadbeef1234567890abcdef1234567890abcdef\n"
    tree_out = (
        "changed in both\n"
        "  base   100644 e600561691646ac9d7c6eeab55de8388c8c136a0 path/to/file.md\n"
        "  our    100644 3dbb3a53711179b78dbe9ac20c77be6d361e32a0 path/to/file.md\n"
        "  their  100644 a6140bbda7525d73eb5f2fe5e87e2065ca505de0 path/to/file.md\n"
        "@@ -1,3 +1,3 @@\n"
        "<<<<<<< .our\n"
        "ours line\n"
        "=======\n"
        "theirs line\n"
        ">>>>>>> .their\n"
    )

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(args))
        if args[1] == "merge-base":
            return _fake_run(base_out, returncode=0)
        return _fake_run(tree_out, returncode=1)

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    hunks = mr._get_conflict_hunks("source", "master")
    assert "path/to/file.md" in hunks
    body = hunks["path/to/file.md"]
    assert "<<<<<<< .our" in body
    assert "=======" in body
    assert ">>>>>>> .their" in body
    # Section header and base/our/their lines must NOT be in the diff body
    assert "changed in both" not in body
    assert "  base   100644" not in body


def test_get_conflict_hunks_multiple_files(monkeypatch) -> None:
    base_out = "deadbeef1234567890abcdef1234567890abcdef\n"
    tree_out = (
        "changed in both\n"
        "  base   100644 aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111 file_a.txt\n"
        "  our    100644 bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222 file_a.txt\n"
        "  their  100644 cccc3333cccc3333cccc3333cccc3333cccc3333 file_a.txt\n"
        "@@ -1 +1 @@\n"
        "diff for A\n"
        "added in remote\n"
        "  their  100644 dddd4444dddd4444dddd4444dddd4444dddd4444 file_b.txt\n"
        "@@ -0,0 +1 @@\n"
        "diff for B\n"
    )
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda args, **kw: _fake_run(base_out, returncode=0)
        if args[1] == "merge-base"
        else _fake_run(tree_out, returncode=1),
    )
    hunks = mr._get_conflict_hunks("source", "master")
    assert set(hunks.keys()) == {"file_a.txt", "file_b.txt"}
    assert "diff for A" in hunks["file_a.txt"]
    assert "diff for B" in hunks["file_b.txt"]
    # Cross-contamination check
    assert "diff for B" not in hunks["file_a.txt"]
    assert "diff for A" not in hunks["file_b.txt"]


def test_get_conflict_hunks_merge_base_failure(monkeypatch) -> None:
    """If merge-base fails (refs not fetched) we cannot compute hunks."""
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run("", returncode=128),
    )
    assert mr._get_conflict_hunks("source", "master") == {}


def test_get_conflict_hunks_empty_merge_tree_output(monkeypatch) -> None:
    base_out = "deadbeef1234567890abcdef1234567890abcdef\n"
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda args, **kw: _fake_run(base_out, returncode=0)
        if args[1] == "merge-base"
        else _fake_run("", returncode=0),
    )
    assert mr._get_conflict_hunks("source", "master") == {}


def test_get_conflict_hunks_handles_subprocess_error(monkeypatch) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise FileNotFoundError("git: not found")
    monkeypatch.setattr(mr.subprocess, "run", boom)
    assert mr._get_conflict_hunks("source", "master") == {}


# ---------------------------------------------------------------------------
# main() — slim status mode
# ---------------------------------------------------------------------------

import json
import sys


def _mr_json_payload(**overrides: Any) -> str:
    """Build a minimal MR JSON response. Override fields per test."""
    base = {
        "iid": 20881,
        "state": "merged",
        "merge_status": "merged",
        "has_conflicts": False,
        "head_pipeline": {"status": "success", "id": 136900},
        "merged_at": "2026-05-04T13:48:21.913Z",
        "merge_commit_sha": "b5cd36306f6712345678",
        "web_url": "https://gitlab.example/foo/-/merge_requests/20881",
    }
    base.update(overrides)
    return json.dumps(base)


def test_main_slim_status_mode_outputs_minimal_dashboard(monkeypatch, capsys) -> None:
    """gl-mr:NUMBER:status returns ~5 lines: state, pipeline, merged_at, url."""
    payload = _mr_json_payload()
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881", "status"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    # Slim format keys present
    assert "!20881" in out
    assert "state: merged" in out
    assert "merge_status: merged" in out
    assert "conflicts: no" in out
    assert "pipeline: success (#136900)" in out
    assert "merged_at: 2026-05-04T13:48:21.913Z" in out
    assert "merge_commit: b5cd36306f67" in out
    assert "url: https://gitlab.example/foo/-/merge_requests/20881" in out
    # Full-dashboard sections must NOT appear
    assert "## Description" not in out
    assert "## Comments" not in out
    assert "Reviewers:" not in out
    assert "Branch:" not in out
    # Output stays under 500 bytes — fits in hook cache
    assert len(out) < 500


def test_main_slim_status_with_conflicts(monkeypatch, capsys) -> None:
    payload = _mr_json_payload(state="opened", has_conflicts=True, merged_at=None,
                                merge_commit_sha="")
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881", "status"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "state: opened" in out
    assert "conflicts: yes" in out
    assert "merged_at: -" in out
    assert "merge_commit:" not in out


def test_main_full_mode_unaffected(monkeypatch, capsys) -> None:
    """No 2nd arg = full dashboard. Slim short-circuit must NOT kick in."""
    payload = _mr_json_payload()
    # Suppress secondary glab/api calls (approvals, notes, etc.) — return empty list
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    # Full dashboard headers
    assert "Branch:" in out
    assert "Pipeline:" in out


def test_main_slim_ignores_unknown_second_arg(monkeypatch, capsys) -> None:
    """Only literal 'status' triggers slim mode — anything else = full dashboard."""
    payload = _mr_json_payload()
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881", "verbose"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0


# ---------------------------------------------------------------------------
# Always-print sections — empty case must show explicit marker
# ---------------------------------------------------------------------------

def test_main_full_mode_empty_sections_always_printed(monkeypatch, capsys) -> None:
    """Empty description / comments / reviewers / assignees print explicit markers."""
    payload = _mr_json_payload(
        description="",
        reviewers=[],
        assignees=[],
        created_at="2026-05-08T10:00:00.000Z",
    )
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Description\n_(empty)_" in out
    assert "## Comments (0)" in out
    assert "Reviewers: none" in out
    assert "Assignees: none" in out
    assert "Created:" in out


def test_main_full_mode_with_assignees_and_age(monkeypatch, capsys) -> None:
    payload = _mr_json_payload(
        assignees=[{"username": "alice"}, {"username": "bob"}],
        created_at="2026-05-01T10:00:00.000Z",
        updated_at="2026-05-08T10:00:00.000Z",
    )
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Assignees: alice, bob" in out
    assert "Created:" in out
    assert "Updated:" in out


def test_relative_age_formats() -> None:
    """Helper formats produces 'd', 'h', 'm', 's' suffixes."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    assert mr._relative_age((now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")).endswith("s ago")
    assert mr._relative_age((now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")).endswith("m ago")
    assert mr._relative_age((now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")).endswith("h ago")
    assert mr._relative_age((now - timedelta(days=3)).isoformat().replace("+00:00", "Z")).endswith("d ago")
    assert mr._relative_age("") == "?"
    assert mr._relative_age("not-a-date") == "?"


def _note(author: str, body: str, day: str = "2026-05-24") -> dict:
    return {"author": {"username": author}, "body": body, "created_at": f"{day}T00:00:00Z"}


def test_budgeted_comments_no_truncation_when_under_budget() -> None:
    notes = [_note("alice", "hi"), _note("bob", "hey")]
    rendered, hidden_count, hidden_bytes = mr._budgeted_comments(notes, budget=10_000, tail=2)
    assert "__GAP__" not in rendered
    assert hidden_count == 0
    assert hidden_bytes == 0
    assert len(rendered) == 2


def test_budgeted_comments_inserts_gap_and_keeps_tail() -> None:
    big = "X" * 1500
    notes = [_note(f"u{i}", big) for i in range(8)]
    rendered, hidden_count, hidden_bytes = mr._budgeted_comments(notes, budget=2000, tail=2)
    assert "__GAP__" in rendered
    assert hidden_count >= 1
    assert hidden_bytes > 0
    # Tail of 2 still present at end
    tail = [r for r in rendered if r != "__GAP__"][-2:]
    assert any("u6" in r for r in tail)
    assert any("u7" in r for r in tail)


def test_budgeted_comments_empty_list() -> None:
    rendered, hidden_count, hidden_bytes = mr._budgeted_comments([], budget=2000, tail=2)
    assert rendered == []
    assert hidden_count == 0
    assert hidden_bytes == 0


def test_budgeted_comments_tail_larger_than_list_skips_gap() -> None:
    notes = [_note("a", "x"), _note("b", "y")]
    rendered, hidden_count, _ = mr._budgeted_comments(notes, budget=2000, tail=5)
    assert "__GAP__" not in rendered
    assert hidden_count == 0
    assert len(rendered) == 2


def test_fmt_kb_thresholds() -> None:
    assert mr._fmt_kb(500) == "500B"
    assert mr._fmt_kb(2048) == "2.0KB"
    assert mr._fmt_kb(8192) == "8.0KB"


def test_budgeted_comments_hidden_bytes_counts_utf8() -> None:
    """Hidden-bytes must reflect UTF-8 byte length, not codepoint count.

    Body is truncated to COMMENT_MAX=500 chars before rendering; with all-multibyte
    content the resulting render is ~1000 bytes — strictly larger than the codepoint
    count, which proves we're counting bytes.
    """
    big_multibyte = "é" * 1500  # 2 bytes/char in UTF-8, gets truncated to 500 chars
    notes = [_note(f"u{i}", big_multibyte) for i in range(8)]
    rendered_one_chars = len(mr._render_note(notes[0]))
    rendered_one_bytes = len(mr._render_note(notes[0]).encode("utf-8"))
    assert rendered_one_bytes > rendered_one_chars, "multibyte body must inflate byte count"
    _, hidden_count, hidden_bytes = mr._budgeted_comments(notes, budget=2000, tail=2)
    assert hidden_count > 0
    assert hidden_bytes == hidden_count * rendered_one_bytes
