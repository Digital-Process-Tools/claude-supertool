"""A `git status` configured not to look authorised a deletion (#1290).

`_worktree_dirt` ran `git status --porcelain --ignored` with **inherited**
config. `status.showUntrackedFiles=no` — an ordinary user or repo preference,
not an exotic setting — suppresses `!!` records as well as `??`, so the gate
that shipped in #1280 received an empty list, could not tell "nothing there"
from "not looked", and authorised the removal. The receipt then asserted, in
the same sentence that reported the deletion, that `git status --ignored` had
found nothing.

Two things are pinned here, and only the first is about this instance:

* the display knobs go on the command line, where they outrank the config
  files and `GIT_CONFIG_*` both;
* an empty answer here is the authorisation for a destructive act, so it has
  to come from two reads that fail in different ways — `git status`, the only
  one that sees modified tracked files, and `git ls-files --others`, which is
  plumbing and has no display setting to turn off. **Either** read failing is
  `could not check`, never `checked and found nothing`.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_merge.py"
_spec = importlib.util.spec_from_file_location("gh_pr_merge_dirt_1290", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class Reads:
    """Stands in for `_git`, answering the two reads independently."""

    def __init__(self, *, status_out="", status_rc=0, status_raise=None,
                 ls_out="", ls_rc=0, ls_raise=None):
        self.status_out = status_out
        self.status_rc = status_rc
        self.status_raise = status_raise
        self.ls_out = ls_out
        self.ls_rc = ls_rc
        self.ls_raise = ls_raise
        self.calls: list = []

    def __call__(self, args, timeout=30):
        args = list(args)
        self.calls.append(args)
        if "status" in args:
            if self.status_raise is not None:
                raise self.status_raise
            return subprocess.CompletedProcess(
                args, self.status_rc, self.status_out,
                "" if self.status_rc == 0 else "the read blew up")
        if "ls-files" in args:
            if self.ls_raise is not None:
                raise self.ls_raise
            return subprocess.CompletedProcess(
                args, self.ls_rc, self.ls_out,
                "" if self.ls_rc == 0 else "the read blew up")
        return subprocess.CompletedProcess(args, 0, "", "")

    def removals(self):
        return [c for c in self.calls if c[:2] == ["worktree", "remove"]]

    def read(self, name):
        return [c for c in self.calls if name in c]


def arm(monkeypatch, reads: Reads, path="/w/fix"):
    monkeypatch.setattr(m, "_git", reads)
    monkeypatch.setattr(m, "_worktrees_for_branch", lambda head: [path])
    monkeypatch.setattr(m, "_worktree_state", lambda p: "idle")
    return lambda: m._cleanup_worktree("fix/1290")


# ---------------------------------------------------------------------------
# The reproduction, against real git
# ---------------------------------------------------------------------------

def test_a_status_configured_not_to_look_does_not_authorise_a_removal(
        tmp_path, monkeypatch) -> None:
    main = tmp_path / "main"
    main.mkdir()

    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=str(cwd or main), check=True,
            capture_output=True, text=True, encoding="utf-8", errors="replace")

    git("init", "-q", "-b", "master")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (main / ".gitignore").write_text("secret.env\n", encoding="utf-8")
    git("add", ".gitignore")
    git("commit", "-qm", "init")

    wt = tmp_path / "w1"
    git("worktree", "add", "-q", str(wt), "-b", "b1")
    secret = wt / "secret.env"
    secret.write_text("S3CRET\n", encoding="utf-8")
    git("config", "status.showUntrackedFiles", "no", cwd=wt)

    # The tree really is invisible to an inherited-config read.
    blind = subprocess.run(
        ["git", "-C", str(wt), "status", "--porcelain", "--ignored"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert blind.stdout.strip() == "", "the premise no longer reproduces"

    monkeypatch.setattr(m, "_worktrees_for_branch", lambda head: [str(wt)])
    monkeypatch.setattr(m, "_worktree_state", lambda p: "idle")
    monkeypatch.chdir(main)

    _item, state, detail = m._cleanup_worktree("b1")
    assert state == m.CLEAN_REFUSED, detail
    assert secret.exists(), "the ignored file the gate exists to protect"
    assert "secret.env" in detail


# ---------------------------------------------------------------------------
# The pin, and the second mechanism
# ---------------------------------------------------------------------------

def test_the_display_knobs_are_set_on_the_command_line(monkeypatch) -> None:
    r = Reads()
    monkeypatch.setattr(m, "_git", r)
    m._worktree_dirt("/w/fix")
    status = r.read("status")
    assert status, "nothing asked the tree what it holds"
    argv = status[0]
    assert "-c" in argv
    assert "status.showUntrackedFiles=normal" in argv
    assert "--untracked-files=normal" in argv
    assert "--ignored" in argv


def test_the_untracked_half_is_also_read_by_plumbing(monkeypatch) -> None:
    r = Reads()
    monkeypatch.setattr(m, "_git", r)
    m._worktree_dirt("/w/fix")
    ls = r.read("ls-files")
    assert ls, "only the configurable read was performed"
    assert "--others" in ls[0] and "/w/fix" in ls[0]


def test_the_plumbing_read_sees_what_a_suppressed_status_does_not(
        monkeypatch) -> None:
    r = Reads(status_out="", ls_out="secret.env\nvenv/\n")
    run = arm(monkeypatch, r)
    _item, state, detail = run()
    assert state == m.CLEAN_REFUSED
    assert "secret.env" in detail
    assert r.removals() == []


def test_the_two_reads_are_unioned_rather_than_one_shadowing_the_other(
        monkeypatch) -> None:
    r = Reads(status_out=" M tracked.py\n", ls_out="secret.env\n")
    monkeypatch.setattr(m, "_git", r)
    dirt, err = m._worktree_dirt("/w/fix")
    assert err == ""
    assert set(dirt) == {"tracked.py", "secret.env"}


# ---------------------------------------------------------------------------
# The third state: a read that did not happen
# ---------------------------------------------------------------------------

BROKEN = [
    pytest.param({"ls_raise": FileNotFoundError("[WinError 2] git")},
                 id="ls-spawn"),
    pytest.param({"ls_raise": subprocess.TimeoutExpired("git", 30)},
                 id="ls-timeout"),
    pytest.param({"ls_rc": 128}, id="ls-exit"),
    pytest.param({"status_raise": FileNotFoundError("[WinError 2] git")},
                 id="status-spawn"),
    pytest.param({"status_raise": subprocess.TimeoutExpired("git", 30)},
                 id="status-timeout"),
    pytest.param({"status_rc": 128}, id="status-exit"),
]


@pytest.mark.parametrize("kw", BROKEN)
def test_a_read_that_could_not_be_performed_refuses(monkeypatch, kw) -> None:
    r = Reads(**kw)
    run = arm(monkeypatch, r)
    _item, state, detail = run()
    assert state == m.CLEAN_REFUSED, detail
    assert r.removals() == []
    assert "could not be read" in detail


@pytest.mark.parametrize("kw", BROKEN)
def test_a_read_that_did_not_happen_is_never_reported_as_a_clean_tree(
        monkeypatch, kw) -> None:
    r = Reads(**kw)
    run = arm(monkeypatch, r)
    _item, _state, detail = run()
    assert "found nothing" not in detail
    assert "destroyed nothing" not in detail


@pytest.mark.parametrize("kw", BROKEN)
def test_a_failed_read_names_which_command_failed(monkeypatch, kw) -> None:
    r = Reads(**kw)
    monkeypatch.setattr(m, "_git", r)
    dirt, err = m._worktree_dirt("/w/fix")
    assert dirt == []
    expected = "ls-files" if any(k.startswith("ls") for k in kw) else "status"
    assert expected in err, err


# ---------------------------------------------------------------------------
# The receipt states what was established, and nothing wider
# ---------------------------------------------------------------------------

def test_the_receipt_names_both_reads_it_actually_performed(monkeypatch) -> None:
    r = Reads()
    run = arm(monkeypatch, r)
    _item, state, detail = run()
    assert state == m.CLEAN_DONE, detail
    assert "status" in detail and "ls-files" in detail
    assert len(r.removals()) == 1


def test_no_receipt_credits_a_single_status_read_with_the_whole_guarantee(
) -> None:
    src = MOD_PATH.read_text(encoding="utf-8")
    assert "`git status --ignored` found " not in src
