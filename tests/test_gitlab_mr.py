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
    hunks, skipped = mr._get_conflict_hunks("source", "master")
    assert skipped is None
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
    hunks, skipped = mr._get_conflict_hunks("source", "master")
    assert skipped is None
    assert set(hunks.keys()) == {"file_a.txt", "file_b.txt"}
    assert "diff for A" in hunks["file_a.txt"]
    assert "diff for B" in hunks["file_b.txt"]
    # Cross-contamination check
    assert "diff for B" not in hunks["file_a.txt"]
    assert "diff for A" not in hunks["file_b.txt"]


def test_get_conflict_hunks_merge_base_failure(monkeypatch) -> None:
    """If merge-base fails (refs not fetched) we cannot compute hunks — and say so."""
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run("", returncode=128),
    )
    hunks, skipped = mr._get_conflict_hunks("source", "master")
    assert hunks == {}
    assert skipped is not None and "merge-base" in skipped


def test_get_conflict_hunks_empty_merge_tree_output(monkeypatch) -> None:
    base_out = "deadbeef1234567890abcdef1234567890abcdef\n"
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda args, **kw: _fake_run(base_out, returncode=0)
        if args[1] == "merge-base"
        else _fake_run("", returncode=0),
    )
    hunks, skipped = mr._get_conflict_hunks("source", "master")
    # Empty stdout on a clean exit is a real answer: no hunks, nothing skipped.
    assert hunks == {}
    assert skipped is None


def test_get_conflict_hunks_handles_subprocess_error(monkeypatch) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise FileNotFoundError("git: not found")
    monkeypatch.setattr(mr.subprocess, "run", boom)
    hunks, skipped = mr._get_conflict_hunks("source", "master")
    assert hunks == {}
    assert skipped is not None and "git" in skipped


def test_get_conflict_hunks_timeout_reports_reason(monkeypatch) -> None:
    """#507: a merge-tree timeout is not 'no hunks' — it is 'we do not know'."""
    base_out = "deadbeef1234567890abcdef1234567890abcdef\n"

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[1] == "merge-base":
            return _fake_run(base_out, returncode=0)
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 15))

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    hunks, skipped = mr._get_conflict_hunks("source", "master")
    assert hunks == {}
    assert skipped is not None
    assert "timed out" in skipped
    assert "merge-tree" in skipped


def test_get_conflict_hunks_nonzero_exit_reports_reason(monkeypatch) -> None:
    """A failed merge-tree (bad object, old git) is a skip, not an empty answer."""
    base_out = "deadbeef1234567890abcdef1234567890abcdef\n"

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[1] == "merge-base":
            return _fake_run(base_out, returncode=0)
        r = _fake_run("", returncode=128)
        r.stderr = "fatal: not a valid object name"
        return r

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    hunks, skipped = mr._get_conflict_hunks("source", "master")
    assert hunks == {}
    assert skipped is not None
    assert "not a valid object name" in skipped


def test_get_conflict_hunks_oserror_reports_reason(monkeypatch) -> None:
    """OSError gets the same treatment as TimeoutExpired — same absence, same note."""
    def boom(*a: Any, **kw: Any) -> Any:
        raise OSError("cannot fork")
    monkeypatch.setattr(mr.subprocess, "run", boom)
    hunks, skipped = mr._get_conflict_hunks("source", "master")
    assert hunks == {}
    assert skipped is not None and "cannot fork" in skipped


def test_hunk_timeout_scales_with_conflicted_file_count() -> None:
    """15s is the floor, not the whole story: merge-tree cost tracks file count."""
    assert mr._hunk_timeout(0) == mr.HUNK_TIMEOUT_BASE
    assert mr._hunk_timeout(1) == mr.HUNK_TIMEOUT_BASE
    assert mr._hunk_timeout(2) == mr.HUNK_TIMEOUT_BASE
    assert mr._hunk_timeout(10) > mr.HUNK_TIMEOUT_BASE
    assert mr._hunk_timeout(10_000) == mr.HUNK_TIMEOUT_MAX


