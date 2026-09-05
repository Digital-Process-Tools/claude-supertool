"""`git-worktrees`' `cannot tell` conflated a live agent's write with the
caller's own `git merge`/`git push` moments earlier (#2272).

Reproduced on claude-oss: the manager merged `origin/main` into a lane's
worktree itself (no agent involved), and a readiness read of that same tree
moments later returned `cannot tell`, citing the manager's own index write as
the disqualifying signal. The correct downstream rule — `cannot tell` is not
`idle`, never brief a second agent into that tree — was followed exactly, and
the result was a blocked dispatch for a reason unrelated to actual risk.

The fix: a caller who holds outside knowledge ("I just wrote this tree myself
N seconds ago") can declare a known-good cutoff (`since=`), and any occupancy
signal at or before that point is attributed to the caller rather than read as
evidence of a live agent. A write strictly AFTER the declared cutoff is
unaffected — that is the must-fire sibling every must-not-fire case here
needs, so a silent no-op declaration cannot be confused with a real exclusion.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "worktrees.py"
_spec = importlib.util.spec_from_file_location("git_worktrees", PRESET)
assert _spec is not None and _spec.loader is not None
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)


def _entry(tmp_path: Path, **over) -> dict:
    base = {
        "path": str(tmp_path),
        "gitdir": str(tmp_path / ".git"),
        "branch": "fix/2272",
        "detached": False,
        "bare": False,
        "locked": None,
        "prunable": None,
    }
    base.update(over)
    return base


def _silence(monkeypatch):
    monkeypatch.setattr(wt, "_lock_signals", lambda gitdir: [])
    monkeypatch.setattr(wt, "_inprogress_signals", lambda gitdir: [])


# ── `_parse_since` — turning a caller's declaration into a cutoff ────────

def test_parse_since_absolute_epoch() -> None:
    got, why = wt._parse_since("@1000", now=99999.0)
    assert why is None, why
    assert got == 1000.0


def test_parse_since_seconds_ago() -> None:
    got, why = wt._parse_since("90", now=1000.0)
    assert why is None, why
    assert got == 910.0


def test_parse_since_rejects_garbage() -> None:
    got, why = wt._parse_since("banana", now=1000.0)
    assert got is None
    assert why and "banana" in why


def test_parse_since_rejects_negative_duration() -> None:
    got, why = wt._parse_since("-5", now=1000.0)
    assert got is None
    assert why and "negative" in why


# ── `parse_args` — the CLI surface ────────────────────────────────────────

def test_parse_args_extracts_since_raw() -> None:
    path, want_pr, since_raw = wt.parse_args(["/tmp/st-wt/2272", "since=90"])
    assert path == "/tmp/st-wt/2272"
    assert want_pr is True
    assert since_raw == "90"


def test_parse_args_since_absent_is_none() -> None:
    path, want_pr, since_raw = wt.parse_args(["/tmp/st-wt/2272"])
    assert path == "/tmp/st-wt/2272"
    assert since_raw is None


def test_parse_args_still_parses_nopr_alongside_since() -> None:
    path, want_pr, since_raw = wt.parse_args(["/tmp/x", "nopr", "since=@500"])
    assert path == "/tmp/x"
    assert want_pr is False
    assert since_raw == "@500"


# ── `_newest_write` — the mechanism, against real files ───────────────────

def test_newest_write_excludes_a_declared_known_good_write(tmp_path: Path) -> None:
    """MUST NOT FIRE: a write at/before the declared cutoff is not evidence."""
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    index = gitdir / "index"
    index.write_text("x", encoding="utf-8")
    now = time.time()
    write_time = now - 100
    os.utime(index, (write_time, write_time))
    # Declares: "I wrote this myself 90 seconds ago" -> cutoff is now-90,
    # which is AFTER (more recent than) the write at now-100, so it excludes it.
    cutoff = now - 90
    age, label = wt._newest_write(str(tmp_path), str(gitdir), now,
                                  known_good_since=cutoff)
    assert age is not None, label
    # No write survived the filter, so age falls back to elapsed-since-cutoff.
    assert abs(age - 90) < 5, (age, label)
    assert "index" not in label or "declared" in label, label


def test_newest_write_still_reports_a_write_after_the_cutoff(tmp_path: Path) -> None:
    """MUST FIRE: a write strictly after the declared cutoff is still evidence.

    The sibling of the test above — proves the declaration only cancels
    writes at/before it, not every signal in the tree.
    """
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    index = gitdir / "index"
    index.write_text("x", encoding="utf-8")
    now = time.time()
    write_time = now - 10  # AFTER the declared cutoff below
    os.utime(index, (write_time, write_time))
    cutoff = now - 90
    age, label = wt._newest_write(str(tmp_path), str(gitdir), now,
                                  known_good_since=cutoff)
    assert age is not None, label
    assert abs(age - 10) < 5, (age, label)
    assert "index" in label, label


def test_newest_write_without_since_is_unchanged(tmp_path: Path) -> None:
    """Backward compatibility: omitting `known_good_since` changes nothing."""
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    index = gitdir / "index"
    index.write_text("x", encoding="utf-8")
    now = time.time()
    write_time = now - 100
    os.utime(index, (write_time, write_time))
    age, label = wt._newest_write(str(tmp_path), str(gitdir), now)
    assert age is not None
    assert abs(age - 100) < 5


# ── `assess` — the state machine reading the declaration ─────────────────

def test_assess_gap_window_without_declaration_is_cannot_tell(monkeypatch, tmp_path) -> None:
    """MUST NOT FIRE (regression): no declaration, same old ambiguous verdict."""
    _silence(monkeypatch)
    monkeypatch.setattr(
        wt, "_newest_write",
        lambda path, gitdir, now: (1800.0, "index written 30m ago"),
    )
    got = wt.assess(_entry(tmp_path), scan=wt.CwdScan("no", "412 scanned, none inside"))
    assert got.state == wt.STATE_UNKNOWN, got


def test_assess_with_declared_known_good_resolves_to_idle(monkeypatch, tmp_path) -> None:
    """MUST FIRE: the exact reproduction — declaring the caller's own recent
    write resolves what was `cannot tell` into `idle`, because the only
    remaining signal is quiet well past the idle threshold."""
    _silence(monkeypatch)

    def _fake_newest_write(path, gitdir, now, known_good_since=None):
        assert known_good_since is not None
        # Nothing written since the declared cutoff -> quiet for exactly the
        # elapsed time since it, well past IDLE_QUIET_DEFAULT.
        age = now - known_good_since
        return age, f"no write since the declared known-good point ({wt._age(age)} ago)"

    monkeypatch.setattr(wt, "_newest_write", _fake_newest_write)
    now = time.time()
    cutoff = now - (wt.IDLE_QUIET_DEFAULT + 60)
    got = wt.assess(_entry(tmp_path), now=now,
                    scan=wt.CwdScan("no", "412 scanned, none inside"),
                    known_good_since=cutoff)
    assert got.state == wt.STATE_IDLE, got


def test_assess_declaration_does_not_swallow_a_later_write(monkeypatch, tmp_path) -> None:
    """MUST FIRE (positive control on the declaration itself): a live agent's
    write AFTER the caller's declared cutoff must still read as occupied —
    the declaration excludes writes at/before it, never the whole signal."""
    _silence(monkeypatch)

    def _fake_newest_write(path, gitdir, now, known_good_since=None):
        assert known_good_since is not None
        return 30.0, "index written 30s ago"  # inside the active window

    monkeypatch.setattr(wt, "_newest_write", _fake_newest_write)
    now = time.time()
    cutoff = now - 3600
    got = wt.assess(_entry(tmp_path), now=now,
                    scan=wt.CwdScan("no", "412 scanned, none inside"),
                    known_good_since=cutoff)
    assert got.state == wt.STATE_OCCUPIED, got


def test_assess_discloses_the_declaration_in_its_evidence(monkeypatch, tmp_path) -> None:
    """The declaration is disclosed, not applied silently."""
    _silence(monkeypatch)

    def _fake_newest_write(path, gitdir, now, known_good_since=None):
        age = now - known_good_since
        return age, f"no write since the declared known-good point ({wt._age(age)} ago)"

    monkeypatch.setattr(wt, "_newest_write", _fake_newest_write)
    now = time.time()
    cutoff = now - (wt.IDLE_QUIET_DEFAULT + 60)
    got = wt.assess(_entry(tmp_path), now=now,
                    scan=wt.CwdScan("no", "412 scanned, none inside"),
                    known_good_since=cutoff)
    assert any("known-good" in e or "declared" in e for e in got.evidence), got.evidence


# ── `main()` — end to end argument handling ───────────────────────────────

def test_main_refuses_since_without_a_path(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["worktrees.py", "since=90"])
    code = wt.main()
    out = capsys.readouterr().out
    assert code == wt.EXIT_UNKNOWN
    assert "since=" in out
    assert "PATH" in out


def test_main_refuses_unparseable_since(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["worktrees.py", str(tmp_path), "since=nonsense"])
    monkeypatch.setattr(wt, "_git", lambda *a, **k: type("R", (), {
        "stdout": f"worktree {tmp_path}\nHEAD 0000000000000000000000000000000000000000\nbranch refs/heads/fix/2272\n",
        "stderr": "", "returncode": 0})())
    monkeypatch.setattr(wt, "resolve_gitdir", lambda p: str(tmp_path / ".git"))
    code = wt.main()
    out = capsys.readouterr().out
    assert code == wt.EXIT_UNKNOWN
    assert "since=" in out
