"""git-push hazards: #640 (rebase-recovery timeouts), #642 (silent
degradations), #647 (`:watch` accepted and dropped).

Everything that can be a real artifact is one. The two timeout tests do not
mock `subprocess.TimeoutExpired` into existence — they make real git commands
genuinely outlast a real `subprocess.run(timeout=…)`:

  - the **fetch** is pointed at an `ext::` transport helper that sleeps, with
    `remote.origin.pushurl` still on the real bare repo so the push that
    triggers the recovery path is a real rejected push;
  - the **rebase** is stalled by a custom low-level merge driver that sleeps,
    which git invokes *after* `.git/rebase-merge` exists — i.e. the exact
    ordering #640 is about: the clock expires on a worktree git has already
    left paused.

Both assert their fixture actually ran (a sentinel file the helper writes), so
a fixture that cannot spawn fails loudly instead of making the test pass while
testing nothing (#649).

The only stubbed boundary is `query_open_mr` — the network call to glab/gh.
Its return value is API metadata, not a git fact, and a sandbox has no MR.
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_hazards", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)

_HERMETIC_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}

NL = chr(10)


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
                          capture_output=True, text=True, timeout=60)


def _write(cwd: str, fname: str, body: str) -> None:
    Path(cwd, fname).write_text(body, encoding="utf-8")


def _commit(cwd: str, fname: str, body: str, msg: str = "") -> None:
    _write(cwd, fname, body)
    assert _run(["add", fname], cwd).returncode == 0
    assert _run(["commit", "-m", msg or body], cwd).returncode == 0


class _Sandbox:
    """Bare remote + `mine` (repo under test) + `mate` (a collaborator).

    `remote_name` lets a test build the fork/upstream layout #642 is about:
    a branch whose upstream remote is not called `origin`.
    """

    def __init__(self, remote_name: str = "origin") -> None:
        self.tmp = tempfile.mkdtemp(prefix="st_hazard_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.mine = os.path.join(self.tmp, "mine")
        self.mate = os.path.join(self.tmp, "mate")
        self.remote_name = remote_name
        self.sentinel = os.path.join(self.tmp, "fixture_ran")

        assert _run(["init", "--bare", "-b", "master", "remote.git"],
                    self.tmp).returncode == 0
        assert _run(["clone", "-o", remote_name, self.remote, "mine"],
                    self.tmp).returncode == 0
        _commit(self.mine, "a.txt", "base" + NL, "base on remote")
        assert _run(["push", "-u", remote_name, "HEAD:master"],
                    self.mine).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        _commit(self.mine, "f.txt", "feature" + NL, "feature work")
        assert _run(["push", "-u", remote_name, "feature"],
                    self.mine).returncode == 0

    # -- collaborators -----------------------------------------------------

    def _clone_mate(self) -> None:
        if not os.path.isdir(self.mate):
            assert _run(["clone", self.remote, "mate"], self.tmp).returncode == 0

    def advance_remote_feature(self, fname: str = "r.txt",
                               body: str = "theirs" + NL) -> None:
        """A teammate pushes to `feature` — the remote is genuinely ahead."""
        self._clone_mate()
        assert _run(["checkout", "-B", "feature", "origin/feature"],
                    self.mate).returncode == 0
        _commit(self.mate, fname, body, "teammate commit")
        assert _run(["push", "origin", "feature"], self.mate).returncode == 0

    def advance_remote_master(self, n: int = 2) -> None:
        """Master moves on without us — the stale-base condition of #642(1)."""
        self._clone_mate()
        assert _run(["checkout", "-B", "master", "origin/master"],
                    self.mate).returncode == 0
        for i in range(n):
            _commit(self.mate, f"m{i}.txt", f"master {i}" + NL)
        assert _run(["push", "origin", "master"], self.mate).returncode == 0
        assert _run(["fetch", self.remote_name, "master"],
                    self.mine).returncode == 0
        assert _run(["fetch", self.remote_name], self.mine).returncode == 0

    # -- hazard fixtures ---------------------------------------------------

    def _sleeper(self, name: str, seconds: int = 90) -> str:
        """A real python script that records it ran, then sleeps past a budget."""
        script = os.path.join(self.tmp, name)
        Path(script).write_text(
            "import pathlib, time" + NL +
            "pathlib.Path(%r).write_text('ran')" % self.sentinel + NL +
            "time.sleep(%d)" % seconds + NL,
            encoding="utf-8")
        return script

    def make_fetch_hang(self) -> None:
        """Route *fetch* through a sleeping `ext::` helper; push stays real.

        `remote.<name>.pushurl` keeps `git push` on the real bare repo, so the
        non-fast-forward rejection that opens the recovery path is genuine.
        Only the fetch inside `_recover_by_rebase` hits the stalling transport.
        """
        script = self._sleeper("slow_transport.py")
        assert _run(["config", f"remote.{self.remote_name}.pushurl",
                     Path(self.remote).as_posix()], self.mine).returncode == 0
        assert _run(["config", f"remote.{self.remote_name}.url",
                     "ext::%s %s" % (Path(sys.executable).as_posix(),
                                     Path(script).as_posix())],
                    self.mine).returncode == 0
        assert _run(["config", "protocol.ext.allow", "always"],
                    self.mine).returncode == 0

    def make_rebase_hang(self) -> None:
        """Stall the rebase *after* git has paused the worktree.

        A custom low-level merge driver is invoked while `.git/rebase-merge`
        already exists, so the timeout lands on a half-rebased tree — the
        ordering that makes #640 more than a cosmetic traceback.
        """
        script = self._sleeper("slow_merge_driver.py")
        _commit(self.mine, ".gitattributes", "*.txt merge=slow" + NL,
                "slow merge driver")
        assert _run(["push", self.remote_name, "feature"],
                    self.mine).returncode == 0
        assert _run(["config", "merge.slow.name", "slow"],
                    self.mine).returncode == 0
        assert _run(["config", "merge.slow.driver",
                     "%s %s %%A" % (Path(sys.executable).as_posix(),
                                    Path(script).as_posix())],
                    self.mine).returncode == 0
        # Both sides change the same file → the driver is the only way to merge.
        self.advance_remote_feature("c.txt", "theirs" + NL)
        self._clone_mate()
        assert _run(["checkout", "-B", "feature", "origin/feature"],
                    self.mate).returncode == 0
        _commit(self.mate, "c.txt", "theirs again" + NL, "teammate edits c")
        assert _run(["push", "origin", "feature"], self.mate).returncode == 0
        _commit(self.mine, "c.txt", "mine" + NL, "my edit to c")

    # -- observations ------------------------------------------------------

    @property
    def fixture_ran(self) -> bool:
        return os.path.exists(self.sentinel)

    def rebase_in_progress(self) -> bool:
        g = Path(self.mine, ".git")
        return (g / "rebase-merge").exists() or (g / "rebase-apply").exists()

    def head(self) -> str:
        return _run(["rev-parse", "HEAD"], self.mine).stdout.strip()

    def remotes(self) -> list[str]:
        return _run(["remote"], self.mine).stdout.split()

    def drive_push(self, *argv: str) -> tuple[int, str]:
        prev_cwd = os.getcwd()
        prev_argv = sys.argv[:]
        prev_env = {k: os.environ.get(k) for k in _HERMETIC_ENV}
        os.chdir(self.mine)
        os.environ.update({k: v for k, v in _HERMETIC_ENV.items() if v is not None})
        sys.argv = ["push.py", *argv]
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = push.main()
        finally:
            os.chdir(prev_cwd)
            sys.argv = prev_argv
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, buf.getvalue()

    def close(self) -> None:
        if self.rebase_in_progress():
            _run(["rebase", "--abort"], self.mine)
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture
def box():
    s = _Sandbox()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def fork_box():
    """Upstream remote is NOT called `origin` — the #642(1) layout."""
    s = _Sandbox(remote_name="upstream")
    try:
        yield s
    finally:
        s.close()


