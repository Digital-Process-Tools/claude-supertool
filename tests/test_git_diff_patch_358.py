"""Regression tests for issue #358 — git-diff never shows the actual +/- hunks.

`git-diff:PATH` returns a review summary (file list, +/- totals, red-flag scan)
but no patch body, so there's no way to see WHAT changed. This adds an opt-in
`:full` mode that appends the raw `git diff` hunks. These tests build hermetic
throwaway repos in tmp_path and drive diff.py as a subprocess, matching the
existing test harness.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

DIFF = Path(__file__).parent.parent / "presets" / "git" / "diff.py"


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], check=True, cwd=path)
    subprocess.run(["git", "config", "user.name", "T"], check=True, cwd=path)
    (path / "seed.txt").write_text("line one\nline two\nline three\n")
    subprocess.run(["git", "add", "seed.txt"], check=True, cwd=path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], check=True, cwd=path)


def _run(repo: Path, *args: str) -> str:
    res = subprocess.run(
        [sys.executable, str(DIFF), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=repo, env=dict(os.environ),
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def _hunk_lines(out: str) -> list[str]:
    """Lines that are real diff body (+/-), excluding the +/- summary count line
    and the '@@' hunk headers we also want to see."""
    body = []
    for ln in out.splitlines():
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")):
            body.append(ln)
    return body


def test_default_path_mode_has_no_hunks(tmp_path: Path) -> None:
    """Baseline: the summary mode must NOT include patch body lines."""
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("line one\nline TWO changed\nline three\n")

    out = _run(tmp_path, "seed.txt")

    assert "1 file changed" in out  # summary still there
    assert _hunk_lines(out) == [], f"default mode leaked hunks:\n{out}"
    assert "@@" not in out


def test_full_mode_shows_actual_hunks(tmp_path: Path) -> None:
    """The new :full mode MUST print the real +/- hunks for the path.

    RED before fix: diff.py ignores arg2 for paths, so no patch body appears.
    """
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("line one\nline TWO changed\nline three\n")

    out = _run(tmp_path, "seed.txt", "full")

    body = _hunk_lines(out)
    assert any(ln.startswith("+") and "TWO changed" in ln for ln in body), \
        f"expected an added '+ ...TWO changed' hunk line, got:\n{out}"
    assert any(ln.startswith("-") and "line two" in ln for ln in body), \
        f"expected a removed '- line two' hunk line, got:\n{out}"
    assert "@@" in out, f"expected a @@ hunk header, got:\n{out}"
    # summary is preserved above the patch
    assert "1 file changed" in out


def test_full_mode_keeps_summary_and_review(tmp_path: Path) -> None:
    """:full is additive — the classified file list still renders."""
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("line one\nchanged\nline three\n")

    out = _run(tmp_path, "seed.txt", "full")

    assert "## Files changed" in out
    assert "@@" in out


def test_full_mode_no_changes_stays_clean(tmp_path: Path) -> None:
    """A tracked-but-unmodified path in :full mode still says 'No changes.'
    and prints no phantom hunks."""
    _init_repo(tmp_path)

    out = _run(tmp_path, "seed.txt", "full")

    assert "No changes." in out
    assert "@@" not in out