def test_get_conflict_hunks_passes_scaled_timeout(monkeypatch) -> None:
    """The scaled timeout has to reach subprocess.run, not just exist as a helper."""
    base_out = "deadbeef1234567890abcdef1234567890abcdef\n"
    seen: list[int] = []

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[1] == "merge-base":
            return _fake_run(base_out, returncode=0)
        seen.append(kwargs["timeout"])
        return _fake_run("", returncode=0)

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    mr._get_conflict_hunks("source", "master", file_count=12)
    assert seen == [mr._hunk_timeout(12)]


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
        "source_branch": "12167-notification-configuration-ui",
        "target_branch": "master",
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
    # Branch is identity, not detail — must be here so nobody escalates to :full for it
    assert "branch: 12167-notification-configuration-ui -> master" in out
    # Full-dashboard sections must NOT appear
    assert "## Description" not in out
    assert "## Comments" not in out
    assert "## Files" not in out
    assert "Reviewers:" not in out
    assert "Labels:" not in out
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


def test_main_slim_status_shows_non_master_target(monkeypatch, capsys) -> None:
    """Target matters as much as source: a release-targeted MR must say so."""
    payload = _mr_json_payload(state="opened", source_branch="hotfix/38-crash",
                               target_branch="release/v19.0.x")
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881", "status"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "branch: hotfix/38-crash -> release/v19.0.x" in out


def test_main_slim_status_missing_branch_fields(monkeypatch, capsys) -> None:
    """Absent branch data degrades to '?' rather than crashing or vanishing."""
    payload = _mr_json_payload(source_branch=None, target_branch=None)
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881", "status"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "branch: ? -> ?" in out


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


# ---------------------------------------------------------------------------
# Conflicts line — has_conflicts aliases cannot_be_merged?, which GitLab also
# sets on an MR with no commits. #494: the false positive already fixed on
# the board/radar surfaces (#492) but left on this, the single-MR view.
# ---------------------------------------------------------------------------

def test_main_full_mode_empty_mr_not_reported_as_conflict(monkeypatch, capsys) -> None:
    """No commits, no diff — has_conflicts is true but there is nothing to conflict over.

    _get_conflicting_files would come back empty here (no diff to compare),
    and the old code printed 'Conflicts: YES — cannot merge' anyway.
    """
    payload = _mr_json_payload(
        state="opened", has_conflicts=True, merged_at=None, merge_commit_sha="",
        detailed_merge_status="commits_status", sha=None,
    )
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Conflicts: YES" not in out
    assert "no commits" in out


def test_main_full_mode_empty_mr_null_sha_not_reported_as_conflict(monkeypatch, capsys) -> None:
    """Second no-diff signal: a null sha with no detailed_merge_status to lean on."""
    payload = _mr_json_payload(has_conflicts=True, sha=None)
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Conflicts: YES" not in out


def test_main_full_mode_matching_diff_refs_not_reported_as_conflict(monkeypatch, capsys) -> None:
    """Third no-diff signal: head_sha == base_sha — same tree, no diff possible."""
    payload = _mr_json_payload(
        has_conflicts=True,
        diff_refs={"base_sha": "abc123", "head_sha": "abc123"},
    )
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Conflicts: YES" not in out


def test_main_full_mode_genuine_conflict_still_reported(monkeypatch, capsys) -> None:
    """A real conflict — distinct head/base sha, no no-diff signal — must still say YES."""
    payload = _mr_json_payload(
        has_conflicts=True,
        diff_refs={"base_sha": "aaa111", "head_sha": "bbb222"},
        sha="bbb222",
    )
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Conflicts: YES — cannot merge" in out


def test_main_full_mode_genuine_conflict_on_draft_still_reported(monkeypatch, capsys) -> None:
    """detailed_merge_status reports the FIRST failing check, and draft runs before
    conflict in GitLab's all_mergeability_checks — a conflicted draft reports
    draft_status, not conflict. Must not be read as a no-diff signal."""
    payload = _mr_json_payload(
        has_conflicts=True,
        draft=True,
        detailed_merge_status="draft_status",
        diff_refs={"base_sha": "aaa111", "head_sha": "bbb222"},
        sha="bbb222",
    )
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Conflicts: YES — cannot merge" in out


