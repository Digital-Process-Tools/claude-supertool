"""`git-worktrees`' activity probe read the reflog file's mtime, not its
content (#1923).

`git gc` / `git reflog expire` — auto-triggered by an ordinary `git fetch
--prune` — rewrites `.git/worktrees/<name>/logs/HEAD` for every linked
worktree **without appending anything**: the mtime moves, the content does
not. `/oss:tick` runs a fetch before it reads the board, so every tick
manufactures its own false-positive evidence: six merged, hours-quiet trees
all read `reflog written Ns ago` seconds after the tick's own fetch, and the
cleanup gate correctly (on that evidence) refuses to reap any of them for the
next 15 minutes.

The control pair is the point, not a nicety: a tree whose reflog was
genuinely appended to inside the window must still read `occupied`, and a
tree whose reflog file was only rewritten (`git reflog expire`, or a bare
`touch`) must not. A fix that reads content but keeps an mtime fallback for
the *positive* case would pass the first test and fail the second.
"""
from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path


ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "worktrees.py"
_spec = importlib.util.spec_from_file_location("git_worktrees_reflog_1923", PRESET)
assert _spec is not None and _spec.loader is not None
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)


def _reflog_line(ts: float, msg: str = "commit: x") -> str:
    return f"aaaa0000 bbbb0000 A U Thor <a@x.com> {int(ts)} +0000\t{msg}\n"


def _fixture(tmp_path: Path) -> tuple[str, str]:
    """A worktree dir plus a *separate* gitdir, matching real layout.

    `_newest_write`'s tree walk skips `.git` but otherwise walks the whole
    `path` looking for its own newest mtime — sharing `path` and `gitdir` in
    a fixture would let that walk re-discover the reflog file by its mtime
    and silently defeat the fix being tested here.
    """
    path = tmp_path / "worktree"
    gitdir = tmp_path / "gitdir"
    (gitdir / "logs").mkdir(parents=True)
    path.mkdir()
    return str(path), str(gitdir)


# ── the control pair ─────────────────────────────────────────────────────

def test_a_real_new_entry_inside_the_window_is_occupied(tmp_path: Path) -> None:
    path, gitdir = _fixture(tmp_path)
    now = time.time()
    reflog = os.path.join(gitdir, "logs", "HEAD")
    with open(reflog, "w", encoding="utf-8") as handle:
        handle.write(_reflog_line(now - 5))

    age, label = wt._newest_write(path, gitdir, now)
    assert age is not None and age < 60, (age, label)
    assert "reflog entry" in label, label

    got = wt.assess({"path": path, "gitdir": gitdir}, now=now)
    assert got.state == wt.STATE_OCCUPIED, got


def test_a_reflog_merely_touched_by_gc_is_not_occupied(tmp_path: Path) -> None:
    """The exact incident: `git gc` rewrites the file, appends nothing.

    The last real entry is well outside the activity window; only the file's
    mtime is fresh. That must not read as recent activity.
    """
    path, gitdir = _fixture(tmp_path)
    now = time.time()
    reflog = os.path.join(gitdir, "logs", "HEAD")
    old_ts = now - 2000  # well outside the 900s default window
    with open(reflog, "w", encoding="utf-8") as handle:
        handle.write(_reflog_line(old_ts))
    # Simulate `git gc` / `git reflog expire`: the file is rewritten, mtime
    # bumps to now, and no new entry is appended.
    os.utime(reflog, None)

    age, label = wt._newest_write(path, gitdir, now)
    assert age is not None and age > 1000, (age, label)
    assert "reflog entry" in label, label

    got = wt.assess(
        {"path": path, "gitdir": gitdir}, now=now,
        scan=wt.CwdScan("no", "no process cwd inside (1 scanned)"),
    )
    assert got.state != wt.STATE_OCCUPIED, got


# ── parsing ───────────────────────────────────────────────────────────────

def test_reflog_entry_time_parses_the_last_line(tmp_path: Path) -> None:
    reflog = tmp_path / "HEAD"
    reflog.write_text(
        "0000 1111 A <a@x.com> 1000 +0000\tcommit: first\n"
        "1111 2222 A <a@x.com> 2000 +0200\tcommit: second\n",
        encoding="utf-8",
    )
    ts, why = wt._reflog_newest_entry_time(str(reflog))
    assert why is None, why
    assert ts == 2000.0, ts


def test_reflog_entry_time_reads_from_the_real_git_reflog_shape(tmp_path: Path) -> None:
    """The exact line shape quoted in #1923's own `tail -1`."""
    reflog = tmp_path / "HEAD"
    reflog.write_text(
        "21cad0a9 89d6d6a2 Author Name <a@x.com> 1787397771 +0200\t"
        "commit: Measure the reason, not the message\n",
        encoding="utf-8",
    )
    ts, why = wt._reflog_newest_entry_time(str(reflog))
    assert why is None, why
    assert ts == 1787397771.0, ts


# ── the unparseable case: a deliberate decision, never `idle` ────────────

