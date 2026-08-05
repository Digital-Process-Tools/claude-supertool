"""`git-worktrees` — is an agent working in this worktree? (#860)

The incident this file guards against: `ps aux | grep <worktree path>` returned
zero for a worktree that had a live agent in it — the path is not in that
process's argv — the zero was read as "dead", and a second agent was delegated
into the occupied tree. Two agents wrote through one index for two minutes.

So the assertions below are mostly about the **third state**. A checker that
answers only `occupied` / `not occupied` reproduces the bug exactly: "no
evidence of an agent" is precisely what the `ps` grep already said. Every test
that ends in `idle` has a sibling that ends in `cannot tell`, and the
difference between them is never "was there a signal" — it is "did the one
probe that can license an absence actually answer".
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


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
        "branch": "fix/860",
        "detached": False,
        "bare": False,
        "locked": None,
        "prunable": None,
    }
    base.update(over)
    return base


def _silence(monkeypatch, *, newest_age: float | None = 86400.0, scan=None):
    """Every inference probe quiet; the cwd scan is what the test varies."""
    monkeypatch.setattr(wt, "_lock_signals", lambda gitdir: [])
    monkeypatch.setattr(wt, "_inprogress_signals", lambda gitdir: [])
    monkeypatch.setattr(
        wt, "_newest_write",
        lambda path, gitdir, now: (newest_age, "newest write: docs/x.md 1d ago")
        if newest_age is not None else (None, "could not stat the worktree"),
    )
    if scan is not None:
        monkeypatch.setattr(wt, "_cwd_scan", lambda path: scan)


# ── the third state ──────────────────────────────────────────────────────

def test_no_signals_plus_unanswerable_cwd_scan_is_cannot_tell(monkeypatch, tmp_path) -> None:
    """The incident, in one assertion.

    Nothing found + a probe that could not run must NOT be `idle`. This is the
    `ps | grep` result verbatim: an absence of evidence produced by a check
    that was never able to see the thing it was looking for.
    """
    _silence(monkeypatch, scan=wt.CwdScan("unknown", "lsof not installed (darwin) — cannot scan process cwds"))
    got = wt.assess(_entry(tmp_path))
    assert got.state == wt.STATE_UNKNOWN, got
    assert got.state != wt.STATE_IDLE


def test_no_signals_plus_answered_cwd_scan_is_idle(monkeypatch, tmp_path) -> None:
    """Same absence of signals — but this time a probe positively answered."""
    _silence(monkeypatch, scan=wt.CwdScan("no", "no process cwd inside (412 scanned)"))
    got = wt.assess(_entry(tmp_path))
    assert got.state == wt.STATE_IDLE, got


def test_the_two_states_are_distinguishable_by_name(monkeypatch, tmp_path) -> None:
    """A two-state implementation passes every `occupied` test in this file.

    It cannot pass this one: the same tree, the same silence, two different
    verdicts, decided solely by whether the scan answered.
    """
    _silence(monkeypatch, scan=wt.CwdScan("no", "no process cwd inside (412 scanned)"))
    idle = wt.assess(_entry(tmp_path))
    _silence(monkeypatch, scan=wt.CwdScan("unknown", "lsof timed out after 10s"))
    unknown = wt.assess(_entry(tmp_path))
    assert idle.state != unknown.state
    assert {idle.state, unknown.state} == {wt.STATE_IDLE, wt.STATE_UNKNOWN}


def test_unstattable_tree_is_cannot_tell_even_when_scan_says_no(monkeypatch, tmp_path) -> None:
    """`idle` needs every probe to have answered, not just the strongest one."""
    _silence(monkeypatch, newest_age=None, scan=wt.CwdScan("no", "no process cwd inside (412 scanned)"))
    got = wt.assess(_entry(tmp_path))
    assert got.state == wt.STATE_UNKNOWN, got


def test_recently_quiet_but_not_long_enough_is_cannot_tell(monkeypatch, tmp_path) -> None:
    """A live finding, pinned.

    On the fleet this op was built against, two worktrees written to 7 and 12
    minutes earlier had no process whose cwd was inside them — an agent's
    parent process need not be chdir'd into the tree it edits. So an empty cwd
    scan does not prove absence, and a tree that has merely stopped being
    *active* is not yet `idle`.
    """
    _silence(monkeypatch, newest_age=1200.0,
             scan=wt.CwdScan("no", "no process cwd inside (521 scanned)"))
    got = wt.assess(_entry(tmp_path))
    assert got.state == wt.STATE_UNKNOWN, got
    assert any("does not prove absence" in e for e in got.evidence), got.evidence


def test_idle_needs_the_longer_quiet_window(monkeypatch, tmp_path) -> None:
    _silence(monkeypatch, newest_age=wt.IDLE_QUIET_DEFAULT + 60,
             scan=wt.CwdScan("no", "no process cwd inside (521 scanned)"))
    assert wt.assess(_entry(tmp_path)).state == wt.STATE_IDLE


def test_cannot_tell_names_why_it_could_not_tell(monkeypatch, tmp_path) -> None:
    _silence(monkeypatch, scan=wt.CwdScan("unknown", "lsof not installed (win32) — cannot scan process cwds"))
    got = wt.assess(_entry(tmp_path))
    assert any("lsof not installed" in e for e in got.evidence), got.evidence


# ── occupied, and it names its evidence ──────────────────────────────────

def test_process_cwd_inside_is_occupied_and_names_the_pid(monkeypatch, tmp_path) -> None:
    """The signal the `ps` grep missed: the path is in the process's *cwd*."""
    _silence(monkeypatch, scan=wt.CwdScan("yes", "pid 51234 (claude) has cwd inside", pids=["51234"]))
    got = wt.assess(_entry(tmp_path))
    assert got.state == wt.STATE_OCCUPIED
    assert any("51234" in e for e in got.evidence), got.evidence