def test_main_full_mode_genuine_conflict_with_unresolved_threads_still_reported(
    monkeypatch, capsys
) -> None:
    """Unresolved discussion threads must not mask or suppress a real conflict."""
    payload = _mr_json_payload(
        has_conflicts=True,
        blocking_discussions_resolved=False,
        diff_refs={"base_sha": "aaa111", "head_sha": "bbb222"},
        sha="bbb222",
    )
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Conflicts: YES — cannot merge" in out


def test_main_full_mode_conflict_with_no_no_diff_evidence_still_reported(
    monkeypatch, capsys
) -> None:
    """Absent fields are never evidence — no diff_refs/sha/detailed_merge_status at
    all must not be read as an empty MR. has_conflicts alone still means conflict."""
    payload = _mr_json_payload(has_conflicts=True)
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Conflicts: YES — cannot merge" in out


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
    # The blanket stub used elsewhere in this file answers *every* endpoint
    # with the MR object, including `notes`, which is documented as an array.
    # `## Comments (0)` used to print from that — a count computed from a
    # payload the code had already rejected as unreadable, which is the #812
    # defect rather than the empty-section behaviour this test is about. The
    # notes endpoint answers realistically here so the assertion means what it
    # says: zero comments, verified.
    def _run(cmd, *a, **kw):
        argv = list(cmd)
        if argv and argv[0] == "glab" and "api" in argv:
            return _fake_run("[]", returncode=0)
        return _fake_run(payload, returncode=0)

    monkeypatch.setattr(mr.subprocess, "run", _run)
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


# ---------------------------------------------------------------------------
# _name_status_flag / _get_name_status
# ---------------------------------------------------------------------------

def test_name_status_flag_mapping() -> None:
    assert mr._name_status_flag({"new_file": True}) == "A"
    assert mr._name_status_flag({"deleted_file": True}) == "D"
    assert mr._name_status_flag({"renamed_file": True}) == "R"
    assert mr._name_status_flag({}) == "M"


def _api_json(payload: Any, returncode: int = 0) -> Any:
    import json as _json
    return subprocess.CompletedProcess(
        args=["glab"], returncode=returncode, stdout=_json.dumps(payload), stderr=""
    )


def test_get_name_status_parses_flags_and_paths(monkeypatch) -> None:
    diffs = [
        {"new_file": True, "new_path": "a.py", "old_path": "a.py"},
        {"deleted_file": True, "new_path": "b.py", "old_path": "b.py"},
        {"renamed_file": True, "new_path": "d.py", "old_path": "c.py"},
        {"new_path": "e.py", "old_path": "e.py"},
    ]
    monkeypatch.setattr(mr, "_glab_api", lambda *a, **kw: _api_json(diffs))
    assert mr._get_name_status(42, fetch_all=False).entries == [
        ("A", "a.py"), ("D", "b.py"), ("R", "c.py → d.py"), ("M", "e.py"),
    ]


def test_get_name_status_rename_without_path_change_shows_single(monkeypatch) -> None:
    """A renamed_file flag with identical paths (mode-only change) shows one path."""
    diffs = [{"renamed_file": True, "new_path": "x.py", "old_path": "x.py"}]
    monkeypatch.setattr(mr, "_glab_api", lambda *a, **kw: _api_json(diffs))
    assert mr._get_name_status(1, fetch_all=False).entries == [("R", "x.py")]


def test_get_name_status_deleted_uses_old_path_when_new_missing(monkeypatch) -> None:
    diffs = [{"deleted_file": True, "new_path": "", "old_path": "gone.py"}]
    monkeypatch.setattr(mr, "_glab_api", lambda *a, **kw: _api_json(diffs))
    assert mr._get_name_status(1, fetch_all=False).entries == [("D", "gone.py")]


