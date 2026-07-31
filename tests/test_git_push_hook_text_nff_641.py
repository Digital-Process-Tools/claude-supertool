"""#641: a pre-push hook's own words must not be able to trigger a rebase.

`_is_non_fast_forward` decided "the remote rejected this push as
non-fast-forward" by scanning the *merged* stdout+stderr of the push
subprocess for `fetch first` / `non-fast-forward` / `tip of your current
branch is behind`. A pre-push hook writes to exactly those streams, so a hook
that prints any of those phrases as its own advice — a policy hook, a linter,
a test harness echoing git's hint — was enough to make the op conclude the
remote had moved ahead.

The consequence is not a wrong message. `_recover_by_rebase` runs `git fetch`
and then `git rebase` on the caller's branch, so the op **rewrites local
history** on the strength of a substring appearing in text it did not produce.
Reproduced end to end before the fix: with the remote one commit ahead and a
hook that blocks the push while printing `fetch first`, HEAD came back a
different SHA and the receipt said `REJECTED after a clean rebase` — the push
never reached the remote at all.

The invariant these tests pin is stronger than "match the phrase more
carefully": **hook output must not be able to reach the predicate.** The fix
reads git's own machine-readable channel — `git push --porcelain`, whose
per-ref status lines go to stdout in a fixed `flag TAB from:to TAB summary`
grammar — and matches the status line for our ref. A hook-blocked push emits
no such line at all (git never contacted the remote), so the undetermined case
falls through to the loud rejection instead of into a history rewrite, which
is the three-state contract in docs/validators.md.

Everything is hermetic: a bare "remote" plus working clones in a tmp dir, no
network, no real remote, self-cleaning. The hook is a `#!/bin/sh` shim that
execs *this* interpreter on a Python script, and every test that depends on
the hook asserts it actually ran — a fixture that cannot spawn would otherwise
make these tests pass while testing nothing.
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

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_641", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)

# The three phrases the predicate keyed on. Fixing only the one the issue
# named would leave the same defect standing behind the other two.
MARKERS = [
    "fetch first",
    "non-fast-forward",
    "tip of your current branch is behind",
]

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


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
                          capture_output=True, text=True, timeout=60)


def _commit(cwd: str, fname: str, msg: str) -> None:
    Path(cwd, fname).write_text(msg, encoding="utf-8")
    assert _run(["add", fname], cwd).returncode == 0
    assert _run(["commit", "-m", msg], cwd).returncode == 0


class _Sandbox:
    """Bare remote + `mine` (the repo under test) + `mate` (a collaborator)."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st641_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.mine = os.path.join(self.tmp, "mine")
        self.mate = os.path.join(self.tmp, "mate")
        self.sentinel = os.path.join(self.tmp, "hook_ran")

        assert _run(["init", "--bare", "remote.git"], self.tmp).returncode == 0
        assert _run(["clone", self.remote, "mine"], self.tmp).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        _commit(self.mine, "a.txt", "base on remote")
        assert _run(["push", "-u", "origin", "feature"], self.mine).returncode == 0
        _commit(self.mine, "b.txt", "my local work")

    def advance_remote(self) -> None:
        """A teammate pushes — the remote is genuinely ahead of `mine`."""
        assert _run(["clone", self.remote, "mate"], self.tmp).returncode == 0
        assert _run(["checkout", "feature"], self.mate).returncode == 0
        _commit(self.mate, "r.txt", "teammate commit")
        assert _run(["push", "origin", "feature"], self.mate).returncode == 0

    def install_hook(self, says: str, exit_code: int = 1) -> None:
        """A pre-push hook printing `says`, exiting `exit_code`.

        Cross-platform by construction: the hook file is the `#!/bin/sh` shim
        git itself knows how to run on every platform it ships for, and the
        only thing it does is exec the *running* interpreter (an absolute
        path, never the bare name `python3`) on a Python file. `echo` is a
        cmd.exe builtin and a bare `python3` can resolve to the Windows App
        Execution Alias, so neither is used.
        """
        script = os.path.join(self.tmp, "hook.py")
        Path(script).write_text(
            "import sys" + chr(10) +
            "open(%r, 'a').write('ran' + chr(10))" % self.sentinel + chr(10) +
            "sys.stderr.write(%r + chr(10))" % says + chr(10) +
            "sys.exit(%d)" % exit_code + chr(10),
            encoding="utf-8")
        hook = Path(self.mine, ".git", "hooks", "pre-push")
        hook.write_text(
            "#!/bin/sh" + chr(10) +
            'exec "%s" "%s"%s' % (Path(sys.executable).as_posix(),
                                  Path(script).as_posix(), chr(10)),
            encoding="utf-8")
        hook.chmod(0o755)

    @property
    def hook_ran(self) -> bool:
        return os.path.exists(self.sentinel)

    def head(self) -> str:
        return _run(["rev-parse", "HEAD"], self.mine).stdout.strip()

    def fetched(self) -> bool:
        """Did anything in this run perform a fetch? FETCH_HEAD is the trace.

        `git clone` does not write it and `ls-remote` does not write it, so its
        appearance means `_recover_by_rebase` ran its `git fetch`.
        """
        return Path(self.mine, ".git", "FETCH_HEAD").exists()

    def remote_tip_subject(self) -> str:
        return _run(["log", "-1", "--format=%s", "feature"], self.remote).stdout.strip()

    def subjects(self) -> list[str]:
        r = _run(["log", "--format=%s"], self.mine)
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

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
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture
def box():
    s = _Sandbox()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# the deliverable: hook text cannot reach the predicate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("marker", MARKERS)