def test_index_lock_is_occupied(monkeypatch, tmp_path) -> None:
    _silence(monkeypatch, scan=wt.CwdScan("no", "no process cwd inside (412 scanned)"))
    monkeypatch.setattr(wt, "_lock_signals", lambda gitdir: ["index.lock present (held 2s)"])
    got = wt.assess(_entry(tmp_path))
    assert got.state == wt.STATE_OCCUPIED
    assert any("index.lock" in e for e in got.evidence)


def test_positive_signal_beats_a_scan_that_found_nobody(monkeypatch, tmp_path) -> None:
    """index.lock is transient but it is proof; an empty scan is not a refutation."""
    _silence(monkeypatch, scan=wt.CwdScan("no", "no process cwd inside (412 scanned)"))
    monkeypatch.setattr(wt, "_inprogress_signals", lambda gitdir: ["rebase in progress"])
    assert wt.assess(_entry(tmp_path)).state == wt.STATE_OCCUPIED


def test_recent_write_is_occupied_and_names_the_age(monkeypatch, tmp_path) -> None:
    _silence(monkeypatch, newest_age=40.0, scan=wt.CwdScan("no", "no process cwd inside"))
    monkeypatch.setattr(
        wt, "_newest_write",
        lambda path, gitdir, now: (40.0, "HEAD moved 40s ago"),
    )
    got = wt.assess(_entry(tmp_path))
    assert got.state == wt.STATE_OCCUPIED
    assert any("40s ago" in e for e in got.evidence), got.evidence


def test_git_worktree_lock_is_occupied(monkeypatch, tmp_path) -> None:
    """git's own announce mechanism — `git worktree lock` — is read, not reinvented."""
    _silence(monkeypatch, scan=wt.CwdScan("no", "no process cwd inside"))
    got = wt.assess(_entry(tmp_path, locked="agent 860 working here"))
    assert got.state == wt.STATE_OCCUPIED
    assert any("agent 860 working here" in e for e in got.evidence), got.evidence


def test_every_verdict_names_evidence(monkeypatch, tmp_path) -> None:
    """A bare verdict is the thing that misled the reporter."""
    for scan in (
        wt.CwdScan("yes", "pid 1 has cwd inside", pids=["1"]),
        wt.CwdScan("no", "no process cwd inside (412 scanned)"),
        wt.CwdScan("unknown", "lsof timed out after 10s"),
    ):
        _silence(monkeypatch, scan=scan)
        got = wt.assess(_entry(tmp_path))
        assert got.evidence, f"{got.state} rendered with no evidence"
        assert all(e.strip() for e in got.evidence)