def test_get_name_status_api_failure_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(mr, "_glab_api", lambda *a, **kw: _api_json([], returncode=1))
    assert mr._get_name_status(1, fetch_all=False).entries == []


def test_get_name_status_bad_json_returns_empty(monkeypatch) -> None:
    bad = subprocess.CompletedProcess(args=["glab"], returncode=0, stdout="not json", stderr="")
    monkeypatch.setattr(mr, "_glab_api", lambda *a, **kw: bad)
    assert mr._get_name_status(1, fetch_all=False).entries == []


def test_get_name_status_timeout_returns_empty(monkeypatch) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="glab", timeout=10)
    monkeypatch.setattr(mr, "_glab_api", boom)
    assert mr._get_name_status(1, fetch_all=False).entries == []


def test_get_name_status_single_page_when_not_fetch_all(monkeypatch) -> None:
    """A full page (100) must NOT trigger a second fetch unless fetch_all."""
    calls = []
    full_page = [{"new_path": f"f{i}.py", "old_path": f"f{i}.py"} for i in range(100)]

    def fake(endpoint: str, *a: Any, **kw: Any) -> Any:
        calls.append(endpoint)
        return _api_json(full_page)

    monkeypatch.setattr(mr, "_glab_api", fake)
    entries = mr._get_name_status(1, fetch_all=False).entries
    assert len(entries) == 100
    assert len(calls) == 1


def test_get_name_status_paginates_when_fetch_all(monkeypatch) -> None:
    """fetch_all walks pages until a short page signals the end."""
    page1 = [{"new_path": f"p1_{i}.py", "old_path": f"p1_{i}.py"} for i in range(100)]
    page2 = [{"new_path": "p2_0.py", "old_path": "p2_0.py"}]  # short page -> stop

    def fake(endpoint: str, *a: Any, **kw: Any) -> Any:
        return _api_json(page1 if "&page=1" in endpoint else page2)

    monkeypatch.setattr(mr, "_glab_api", fake)
    entries = mr._get_name_status(1, fetch_all=True).entries
    assert len(entries) == 101
    assert entries[-1] == ("M", "p2_0.py")


def test_get_name_status_respects_fetch_cap(monkeypatch) -> None:
    """fetch_all stops once NAMESTATUS_FETCH_CAP files are collected."""
    full_page = [{"new_path": f"f{i}.py", "old_path": f"f{i}.py"} for i in range(100)]
    monkeypatch.setattr(mr, "_glab_api", lambda *a, **kw: _api_json(full_page))
    entries = mr._get_name_status(1, fetch_all=True).entries
    assert len(entries) == mr.NAMESTATUS_FETCH_CAP


# ---------------------------------------------------------------------------
# _coerce_count / _render_name_status (display-block math)
# ---------------------------------------------------------------------------

def test_coerce_count_handles_string_and_capped() -> None:
    assert mr._coerce_count("18") == 18
    assert mr._coerce_count("1000+") == 1000  # GitLab caps large MRs as "N+"
    assert mr._coerce_count(42) == 42
    assert mr._coerce_count("") is None
    assert mr._coerce_count(None) is None


def test_render_name_status_empty_entries_returns_nothing() -> None:
    assert mr._render_name_status(mr._NameStatus([], 0), "0", full=False, iid=1) == []


def test_render_name_status_under_cap_no_overflow() -> None:
    entries = [("A", "a.py"), ("D", "b.py")]
    lines = mr._render_name_status(mr._NameStatus(entries, 0), "2", full=False, iid=7)
    assert lines == ["\n## Files (2)", " A  a.py", " D  b.py"]
    assert not any("more" in ln for ln in lines)