def test_hook_words_never_trigger_a_fetch_or_a_rebase(box, marker: str) -> None:
    """A hook blocks the push and prints one of git's own divergence phrases.

    The remote has NOT moved. There is nothing to rebase onto and nothing to
    fetch, so any fetch/rebase here is unrequested by construction.
    """
    box.install_hook(f"policy: your branch is behind master; {marker} before pushing")
    before = box.head()

    rc, out = box.drive_push()

    assert box.hook_ran, "the hook never spawned — this test would prove nothing"
    assert not box.fetched(), f"unrequested `git fetch` on hook text:{chr(10)}{out}"
    assert "fetching to rebase" not in out, (
        f"hook text drove the op into _recover_by_rebase:{chr(10)}{out}")
    assert box.head() == before, "local history was rewritten by an unrequested rebase"
    assert rc != 0


@pytest.mark.parametrize("marker", MARKERS)
def test_hook_words_never_rewrite_history_when_the_remote_is_ahead(box, marker: str) -> None:
    """The destructive shape: the remote really is ahead AND a hook blocks.

    The push never reached the remote, so this is not a rejection and a rebase
    is not the recovery — but the rebase is what makes the damage visible: the
    local commit is replayed onto the remote tip and comes back a new SHA.
    """
    box.advance_remote()
    box.install_hook(f"policy: {marker}")
    before = box.head()

    rc, out = box.drive_push()

    assert box.hook_ran, "the hook never spawned — this test would prove nothing"
    assert box.head() == before, (
        f"#641: local HEAD was rewritten off hook text alone:{chr(10)}{out}")
    assert not box.fetched(), f"unrequested `git fetch` on hook text:{chr(10)}{out}"
    assert rc != 0


def test_the_hook_message_and_a_verdict_both_survive(box) -> None:
    """Declining to guess must stay loud: the caller gets the hook's own words
    and a `[result]` line naming the state (#623/#638), not a silent rebase."""
    box.install_hook("policy: rebase onto master first, then fetch first")

    rc, out = box.drive_push()

    assert box.hook_ran
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[-1].startswith("[result] "), f"verdict is not the last line:{chr(10)}{out}"
    assert "NOT PUSHED" in lines[-1]
    assert "policy: rebase onto master first" in out, (
        f"the hook's own message was swallowed:{chr(10)}{out}")
    assert rc != 0


# ---------------------------------------------------------------------------
# the real case must still work
# ---------------------------------------------------------------------------

def test_a_genuine_non_fast_forward_still_rebases_and_pushes(box) -> None:
    """No hook, remote genuinely ahead: the recovery this op exists for."""
    box.advance_remote()

    rc, out = box.drive_push()

    assert rc == 0, out
    assert "fetching to rebase" in out, f"the real non-ff was not recovered:{chr(10)}{out}"
    assert "Rebase clean" in out
    assert box.subjects()[:2] == ["my local work", "teammate commit"]
    assert box.remote_tip_subject() == "my local work"
    assert out.splitlines()[-1].startswith("[result] PUSHED"), out


def test_a_hook_that_prints_the_words_and_lets_the_push_through_is_inert(box) -> None:
    """Exit 0 + `fetch first` on stderr: an ordinary successful push."""
    box.install_hook("advice: fetch first if CI is red", exit_code=0)

    rc, out = box.drive_push()

    assert box.hook_ran
    assert rc == 0, out
    assert not box.fetched()
    assert box.remote_tip_subject() == "my local work"


# ---------------------------------------------------------------------------
# the predicate itself, at its new signature
# ---------------------------------------------------------------------------

_PORCELAIN_REJECTED = (
    "To /tmp/remote.git" + chr(10) +
    "!\trefs/heads/feature:refs/heads/feature\t[rejected] (fetch first)" + chr(10) +
    "Done" + chr(10)
)


def test_predicate_reads_gits_porcelain_ref_status() -> None:
    assert push._is_non_fast_forward(_PORCELAIN_REJECTED, "feature") is True


def test_predicate_ignores_a_status_line_for_a_different_ref() -> None:
    other = _PORCELAIN_REJECTED.replace("feature", "someone-else")
    assert push._is_non_fast_forward(other, "feature") is False


def test_predicate_ignores_prose_however_it_is_phrased() -> None:
    """Hook text, and git's own human-readable rendering, are both prose."""
    for prose in (
        "policy: your branch is behind master, fetch first",
        " ! [rejected]        feature -> feature (non-fast-forward)",
        "hint: tip of your current branch is behind its remote counterpart",
        "",
    ):
        assert push._is_non_fast_forward(prose, "feature") is False, prose


def test_predicate_ignores_a_remote_side_hook_rejection() -> None:
    """`[remote rejected]` is a server-side rule, not a divergence — a rebase
    does not help, and the existing hint says so."""
    line = ("!\trefs/heads/feature:refs/heads/feature\t"
            "[remote rejected] (pre-receive hook declined)" + chr(10))
    assert push._is_non_fast_forward(line, "feature") is False
