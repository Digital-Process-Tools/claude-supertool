"""git-push resolves the push remote instead of assuming `origin` (#656).

Everything here is a real repository. The scenario is not reproducible by
stubbing, because the thing under test is *which name git is handed* — a fake
`subprocess.run` would have to encode the answer to get the question right.
So each box is a real bare remote (or two, or none) plus a real clone whose
remote is named whatever the case needs, on a real branch with no upstream.

The only stubbed boundary is `_mr_lookup` — a network call to glab/gh, API
metadata rather than a git fact, and a sandbox has no MR.

The refusal cases assert the remote refs afterwards, not only the exit code.
A refusal that quietly becomes a guess later would still exit non-zero on some
other ground; what must not happen is a branch appearing on a remote nobody
named.
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
_spec = importlib.util.spec_from_file_location("git_push_remote_656", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)

NL = chr(10)

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
                          capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")


def _commit(cwd: str, fname: str, body: str) -> None:
    Path(cwd, fname).write_text(body, encoding="utf-8")
    assert _run(["add", fname], cwd).returncode == 0
    assert _run(["commit", "-m", body.strip()], cwd).returncode == 0


class _Box:
    """A clone with `remote_names` remotes, on `feature`, with NO upstream.

    The remotes are all real bare repositories, so a push that reaches one is
    observable (`has_branch`) and a push that reaches none is too.
    """

    def __init__(self, remote_names: tuple[str, ...]) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st_remote656_")
        self.mine = os.path.join(self.tmp, "mine")
        self.bare: dict[str, str] = {}

        assert _run(["init", "-b", "master", "mine"], self.tmp).returncode == 0
        _commit(self.mine, "a.txt", "base" + NL)
        for name in remote_names:
            path = os.path.join(self.tmp, f"{name}.git")
            assert _run(["init", "--bare", "-b", "master", f"{name}.git"],
                        self.tmp).returncode == 0
            self.bare[name] = path
            assert _run(["remote", "add", name, Path(path).as_posix()],
                        self.mine).returncode == 0
            # HEAD:master only — never `-u`, so no branch gets an upstream and
            # the op has to resolve the remote for itself.
            assert _run(["push", name, "HEAD:master"], self.mine).returncode == 0

        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        _commit(self.mine, "f.txt", "feature work" + NL)

    def config(self, key: str, value: str) -> None:
        assert _run(["config", key, value], self.mine).returncode == 0

    def has_branch(self, remote: str, branch: str = "feature") -> bool:
        r = _run(["ls-remote", "--heads", self.bare[remote], branch], self.mine)
        return bool(r.stdout.strip())

    def upstream(self, branch: str = "feature") -> str:
        r = _run(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                  f"{branch}@{{upstream}}"], self.mine)
        return r.stdout.strip() if r.returncode == 0 else ""

    def drive_push(self, *argv: str) -> tuple[int, str]:
        prev_cwd = os.getcwd()
        prev_argv = sys.argv[:]
        prev_env = {k: os.environ.get(k) for k in _HERMETIC_ENV}
        os.chdir(self.mine)
        os.environ.update({k: v for k, v in _HERMETIC_ENV.items()
                           if v is not None})
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


def _box(*remote_names: str):
    b = _Box(remote_names)
    try:
        yield b
    finally:
        b.close()


@pytest.fixture
def gitlab_box():
    """One remote, named `gitlab`. The exact repo #656 was filed against."""
    yield from _box("gitlab")


@pytest.fixture
def fork_box():
    """`origin` (my fork) + `upstream` (the canonical, plausibly public one)."""
    yield from _box("origin", "upstream")


@pytest.fixture
def ambiguous_box():
    """Two remotes, neither named `origin`. Nothing here is a correct guess."""
    yield from _box("gitlab", "backup")


@pytest.fixture
def remoteless_box():
    """No remotes at all."""
    yield from _box()


def _no_mr():
    return mock.patch.object(push, "_mr_lookup", return_value=push.MrLookup(None))