def _no_mr():
    return mock.patch.object(push, "query_open_mr", return_value=None)


def _mr(target: str = "master", iid: int = 7):
    return mock.patch.object(push, "query_open_mr", return_value={
        "source": "gitlab", "iid": iid, "target": target,
        "pipeline": None, "pipeline_id": None, "pipeline_url": None,
        "merge_status": "can_be_merged"})


def _last_result(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "no output at all"
    assert lines[-1].startswith("[result] "), (
        "the verdict must be the LAST line (#638/#623):" + NL + out)
    return lines[-1]


# ===========================================================================
# #640 — TimeoutExpired on the rebase-recovery path
# ===========================================================================

def test_fetch_timeout_gives_a_verdict_not_a_traceback(box, monkeypatch) -> None:
    """A fetch that outlasts its budget must produce a receipt, not a stack trace.

    The tree was never touched here, and the verdict has to say so — the
    caller's next move differs entirely from the paused-rebase case.
    """
    box.advance_remote_feature()
    box.make_fetch_hang()
    monkeypatch.setattr(push, "_RECOVER_TIMEOUT", 3, raising=False)
    before = box.head()

    with _no_mr():
        rc, out = box.drive_push()

    assert box.fixture_ran, (
        "the ext:: transport helper never spawned — this test would prove nothing")
    assert rc != 0
    assert not box.rebase_in_progress()
    assert box.head() == before
    verdict = _last_result(out)
    assert "TIMED OUT" in verdict
    assert "fetch" in verdict.lower()
    # The one thing the caller needs: is my worktree ok?
    assert "not started" in verdict.lower() or "unchanged" in verdict.lower(), verdict


def test_rebase_timeout_names_the_paused_worktree_and_the_way_out(
        box, monkeypatch) -> None:
    """The severe case: the clock expires on a tree git has already paused.

    A traceback here leaves the caller with a half-rebased worktree and no
    statement of it. The receipt must name the state and both exits.
    """
    box.make_rebase_hang()
    monkeypatch.setattr(push, "_RECOVER_TIMEOUT", 5, raising=False)

    with _no_mr():
        rc, out = box.drive_push()

    assert box.fixture_ran, (
        "the merge driver never spawned — this test would prove nothing")
    assert box.rebase_in_progress(), (
        "fixture did not reproduce the hazard: git never paused the rebase")
    assert rc != 0
    verdict = _last_result(out)
    assert "TIMED OUT" in verdict
    assert "REBASE IN PROGRESS" in verdict.upper(), verdict
    assert "git rebase --continue" in out
    assert "git rebase --abort" in out


def test_rebase_state_that_cannot_be_read_is_stated_not_guessed() -> None:
    """Three states, not two: unknown is an answer, not a default to 'clean'."""
    assert push._rebase_state() in ("in-progress", "not-started")
    with mock.patch.object(push, "_git",
                           side_effect=subprocess.TimeoutExpired("git", 1)):
        assert push._rebase_state() == "unknown"
    with mock.patch.object(push, "_git",
                           return_value=mock.Mock(stdout="", returncode=1)):
        assert push._rebase_state() == "unknown"


# ===========================================================================
# #642(1) — the stale-base check hardcodes `origin`
# ===========================================================================

def test_stale_base_check_follows_the_branch_upstream_remote(fork_box) -> None:
    """Upstream is `upstream`, not `origin` — the warning must still fire.

    Before the fix `rev-list --count HEAD..origin/master` fails on a repo with
    no `origin`, the count is skipped, and a two-commit-stale base renders
    exactly like a fresh one.
    """
    assert "origin" not in fork_box.remotes(), "fixture must have no `origin`"
    fork_box.advance_remote_master(2)

    with _mr("master"):
        rc, out = fork_box.drive_push()

    assert rc == 0, out
    assert "2 commit(s) behind upstream/master" in out, out


def test_unresolvable_target_says_the_check_was_skipped(fork_box) -> None:
    """Printing nothing must not be able to mean both 'fresh' and 'unknown'."""
    with _mr("no-such-branch"):
        rc, out = fork_box.drive_push()

    assert rc == 0, out
    low = out.lower()
    assert "stale-base" in low or "stale base" in low, out
    assert "skip" in low, out
    assert "upstream/no-such-branch" in out, out


def test_fresh_base_stays_quiet(fork_box) -> None:
    """Silence is reserved for the one absence it can honestly describe."""
    with _mr("master"):
        rc, out = fork_box.drive_push()

    assert rc == 0, out
    assert "behind" not in out.lower().replace("ahead 0, behind 0", "")
    assert "skip" not in out.lower(), out


# ===========================================================================
# #647 — `:watch` accepted and dropped; unknown flags vanish
# ===========================================================================

def test_watch_is_a_known_flag() -> None:
    """The syntax string in presets/git.json advertises it; the parser must too."""
    assert "watch" in push._parse_flags(["watch"])


def test_unknown_flag_is_refused_before_anything_is_pushed(box) -> None:
    """A typo'd flag must not be discarded — and must not push either.

    `git-push:no-verifyy` used to run an ordinary verified push while the
    caller believed the hook was skipped.
    """
    before_remote = _run(["rev-parse", "feature"], box.remote).stdout.strip()
    _commit(box.mine, "new.txt", "unpushed" + NL, "not on the remote yet")

    with _no_mr():
        rc, out = box.drive_push("no-verifyy")

    assert rc != 0, out
    assert "no-verifyy" in out, out
    assert "force-with-lease" in out and "no-verify" in out, (
        "the refusal must list what IS accepted:" + NL + out)
    after_remote = _run(["rev-parse", "feature"], box.remote).stdout.strip()
    assert after_remote == before_remote, "refused flag still pushed"
    verdict = _last_result(out)
    assert "NOT PUSHED" in verdict


def test_known_flags_still_parse() -> None:
    assert push._parse_flags(["force-with-lease", "no-verify", "watch"]) == {
        "force-with-lease", "no-verify", "watch"}
    assert push._parse_flags(["FORCE-WITH-LEASE"]) == {"force-with-lease"}
    assert push._parse_flags(["", "  "]) == set()


# ===========================================================================
# #642(2) — `_spawn_watch` cannot find the wrapper in a worktree
# ===========================================================================

def test_watch_spawns_through_supertool_py_when_the_wrapper_is_missing(
        tmp_path, monkeypatch) -> None:
    """The gitignored `./supertool` symlink does not exist in a worktree.

    `python3 supertool.py` does, and it is the working invocation there. Real
    Popen, real child process, real sentinel — no mock of the spawn.
    """
    root = tmp_path / "root"
    root.mkdir()
    sentinel = tmp_path / "spawned"
    (root / "supertool.py").write_text(
        "import pathlib, sys" + NL +
        "pathlib.Path(%r).write_text(' '.join(sys.argv[1:]))" % str(sentinel) + NL,
        encoding="utf-8")
    assert not (root / "supertool").exists()
    monkeypatch.setattr(push, "_repo_root", lambda: str(root))

    ok, how = push._spawn_watch("gitlab-mr", "42")

    assert ok, how
    for _ in range(200):
        if sentinel.exists():
            break
        __import__("time").sleep(0.05)
    assert sentinel.exists(), "the watcher was reported spawned but never ran"
    assert sentinel.read_text(encoding="utf-8").strip() == "watch:gitlab-mr:42"


def test_watch_spawn_failure_is_reported_not_swallowed(tmp_path,
                                                       monkeypatch, capsys) -> None:
    """`:watch` is an explicit request; a request that vanishes is worse than one that errors."""
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setattr(push, "_repo_root", lambda: str(root))

    with mock.patch.object(push, "_git",
                           return_value=mock.Mock(stdout="", returncode=1)):
        push._post_push_advisories(
            {"source": "gitlab", "iid": 42, "target": "master"},
            {"watch"}, "origin")
    out = capsys.readouterr().out

    assert "watch" in out.lower()
    assert "could not" in out.lower() or "failed" in out.lower(), out
    assert "watch:gitlab-mr:42" in out, "must still hand back the manual command"


def test_watch_requested_with_no_open_mr_still_says_so(capsys, monkeypatch) -> None:
    """Nothing to watch is a state; silence is not a way to report it."""
    with mock.patch.object(push, "_git",
                           return_value=mock.Mock(stdout="", returncode=1)):
        push._post_push_advisories(None, {"watch"}, "origin")
    out = capsys.readouterr().out
    assert "watch" in out.lower(), out
    assert "no open" in out.lower() or "no mr" in out.lower(), out

def test_watch_spawn_oserror_is_reported_not_claimed_as_started(
        tmp_path, monkeypatch) -> None:
    """A Popen that raises must not come back as `started`.

    Popen is stubbed here because there is no portable way to make a real
    execve fail identically on macOS, Linux and Windows — the boundary being
    faked is the OS call itself, nothing above it.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "supertool.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(push, "_repo_root", lambda: str(root))
    monkeypatch.setattr(push.subprocess, "Popen",
                        mock.Mock(side_effect=OSError("Exec format error")))

    started, how = push._spawn_watch("gitlab-mr", "42")

    assert started is False
    assert "Exec format error" in how