# ── the cwd scan itself ──────────────────────────────────────────────────

def test_cwd_scan_timeout_is_unknown_not_absence(monkeypatch, tmp_path) -> None:
    """The bug one layer down: a probe that stalled must not read as 'nobody'."""
    monkeypatch.setattr(wt, "_have_proc", lambda: False)
    monkeypatch.setattr(wt.shutil, "which", lambda name: "/usr/bin/lsof")

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="lsof", timeout=10)
    monkeypatch.setattr(wt.subprocess, "run", _boom)
    got = wt._cwd_scan(str(tmp_path))
    assert got.answer == "unknown", got
    assert "10" in got.detail


def test_cwd_scan_without_lsof_or_proc_declines_naming_the_platform(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(wt, "_have_proc", lambda: False)
    monkeypatch.setattr(wt.shutil, "which", lambda name: None)
    got = wt._cwd_scan(str(tmp_path))
    assert got.answer == "unknown"
    assert sys.platform in got.detail


def test_cwd_scan_parses_lsof_and_matches_only_inside(monkeypatch, tmp_path) -> None:
    inside = tmp_path / "wt"
    inside.mkdir()
    sibling = tmp_path / "wt-other"
    sibling.mkdir()
    monkeypatch.setattr(wt, "_have_proc", lambda: False)
    monkeypatch.setattr(wt.shutil, "which", lambda name: "/usr/bin/lsof")
    out = f"p111\nn{sibling}\np222\nn{inside}/sub\np333\nn/tmp\n"
    monkeypatch.setattr(
        wt.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, out, ""),
    )
    got = wt._cwd_scan(str(inside))
    assert got.answer == "yes"
    assert got.pids == ["222"], got
    assert "222" in got.detail


def test_cwd_scan_empty_lsof_output_is_unknown_not_no(monkeypatch, tmp_path) -> None:
    """No rows at all means the tool did not speak, not that the machine is idle."""
    monkeypatch.setattr(wt, "_have_proc", lambda: False)
    monkeypatch.setattr(wt.shutil, "which", lambda name: "/usr/bin/lsof")
    monkeypatch.setattr(
        wt.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "lsof: WARNING"),
    )
    got = wt._cwd_scan(str(tmp_path))
    assert got.answer == "unknown", got


# ── porcelain parsing ────────────────────────────────────────────────────

def test_parse_worktree_list() -> None:
    text = (
        "worktree /repo\n"
        "HEAD aaaa\n"
        "branch refs/heads/master\n"
        "\n"
        "worktree /repo/../st-wt/860\n"
        "HEAD bbbb\n"
        "branch refs/heads/fix/860\n"
        "locked agent working\n"
        "\n"
        "worktree /repo/../st-wt/532\n"
        "HEAD cccc\n"
        "detached\n"
        "prunable gitdir file points to non-existent location\n"
    )
    got = wt.parse_worktree_list(text)
    assert [e["branch"] for e in got] == ["master", "fix/860", None]
    assert got[1]["locked"] == "agent working"
    assert got[2]["detached"] is True
    assert got[2]["prunable"].startswith("gitdir file")


def test_parse_worktree_list_locked_without_reason() -> None:
    got = wt.parse_worktree_list("worktree /a\nHEAD x\nbranch refs/heads/b\nlocked\n")
    assert got[0]["locked"] == ""  # locked, reason not given — not None


# ── rendering ────────────────────────────────────────────────────────────

def test_render_prints_state_and_evidence_per_row() -> None:
    rows = [
        (
            {"path": "/w/860", "branch": "fix/860"},
            wt.Assessment(wt.STATE_OCCUPIED, ["pid 51234 (claude) has cwd inside"]),
        ),
        (
            {"path": "/w/700", "branch": "fix/700"},
            wt.Assessment(wt.STATE_UNKNOWN, ["lsof not installed (win32)"]),
        ),
    ]
    text = wt.render(rows, merged=None, merged_why="skipped — no base ref")
    assert "occupied" in text and "cannot tell" in text
    assert "pid 51234" in text and "lsof not installed" in text
    assert "fix/860" in text and "/w/860" in text