def _last_result(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "no output at all"
    assert lines[-1].startswith("[result] "), (
        "the verdict must be the LAST line (#638/#623):" + NL + out)
    return lines[-1]


# ===========================================================================
# The push that could not happen: the only remote is not called `origin`
# ===========================================================================

def test_first_push_goes_to_the_only_remote_even_when_not_named_origin(
        gitlab_box) -> None:
    """`git clone -o gitlab` + a new branch = the whole bug.

    There is no upstream to read the remote from and exactly one remote it
    could be, so `-u origin HEAD` names a remote that does not exist and the
    push fails on the one call where `-u` is the point.
    """
    with _no_mr():
        rc, out = gitlab_box.drive_push()

    assert rc == 0, out
    assert gitlab_box.has_branch("gitlab"), (
        "the branch never reached the only remote there was:" + NL + out)
    assert gitlab_box.upstream() == "gitlab/feature", (
        "`-u` did not set the upstream to the remote actually pushed to")
    assert _last_result(out).startswith("[result] PUSHED")


def test_the_header_names_the_resolved_remote_not_origin(gitlab_box) -> None:
    """The receipt must not say `(origin)` about a repo that has no origin.

    The `Upstream: none` line is read before the push happens; naming a remote
    nobody confirmed is the disclosure half of #642/#675 in this op's other
    branch.
    """
    with _no_mr():
        _rc, out = gitlab_box.drive_push()

    head = out.split("Flags:")[0]
    assert "gitlab" in head, out
    assert "(origin)" not in head, (
        "the pre-push header still announces a hardcoded origin:" + NL + out)


# ===========================================================================
# Two or more remotes: prefer origin, and refuse when there is no origin
# ===========================================================================

def test_fork_layout_prefers_origin_over_the_canonical_remote(
        fork_box) -> None:
    """origin=my fork, upstream=canonical. Pushing to `upstream` is the harm.

    This is also what plain `git push` does, so the op is not more surprising
    than the command it wraps on the most common multi-remote layout.
    """
    with _no_mr():
        rc, out = fork_box.drive_push()

    assert rc == 0, out
    assert fork_box.has_branch("origin"), out
    assert not fork_box.has_branch("upstream"), (
        "the branch was created on the canonical remote:" + NL + out)


def test_two_remotes_without_origin_refuse_and_name_the_candidates(
        ambiguous_box) -> None:
    """No correct guess exists, so the op declines (docs/validators.md).

    Guessing wrong on a first push creates a branch on a remote the caller
    never named — plausibly a public one — which is not a mistake an error
    message is worse than.
    """
    with _no_mr():
        rc, out = ambiguous_box.drive_push()

    assert rc != 0, out
    assert not ambiguous_box.has_branch("gitlab"), (
        "the op guessed, and it pushed:" + NL + out)
    assert not ambiguous_box.has_branch("backup"), (
        "the op guessed, and it pushed:" + NL + out)
    verdict = _last_result(out)
    assert "NOT PUSHED" in verdict, verdict
    # Both candidates named: a refusal the caller cannot act on is half a fix.
    assert "gitlab" in out and "backup" in out, out
    assert "git push -u" in out, (
        "the refusal must name the one-line way out:" + NL + out)


def test_no_remotes_at_all_refuses_rather_than_inventing_origin(
        remoteless_box) -> None:
    with _no_mr():
        rc, out = remoteless_box.drive_push()

    assert rc != 0, out
    assert "NOT PUSHED" in _last_result(out)
    assert "no remote" in out.lower(), out


# ===========================================================================
# Configuration outranks every guess — git's own precedence order
# ===========================================================================

@pytest.mark.parametrize("key", ["branch.feature.remote",
                                 "branch.feature.pushRemote",
                                 "remote.pushDefault"])
def test_configured_remote_settles_an_otherwise_ambiguous_repo(
        ambiguous_box, key: str) -> None:
    """The user who works against a non-origin remote has already said so.

    These are the three keys `git push` itself consults before falling back,
    and reading them means the op pushes where plain `git push` would.
    """
    ambiguous_box.config(key, "backup")

    with _no_mr():
        rc, out = ambiguous_box.drive_push()

    assert rc == 0, out
    assert ambiguous_box.has_branch("backup"), out
    assert not ambiguous_box.has_branch("gitlab"), out


def test_push_remote_outranks_branch_remote(ambiguous_box) -> None:
    """git's precedence: pushRemote > pushDefault > branch.<name>.remote.

    A repo that fetches from one place and pushes to another is exactly what
    these keys exist for, and reversing them sends the push to the fetch-only
    remote.
    """
    ambiguous_box.config("branch.feature.remote", "gitlab")
    ambiguous_box.config("branch.feature.pushRemote", "backup")

    with _no_mr():
        rc, out = ambiguous_box.drive_push()

    assert rc == 0, out
    assert ambiguous_box.has_branch("backup"), out
    assert not ambiguous_box.has_branch("gitlab"), out


def test_configured_remote_wins_over_an_existing_origin(fork_box) -> None:
    """`origin` exists, and is not what this branch was told to push to."""
    fork_box.config("branch.feature.remote", "upstream")

    with _no_mr():
        rc, out = fork_box.drive_push()

    assert rc == 0, out
    assert fork_box.has_branch("upstream"), out
    assert not fork_box.has_branch("origin"), out


# ===========================================================================
# The resolver's own three states
# ===========================================================================

def test_resolver_declines_when_git_remote_cannot_be_asked(
        gitlab_box, monkeypatch) -> None:
    """A `git remote` that never completed is not a repo without remotes.

    Stubbed at the one call that matters — `_remote_names` — rather than over
    `subprocess.run`, so every other git call in the op stays real (#731).
    """
    monkeypatch.setattr(push, "_remote_names",
                        lambda: ([], "`git remote` did not complete — boom"))

    with _no_mr():
        rc, out = gitlab_box.drive_push()

    assert rc != 0, out
    assert not gitlab_box.has_branch("gitlab"), (
        "pushed on an answer git never gave:" + NL + out)
    assert "boom" in out, out
    assert "NOT PUSHED" in _last_result(out)


def test_an_existing_upstream_is_still_what_wins(gitlab_box) -> None:
    """The resolver is the no-upstream path only — it must not retarget.

    A second push on a branch that already tracks `gitlab/feature` goes back
    to the same ref without consulting any of the ladder.
    """
    with _no_mr():
        assert gitlab_box.drive_push()[0] == 0
    _commit(gitlab_box.mine, "g.txt", "more" + NL)

    with _no_mr():
        rc, out = gitlab_box.drive_push()

    assert rc == 0, out
    assert gitlab_box.upstream() == "gitlab/feature"
    assert "gitlab/feature" in out, out