def test_render_name_status_overflow_uses_changes_count_not_fetched() -> None:
    """The +N more count must come from changes_count, not the fetched page.

    Regression: changes_count is a *string* ("200"), so an isinstance(int)
    guard always fell through to the fetched-entry count — undercounting the
    overflow on >100-file MRs. Here 100 fetched, 50 shown, true total 200.
    """
    entries = [("M", f"f{i}.py") for i in range(100)]
    lines = mr._render_name_status(mr._NameStatus(entries, 0), "200", full=False, iid=9)
    assert lines[0] == "\n## Files (200)"
    assert len([ln for ln in lines if ln.startswith(" M")]) == mr.NAMESTATUS_DISPLAY_MAX
    assert lines[-1] == f" … +{200 - mr.NAMESTATUS_DISPLAY_MAX} more (use gl-mr:9:full)"


def test_render_name_status_falls_back_to_len_when_count_smaller() -> None:
    """If changes_count is missing/smaller than fetched, use the fetched count."""
    entries = [("A", f"a{i}.py") for i in range(60)]
    lines = mr._render_name_status(mr._NameStatus(entries, 0), "", full=False, iid=1)
    assert lines[-1] == f" … +{60 - mr.NAMESTATUS_DISPLAY_MAX} more (use gl-mr:1:full)"


def test_render_name_status_full_mode_cap_message() -> None:
    """In full mode an overflow points at the fetch cap, not :full again."""
    entries = [("M", f"f{i}.py") for i in range(mr.NAMESTATUS_FETCH_CAP)]
    lines = mr._render_name_status(mr._NameStatus(entries, 0), "1200", full=True, iid=3)
    body = [ln for ln in lines if ln.startswith(" M")]
    assert len(body) == mr.NAMESTATUS_FETCH_CAP  # full = uncapped display
    assert lines[-1] == f" … +{1200 - mr.NAMESTATUS_FETCH_CAP} more (output capped at {mr.NAMESTATUS_FETCH_CAP} files)"


# ---------------------------------------------------------------------------
# #498 — a conflicted binary file puts non-UTF-8 bytes on merge-tree stdout.
# The stub below hands those bytes to a REAL subprocess with the production
# kwargs, so the decode under test is the one subprocess actually performs.
# ---------------------------------------------------------------------------

import base64

_REAL_RUN = subprocess.run

_LF = bytes([10])
_PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")
_BINARY_BLOB = _PNG_MAGIC + bytes(range(0x80, 0x100))


def _emit_bytes(payload: bytes, **kwargs: Any) -> Any:
    """Run a real subprocess that writes PAYLOAD verbatim to stdout."""
    return _REAL_RUN(
        [sys.executable, "-c",
         "import sys,base64;sys.stdout.buffer.write(base64.b64decode(sys.argv[1]))",
         base64.b64encode(payload).decode()],
        **kwargs,
    )


def _merge_tree_bytes() -> bytes:
    """Old-syntax merge-tree output: one text conflict, one binary conflict."""
    parts = [
        b"changed in both",
        b"  base   100644 aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111 .gitlab-ci.yml",
        b"  our    100644 bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222 .gitlab-ci.yml",
        b"  their  100644 cccc3333cccc3333cccc3333cccc3333cccc3333 .gitlab-ci.yml",
        b"@@ -1 +1 @@",
        b"<<<<<<< .our",
        b"image: php:8.3",
        b"=======",
        b"image: php:8.2",
        b">>>>>>> .their",
        b"changed in both",
        b"  base   100644 dddd4444dddd4444dddd4444dddd4444dddd4444 docs/logo.png",
        b"  our    100644 eeee5555eeee5555eeee5555eeee5555eeee5555 docs/logo.png",
        b"  their  100644 ffff6666ffff6666ffff6666ffff6666ffff6666 docs/logo.png",
        b"@@ -1 +1 @@",
        b"<<<<<<< .our",
        _BINARY_BLOB,
        b"=======",
        _BINARY_BLOB,
        b">>>>>>> .their",
    ]
    return _LF.join(parts) + _LF