def test_render_footer_says_cannot_tell_is_not_free() -> None:
    rows = [({"path": "/w/1", "branch": "b"}, wt.Assessment(wt.STATE_UNKNOWN, ["x"]))]
    text = wt.render(rows, merged=None, merged_why="skipped")
    assert "cannot tell" in text
    assert "not" in text.lower().split("[result]")[-1]


def test_render_never_prints_a_removal_command() -> None:
    """Inspection only. A destructive suggestion in an ambiguous report is how
    an occupied tree gets removed."""
    rows = [({"path": "/w/1", "branch": "b"}, wt.Assessment(wt.STATE_IDLE, ["x"]))]
    text = wt.render(rows, merged=set(), merged_why="")
    assert "worktree remove" not in text


# ── end to end, against a real worktree ──────────────────────────────────

def _git(*args, cwd):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("GIT_"):
            env.pop(k)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    })
    return subprocess.run(["git", *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.fixture()
def repo_with_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-b", "master", ".", cwd=main)
    (main / "f.txt").write_text("hi\n")
    _git("add", "f.txt", cwd=main)
    _git("commit", "-m", "init", cwd=main)
    tree = tmp_path / "wt-a"
    _git("worktree", "add", "-b", "feat", str(tree), cwd=main)
    return main, tree


def test_end_to_end_index_lock_reports_occupied(repo_with_worktree) -> None:
    main, tree = repo_with_worktree
    gitdir = wt.resolve_gitdir(str(tree))
    assert gitdir and Path(gitdir).exists(), gitdir
    (Path(gitdir) / "index.lock").write_text("")
    try:
        got = wt.assess({"path": str(tree), "gitdir": gitdir, "branch": "feat",
                         "locked": None, "prunable": None, "detached": False,
                         "bare": False})
        assert got.state == wt.STATE_OCCUPIED
        assert any("index.lock" in e for e in got.evidence), got.evidence
    finally:
        (Path(gitdir) / "index.lock").unlink()


def test_end_to_end_script_lists_both_trees(repo_with_worktree) -> None:
    main, tree = repo_with_worktree
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("GIT_"):
            env.pop(k)
    res = subprocess.run([sys.executable, str(PRESET)], cwd=str(main), env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "git-worktrees" in res.stdout
    assert str(tree) in res.stdout
    assert "feat" in res.stdout


def test_end_to_end_single_path_exit_code_flags_occupied(repo_with_worktree) -> None:
    """Exit 0 must mean 'safe to use'. Occupied and cannot-tell must not."""
    main, tree = repo_with_worktree
    gitdir = wt.resolve_gitdir(str(tree))
    (Path(gitdir) / "index.lock").write_text("")
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("GIT_"):
            env.pop(k)
    try:
        res = subprocess.run([sys.executable, str(PRESET), str(tree)], cwd=str(main),
                             env=env, capture_output=True, text=True, timeout=60)
    finally:
        (Path(gitdir) / "index.lock").unlink()
    assert res.returncode == wt.EXIT_OCCUPIED, (res.returncode, res.stdout)
    assert "occupied" in res.stdout


def test_leading_dash_path_is_refused(repo_with_worktree) -> None:
    main, _tree = repo_with_worktree
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("GIT_"):
            env.pop(k)
    res = subprocess.run([sys.executable, str(PRESET), "--upload-pack=touch x"],
                         cwd=str(main), env=env, capture_output=True, text=True, timeout=30)
    assert res.returncode != 0
    assert "refus" in res.stdout.lower() or "refus" in res.stderr.lower()


# ── shipped means findable ───────────────────────────────────────────────

def test_op_is_registered_in_the_git_preset() -> None:
    manifest = json.loads((ROOT / "presets" / "git.json").read_text(encoding="utf-8"))
    assert "git-worktrees" in manifest["ops"]
    entry = manifest["ops"]["git-worktrees"]
    assert "worktrees.py" in entry["cmd"]
    assert "cannot tell" in entry["description"]


def test_op_is_documented() -> None:
    doc = (ROOT / "docs" / "presets" / "git.md").read_text(encoding="utf-8")
    assert "git-worktrees" in doc
    assert "cannot tell" in doc
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "git-worktrees" in changelog
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "git-worktrees" in readme
