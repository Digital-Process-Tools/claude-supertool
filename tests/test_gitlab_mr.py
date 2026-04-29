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