def test_get_conflict_hunks_survives_binary_blob(monkeypatch) -> None:
    """A PNG in the conflict set must not take the whole helper down."""
    tree_bytes = _merge_tree_bytes()

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[1] == "merge-base":
            return _fake_run("deadbeef1234567890abcdef1234567890abcdef" + chr(10), returncode=0)
        return _emit_bytes(tree_bytes, **kwargs)

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    hunks, skipped = mr._get_conflict_hunks("source", "master")
    assert skipped is None
    assert "image: php:8.3" in hunks[".gitlab-ci.yml"]
    assert "docs/logo.png" in hunks


def test_main_full_mode_binary_conflict_does_not_crash(monkeypatch, capsys) -> None:
    """End to end: the binary file stays listed, is labelled, and the sections
    after ## Conflicts still print instead of being replaced by a traceback."""
    payload = _mr_json_payload(
        has_conflicts=True,
        diff_refs={"base_sha": "aaa111", "head_sha": "bbb222"},
        sha="bbb222",
    )
    name_only = _LF.join([
        b"abc123def456abc123def456abc123def456abcd",
        b".gitlab-ci.yml",
        b"docs/logo.png",
    ]).decode() + chr(10)
    tree_bytes = _merge_tree_bytes()

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args and args[0] == "git":
            if args[1] == "merge-base":
                return _fake_run("deadbeef1234567890abcdef1234567890abcdef" + chr(10), returncode=0)
            if args[1] == "merge-tree" and "--name-only" in args:
                return _fake_run(name_only, returncode=1)
            if args[1] == "merge-tree":
                return _emit_bytes(tree_bytes, **kwargs)
            return _fake_run("", returncode=0)
        return _fake_run(payload, returncode=0)

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["mr.py", "19509"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "docs/logo.png" in out
    assert "image: php:8.3" in out
    assert "binary" in out.lower()
    assert "To resolve:" in out

# ---------------------------------------------------------------------------
# #507 — the conflicts section must distinguish three outcomes, not one:
# hunks computed and present / computed and genuinely none / not computed.
# These drive main() end to end so a fix to the helper that never reaches the
# rendering still fails.
# ---------------------------------------------------------------------------

_TREE_HASH = "a" * 40
_MERGE_BASE = "deadbeef1234567890abcdef1234567890abcdef\n"

_REAL_HUNK_OUTPUT = (
    "changed in both\n"
    "  base   100644 e600561691646ac9d7c6eeab55de8388c8c136a0 src/foo.py\n"
    "  our    100644 3dbb3a53711179b78dbe9ac20c77be6d361e32a0 src/foo.py\n"
    "  their  100644 a6140bbda7525d73eb5f2fe5e87e2065ca505de0 src/foo.py\n"
    "@@ -1,3 +1,3 @@\n"
    "<<<<<<< .our\n"
    "ours line\n"
    "=======\n"
    "theirs line\n"
    ">>>>>>> .their\n"
)


def _conflict_run(hunk_behavior, files=("src/foo.py",)):
    """subprocess.run stub for a genuinely conflicted MR.

    Routes the four calls main() makes: glab (MR payload), git rev-parse
    (local branch check), git merge-tree --name-only --write-tree (the
    authoritative file list) and git merge-tree BASE (the hunks). Only the
    last is under test; hunk_behavior decides what it does.
    """
    payload = _mr_json_payload(
        state="opened", has_conflicts=True, merged_at=None, merge_commit_sha="",
        diff_refs={"base_sha": "aaa111", "head_sha": "bbb222"}, sha="bbb222",
    )
    name_only = _TREE_HASH + "\n" + "\n".join(files) + "\n"

    def run(args, **kwargs):  # type: ignore[no-untyped-def]
        argv = list(args)
        if argv[0] != "git":
            return _fake_run(payload, returncode=0)
        if argv[1] == "rev-parse":
            return _fake_run("", returncode=1)
        if argv[1] == "merge-tree" and "--name-only" in argv:
            return _fake_run(name_only, returncode=1)
        if argv[1] == "merge-base":
            return _fake_run(_MERGE_BASE, returncode=0)
        return hunk_behavior(argv, kwargs)

    return run


def _run_conflict_view(monkeypatch, capsys, hunk_behavior, files=("src/foo.py",)):
    monkeypatch.setattr(mr.subprocess, "run", _conflict_run(hunk_behavior, files))
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    return rc, capsys.readouterr().out


def test_conflicts_section_hunks_present(monkeypatch, capsys) -> None:
    """Outcome 1 — hunks computed and present. No note of any kind."""
    rc, out = _run_conflict_view(
        monkeypatch, capsys,
        lambda argv, kw: _fake_run(_REAL_HUNK_OUTPUT, returncode=1),
    )
    assert rc == 0
    assert "## Conflicts (1 file)" in out
    assert "  src/foo.py" in out
    assert "### src/foo.py" in out
    assert "<<<<<<< .our" in out
    assert "Hunk preview unavailable" not in out
    assert "No hunk preview for" not in out


def test_conflicts_section_hunks_genuinely_empty_says_so(monkeypatch, capsys) -> None:
    """Outcome 2 — merge-tree answered, and the answer was 'nothing to show'.

    That is a fact about the merge, so it gets its own wording and must not
    be dressed up as a failure.
    """
    rc, out = _run_conflict_view(
        monkeypatch, capsys,
        lambda argv, kw: _fake_run("", returncode=0),
    )
    assert rc == 0
    assert "## Conflicts (1 file)" in out
    assert "  src/foo.py" in out
    assert "No hunk preview for" in out
    assert "src/foo.py" in out.split("No hunk preview for", 1)[1]
    assert "Hunk preview unavailable" not in out


def test_conflicts_section_hunk_timeout_says_so(monkeypatch, capsys) -> None:
    """Outcome 3 — the tool gave up. Say that, and say the file list survives it."""
    def timeout(argv, kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout", 15))

    rc, out = _run_conflict_view(monkeypatch, capsys, timeout)
    assert rc == 0
    # The file list comes from a different call and is still authoritative.
    assert "## Conflicts (1 file)" in out
    assert "  src/foo.py" in out
    assert "Hunk preview unavailable" in out
    assert "timed out" in out
    assert "still accurate" in out
    assert "--write-tree" in out
    # Not the same thing as an empty answer.
    assert "No hunk preview for" not in out
    # The resolve recipe is unaffected — it never needed hunks.
    assert "git add src/foo.py" in out


def test_conflicts_section_hunk_command_failure_says_so(monkeypatch, capsys) -> None:
    """A non-zero merge-tree exit is the same absence as a timeout (#507 judgment 3)."""
    def failed(argv, kw):
        r = _fake_run("", returncode=128)
        r.stderr = "fatal: not a valid object name"
        return r

    rc, out = _run_conflict_view(monkeypatch, capsys, failed)
    assert rc == 0
    assert "Hunk preview unavailable" in out
    assert "not a valid object name" in out
    assert "still accurate" in out
    assert "No hunk preview for" not in out


def test_conflicts_section_hunk_oserror_says_so(monkeypatch, capsys) -> None:
    """An OSError from the fork gets the note too, not silence."""
    def boom(argv, kw):
        raise OSError("cannot fork")

    rc, out = _run_conflict_view(monkeypatch, capsys, boom)
    assert rc == 0
    assert "Hunk preview unavailable" in out
    assert "cannot fork" in out


def test_conflicts_section_partial_hunks_names_the_gap(monkeypatch, capsys) -> None:
    """Two conflicted files, one hunk block: the file without one is named."""
    rc, out = _run_conflict_view(
        monkeypatch, capsys,
        lambda argv, kw: _fake_run(_REAL_HUNK_OUTPUT, returncode=1),
        files=("src/foo.py", "src/bar.py"),
    )
    assert rc == 0
    assert "## Conflicts (2 files)" in out
    assert "### src/foo.py" in out
    assert "No hunk preview for" in out
    tail = out.split("No hunk preview for", 1)[1]
    assert "src/bar.py" in tail
    assert "src/foo.py" not in tail.split("\n")[0]
