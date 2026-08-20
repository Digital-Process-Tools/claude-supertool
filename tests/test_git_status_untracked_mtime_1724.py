"""#1724 — an untracked path carries its mtime, and an unreadable one says so.

The incident: a reviewer agent running under the #1717 author left
`conftest_patch.py` at the repository root. `git-status` listed it in the same
shape as every file the author had made itself, and the author had to establish
by hand that the file was not its own before it could read its own suite figures.

What this file pins is **one field, not a verdict**. Nothing on disk records
which process wrote a file, so no marker here claims authorship. What it claims
is a time, and — where the time cannot be obtained — that it could not be
obtained, because a silently omitted marker reads as "this one is yours", which
is this repository's own defect class facing a new surface.

Every case is paired inside one fixture: a tagged path next to an untagged one,
an unreadable path next to a readable one. A "no marker" assertion on its own
passes when the whole render is missing.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import time
from contextlib import redirect_stdout
from pathlib import Path

PRESET_DIR = Path(__file__).parent.parent / "presets" / "git"
_spec = importlib.util.spec_from_file_location("git_status_1724",
                                               PRESET_DIR / "status.py")
assert _spec is not None and _spec.loader is not None
status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status)

NL = chr(10)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f").write_text("x" + NL)
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "initial")
    return repo


def _stub_no_mr(monkeypatch) -> None:
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if args and args[0] in ("glab", "gh"):
            return subprocess.CompletedProcess(args=args, returncode=1,
                                               stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(status.subprocess, "run", fake_run)


def _run_main(repo: Path, monkeypatch, *args: str) -> str:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(status.sys, "argv", ["status.py", *args])
    buf = io.StringIO()
    with redirect_stdout(buf):
        status.main()
    return buf.getvalue()


def _line_for(out: str, name: str) -> str:
    """The one rendered untracked row naming `name`, or fail loudly.

    A positive control in its own right: every "no marker" assertion below
    reads a line this found, so a render that vanished fails here rather than
    passing as an absent marker.
    """
    hits = [l for l in out.splitlines()
            if l.startswith("  ") and l.strip().split()[0:1] == [name]]
    assert len(hits) == 1, (
        "expected exactly one untracked row for " + repr(name)
        + ", got " + repr(hits) + NL + out)
    return hits[0]


def test_fresh_and_stale_untracked_paths_both_carry_a_time(tmp_path, monkeypatch) -> None:
    """Both halves in one tree: a write inside the window is tagged, an old one is not.

    The tag is a fact about the clock, not a claim about an author — which is
    why the old file is not tagged "yours" and the fresh one is not tagged
    "someone else's".
    """
    repo = _init_repo(tmp_path)
    (repo / "old_scratch").write_text("mine, hours ago" + NL)
    two_hours_ago = time.time() - 7200
    os.utime(repo / "old_scratch", (two_hours_ago, two_hours_ago))
    (repo / "fresh_stray").write_text("dropped just now" + NL)
    # Pinned, not left at "now". `_age` renders whole seconds, so `written 0s
    # ago` is an assertion with a ONE-SECOND budget: a loaded machine that
    # takes two seconds to get from here to the render fails it, and the
    # failure reads as a verdict about the product (#1845). Five minutes is
    # inside the 15m activity window with 3x margin, and `_age`'s minute
    # bucket holds it at `5m` for a full 60 seconds of drift -- a 60x wider
    # budget. It is also a STRONGER assertion than the old disjunction: `5m`
    # can only come from the mtime set here, where `0s` is equally what a
    # render that had lost the mtime and formatted `_age(0)` would print.
    five_minutes_ago = time.time() - 300
    os.utime(repo / "fresh_stray", (five_minutes_ago, five_minutes_ago))

    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch, "full")

    fresh = _line_for(out, "fresh_stray")
    old = _line_for(out, "old_scratch")

    # Marker present.
    assert "activity window" in fresh, fresh
    assert "written 5m ago" in fresh, fresh
    # Marker absent — on a line that provably rendered, and that still carries
    # the field, so this cannot pass by the whole column being missing.
    assert "activity window" not in old, old
    assert "written 2h ago" in old, old
    # No authorship claim anywhere in the section.
    assert "yours" not in out.lower()


def test_mtime_that_cannot_be_read_says_so_rather_than_going_silent(tmp_path, monkeypatch) -> None:
    """The real race: git listed the path, then it went away before the stat.

    Not a mocked `os.stat`. `git status` really lists both files, one of them is
    really removed between that call and the render, and the stat that fails is a
    real stat of a real absent path — the exact shape of "another process is
    writing in your tree", which is the situation this whole field exists for.
    """
    repo = _init_repo(tmp_path)
    (repo / "still_here").write_text("a" + NL)
    (repo / "vanished").write_text("b" + NL)

    real_git = status._git

    def racing_git(args, timeout=None):
        res = real_git(args, timeout)
        if args[:1] == ["status"]:
            gone = repo / "vanished"
            if gone.exists():
                gone.unlink()
        return res

    monkeypatch.setattr(status, "_git", racing_git)
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch, "full")

    vanished = _line_for(out, "vanished")
    here = _line_for(out, "still_here")
    assert status.MTIME_UNREADABLE_MARKER in vanished, vanished
    # Paired: the readable sibling is not marked, and does carry a time.
    assert status.MTIME_UNREADABLE_MARKER not in here, here
    assert "written " in here, here


def test_the_window_is_the_one_git_worktrees_already_uses(tmp_path, monkeypatch) -> None:
    """One number and one knob, shared with `git-worktrees` rather than forked.

    A second default would drift, and the reader comparing two ops' renders of
    the same tree would be comparing two different windows.
    """
    _wspec = importlib.util.spec_from_file_location("git_worktrees_1724",
                                                    PRESET_DIR / "worktrees.py")
    assert _wspec is not None and _wspec.loader is not None
    worktrees = importlib.util.module_from_spec(_wspec)
    _wspec.loader.exec_module(worktrees)
    assert status.ACTIVE_WINDOW_DEFAULT == worktrees.ACTIVE_WINDOW_DEFAULT == 900

    repo = _init_repo(tmp_path)
    (repo / "an_hour_old").write_text("x" + NL)
    hour_ago = time.time() - 3600
    os.utime(repo / "an_hour_old", (hour_ago, hour_ago))
    _stub_no_mr(monkeypatch)

    out = _run_main(repo, monkeypatch, "full")
    assert "activity window" not in _line_for(out, "an_hour_old")

    monkeypatch.setenv("SUPERTOOL_WORKTREE_ACTIVE_WINDOW", "7200")
    out = _run_main(repo, monkeypatch, "full")
    widened = _line_for(out, "an_hour_old")
    assert "inside the 2h activity window" in widened, widened


def test_truncated_default_view_still_reports_the_newest_hidden_write(tmp_path, monkeypatch) -> None:
    """The cap must not hide the entry the field was added to surface.

    The default view lists 10 of N in git's own order, so a stray file sorting
    late is cut. The `... (N more)` marker therefore carries the newest write
    among the hidden ones, which is what a reader would otherwise re-run
    `git-status:full` to get.
    """
    repo = _init_repo(tmp_path)
    old = time.time() - 86400
    for i in range(14):
        p = repo / ("a_" + format(i, "02d"))
        p.write_text("x" + NL)
        os.utime(p, (old, old))
    (repo / "zz_stray").write_text("dropped just now" + NL)
    # Pinned for the same reason as the sibling case above (#1845): `newest of
    # them written 0s ago` is a one-second budget, and the delay a loaded
    # machine adds between this write and the render is read as a product
    # verdict. `5m` still sorts as the newest of the fifteen -- the other
    # fourteen are a day old -- and survives 60 seconds of drift.
    five_minutes_ago = time.time() - 300
    os.utime(repo / "zz_stray", (five_minutes_ago, five_minutes_ago))

    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)

    assert "### Untracked (15)" in out
    assert "zz_stray" not in out          # genuinely hidden by the cap
    more = next(l for l in out.splitlines() if l.strip().startswith("... (5 more"))
    assert "newest of them written 5m ago" in more, more
    assert "unreadable" not in more, more


def test_a_hidden_tail_nobody_could_time_says_so_rather_than_going_quiet(
        tmp_path, monkeypatch) -> None:
    """The same defect one level in: silence where the newest hidden write goes.

    A `... (5 more)` with no time on it renders identically whether every hidden
    row was stat'd and none was interesting, or none of them could be stat'd at
    all. The second is exactly the state a process writing in your tree
    produces, so the marker has to say which it is.

    Real, not mocked: the five hidden rows are really removed between `git
    status` and the stat, the same race the sibling test uses.
    """
    repo = _init_repo(tmp_path)
    old = time.time() - 86400
    for i in range(10):
        p = repo / ("a_" + format(i, "02d"))
        p.write_text("x" + NL)
        os.utime(p, (old, old))
    for i in range(5):
        (repo / ("z_" + str(i))).write_text("gone by the time we look" + NL)

    real_git = status._git

    def racing_git(args, timeout=None):
        res = real_git(args, timeout)
        if args[:1] == ["status"]:
            for i in range(5):
                gone = repo / ("z_" + str(i))
                if gone.exists():
                    gone.unlink()
        return res

    monkeypatch.setattr(status, "_git", racing_git)
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)

    more = next(l for l in out.splitlines() if l.strip().startswith("... (5 more"))
    assert "UNKNOWN" in more, more
    assert "5 of 5" in more or "no mtime" in more, more
    # Paired positive control in the same fixture: the ten VISIBLE rows were
    # readable and do carry a time, so this cannot pass by the column having
    # collapsed everywhere.
    assert "written 24h ago" in _line_for(out, "a_00")


def test_a_partly_readable_hidden_tail_reports_both_halves(tmp_path, monkeypatch) -> None:
    """Newest known write AND how many could not be read — neither alone."""
    repo = _init_repo(tmp_path)
    old = time.time() - 86400
    for i in range(10):
        p = repo / ("a_" + format(i, "02d"))
        p.write_text("x" + NL)
        os.utime(p, (old, old))
    for i in range(5):
        (repo / ("z_" + str(i))).write_text("y" + NL)

    real_git = status._git

    def racing_git(args, timeout=None):
        res = real_git(args, timeout)
        if args[:1] == ["status"]:
            for i in (0, 1):
                gone = repo / ("z_" + str(i))
                if gone.exists():
                    gone.unlink()
        return res

    monkeypatch.setattr(status, "_git", racing_git)
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)

    more = next(l for l in out.splitlines() if l.strip().startswith("... (5 more"))
    assert "newest of them written" in more, more
    assert "2 of 5" in more, more