def test_an_unparseable_reflog_falls_back_to_mtime_not_to_silence(tmp_path: Path) -> None:
    """The hidden judgment call the issue asks for.

    An empty file (or one whose last line does not parse) cannot license
    `idle` by disappearing from evidence. The chosen behaviour: fall back to
    the file's own mtime, which keeps this on the side of the failure #1923
    already showed to be safe — spurious `occupied`, never spurious `idle`.
    """
    reflog = tmp_path / "HEAD"
    reflog.write_text("not a reflog line at all\n", encoding="utf-8")
    ts, why = wt._reflog_newest_entry_time(str(reflog))
    assert ts is None
    assert why

    path, gitdir = _fixture(tmp_path)
    real_reflog = os.path.join(gitdir, "logs", "HEAD")
    with open(real_reflog, "w", encoding="utf-8") as handle:
        handle.write("garbage, not a reflog entry\n")
    now = time.time()

    age, label = wt._newest_write(path, gitdir, now)
    assert age is not None and age < 60, (age, label, "mtime fallback must still answer")
    assert "unreadable" in label, label

    got = wt.assess({"path": path, "gitdir": gitdir}, now=now)
    assert got.state != wt.STATE_IDLE, got


def test_an_empty_reflog_file_does_not_read_as_idle(tmp_path: Path) -> None:
    """Not vacuous against the pre-fix code: an empty file's mtime is fresh
    either way, so `!= idle` alone would pass unmodified code too (a real
    reflog is never empty for a worktree old enough to be `idle`-eligible).
    What only the new code produces is the specific `why` naming the empty
    reflog, carried into `_newest_write`'s fallback evidence line -- the
    pre-fix module has no `_reflog_newest_entry_time` at all.
    """
    path, gitdir = _fixture(tmp_path)
    reflog = os.path.join(gitdir, "logs", "HEAD")
    open(reflog, "w", encoding="utf-8").close()
    now = time.time()

    ts, why = wt._reflog_newest_entry_time(reflog)
    assert ts is None
    assert "no entries" in why, why

    age, label = wt._newest_write(path, gitdir, now)
    assert "no entries" in label, label

    got = wt.assess({"path": path, "gitdir": gitdir}, now=now)
    assert got.state != wt.STATE_IDLE, got


# ── the tail-seek that grows past a mis-landed cut (auditor finding) ─────

def test_a_message_longer_than_the_tail_window_is_still_parsed_correctly(tmp_path: Path) -> None:
    """The tail-seek in `_reflog_newest_entry_time` defaults to 8192 bytes.

    A fixed seek that lands inside the newest entry's own commit message --
    past the tab that marks where the header ends -- once mis-read message
    digits as a plausible but wrong timestamp, silently, with no error at
    all. The header itself is short and near the front of the line; only a
    message long enough to push the true header more than 8192 bytes from
    EOF can trigger this, so the message here is built to do exactly that.
    """
    reflog = tmp_path / "HEAD"
    ts = 1700000000
    long_message = "x" * 20000
    reflog.write_text(
        f"0000 1111 A <a@x.com> {ts} +0000\t commit: {long_message}\n",
        encoding="utf-8",
    )
    got_ts, why = wt._reflog_newest_entry_time(str(reflog))
    assert why is None, why
    assert got_ts == float(ts), (got_ts, "must read the real header, not a message fragment")


def test_a_message_past_the_hard_cap_declines_rather_than_guesses(tmp_path: Path) -> None:
    """Growth is bounded. Past the cap, decline -- never parse a fragment."""
    reflog = tmp_path / "HEAD"
    # No tab anywhere in the tail-sized window even after growing past the
    # cap: a file that is not a reflog at all, well over 1 MiB.
    reflog.write_text("x" * (wt._REFLOG_TAIL_MAX_BYTES + 1000), encoding="utf-8")
    got_ts, why = wt._reflog_newest_entry_time(str(reflog))
    assert got_ts is None
    assert why


# ── forged line separators in an attacker-influenced message (#1130) ─────

def test_a_u2028_in_the_message_cannot_forge_a_second_line(tmp_path: Path) -> None:
    """The register's own question, applied to this reader.

    `str.splitlines()` treats U+2028 / U+2029 as line terminators; git's
    reflog format does not. A commit message ending in one, followed by
    text that itself parses as a plausible header, must not let
    `lines[-1]` become that forged tail instead of the true last physical
    line -- the same "read a fragment, not the real header" shape the
    tail-seek growth test above closes for a different cause.
    """
    reflog = tmp_path / "HEAD"
    real_ts = 1700000000
    forged_tail = "0 1 A <a@x.com> 999999999 +0000"  # looks like a header
    line = f"0000 1111 A <a@x.com> {real_ts} +0000\tcommit: hi {forged_tail}\n"
    reflog.write_bytes(line.encode("utf-8"))

    ts, why = wt._reflog_newest_entry_time(str(reflog))
    assert why is None, why
    assert ts == float(real_ts), (ts, "a forged U+2028 tail must not become the parsed entry")


# ── the sweep: `_newest_write`'s tree walk is content-free by nature ─────

def test_the_tree_walk_signal_is_unaffected_real_file_writes_still_count(tmp_path: Path) -> None:
    """The sweep the issue asks for.

    `_newest_write`'s second half — the walk over the worktree's own files —
    is `os.stat`-based too, but it has no sibling of git's rewrite-without-
    append behaviour: a source file's mtime only moves when something writes
    to it. This is recorded as the outcome of the sweep, not assumed.
    """
    path, gitdir = _fixture(tmp_path)
    (Path(path) / "a.txt").write_text("x", encoding="utf-8")
    now = time.time()
    age, label = wt._newest_write(path, gitdir, now)
    assert age is not None and age < 60, (age, label)
    assert "newest write" in label, label
