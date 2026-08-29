"""The branch→MR lookup says which of three things happened (#948).

`query_open_mr` returns `None` for "there is no open MR/PR for this branch"
and for "the lookup never happened", and swallows every exception on the way.
`git-push` calls it three times, immediately before and after the one
irreversible action supertool takes, and every consumer of that `None` reads it
as the first meaning:

* `_open_mr_line` renders nothing — the receipt simply has no MR line;
* `_post_push_advisories` skips the mergeability warning and the stale-base
  check, because it has no target branch to check against;
* `_watch_advisory`, on `:watch`, prints **"there is no open MR/PR for this
  branch yet — nothing to watch. Open one"**, which is a positive claim about
  the world, made out of a lookup that did not answer.

The shape is the one `PrIndex` was built for in #941 one function over, and the
one `presets/git/status.py::_hosted_request` already applies to the same two
CLIs: a result that carries either a fact or the reason there is no fact.

Two things this must NOT do, both stated in the issue:

* it must not block the push. A GitHub that cannot be reached is not a reason
  to refuse to publish work; the receipt degrades to a stated unknown.
* it must not add a line to the healthy path. A lookup that answered prints
  exactly what it printed before, byte for byte.

And one the issue got wrong, checked rather than assumed: `git-status` is not a
consumer. It has its own three-state `_hosted_request` (#705) and never calls
this function. The consumers are `git-push` and `git-commit`'s post-commit hint.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest


_COMMON =Path(__file__).parent.parent / "presets" / "git" / "_git_common.py"
_cspec = importlib.util.spec_from_file_location("_git_common", _COMMON)
assert _cspec is not None and _cspec.loader is not None
common = importlib.util.module_from_spec(_cspec)
sys.modules["_git_common"] = common
_cspec.loader.exec_module(common)

PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_948", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _only(tool: str):
    """`shutil.which` double: exactly one of glab/gh is on PATH."""
    return lambda name: f"/usr/bin/{name}" if name == tool else None


_PR_JSON = '[{"number": 7, "baseRefName": "main", "mergeable": "MERGEABLE"}]'


_HOSTED_REMOTE = ("origin" + chr(9) + "https://github.com/acme/x.git (fetch)" +
                  chr(10) + "origin" + chr(9) +
                  "https://github.com/acme/x.git (push)" + chr(10))


class _GitRemoteProc:
    """A `Popen` double standing in for `_git`'s own "remote -v" call (#2033
    moved `_git` off `subprocess.run` onto `Popen`)."""

    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self._stdout = stdout

    def communicate(self, timeout=None):
        return self._stdout, ""


def _git_remote_popen(cmd, **_kw):
    # `_git` prepends `--no-optional-locks` ahead of the subcommand (#1945),
    # so the discriminator can no longer assume the subcommand sits at
    # cmd[1] -- it looks for "remote" anywhere in a `git` argv.
    assert cmd and cmd[0] == "git" and "remote" in cmd, cmd
    return _GitRemoteProc(_HOSTED_REMOTE)


def _runs(cli):
    """`subprocess.run` double: the CLI's own answer.

    Since #948 the lookup asks git which remotes exist before it spends a CLI
    call — a repo with no forge remote has no tracker, and that is knowable
    without one. `_git` reaches git through `subprocess.Popen` (#2033), a
    separate seam patched by `_git_remote_popen` above wherever this is used,
    so this double only has to answer for the CLI probe.

    `cli` is a `CompletedProcess`, or a callable taking argv.
    """
    def run(cmd, **kw):
        # `hasattr(..., "returncode")`, not `callable(...)`: a `mock.Mock`
        # stub result is itself callable, and calling it returns another Mock.
        return cli if hasattr(cli, "returncode") else cli(cmd)
    return run


# ---------------------------------------------------------------------------
# The lookup itself — one object, three states
# ---------------------------------------------------------------------------

def test_a_timeout_is_not_an_absence() -> None:
    """The case the issue was filed on: a slow host reported as "no PR"."""
    def timeout(cmd):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=5)

    with mock.patch.object(common.shutil, "which", _only("gh")), \
         mock.patch.object(common.subprocess, "run", _runs(timeout)), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        res = common.query_open_mr_result("feature/x")

    assert res.answered is False, (
        "a lookup that never completed reported the same value as a branch "
        "with no PR")
    assert res.mr is None
    assert "timed out" in res.reason or "did not answer" in res.reason, res.reason


def test_an_expired_token_is_not_an_absence() -> None:
    with mock.patch.object(common.shutil, "which", _only("gh")), \
         mock.patch.object(common.subprocess, "run", _runs(_proc(
             4, stderr="gh: To get started with GitHub CLI, please run: "
                       "gh auth login" + chr(10)))), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        res = common.query_open_mr_result("feature/x")

    assert res.answered is False, res.reason
    assert "auth login" in res.reason, res.reason


def test_output_that_is_not_json_is_not_an_absence() -> None:
    with mock.patch.object(common.shutil, "which", _only("gh")), \
         mock.patch.object(common.subprocess, "run",
                           _runs(_proc(0, stdout="<html>proxy</html>"))), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        res = common.query_open_mr_result("feature/x")

    assert res.answered is False, res.reason


def test_an_empty_list_is_an_answer() -> None:
    """The healthy "no PR yet" case has to stay a fact, or the fix is noise."""
    with mock.patch.object(common.shutil, "which", _only("gh")), \
         mock.patch.object(common.subprocess, "run",
                           _runs(_proc(0, stdout="[]"))), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        res = common.query_open_mr_result("feature/x")

    assert res.answered is True
    assert res.mr is None
    assert res.reason == ""


def test_the_other_hosts_cli_declining_is_an_answer_not_a_failure() -> None:
    """`gh` in a GitLab repo says so, and it will say so on every future run.

    Structural and permanent — the same reading `status.py::_ANSWERED_NONE`
    applies. Treating it as "could not answer" would put a warning on every
    push of every repo that is not on GitHub, which is how a warning stops
    being read.
    """
    with mock.patch.object(common.shutil, "which", _only("gh")), \
         mock.patch.object(common.subprocess, "run", _runs(_proc(
             1, stderr="none of the git remotes configured for this "
                       "repository point to a known GitHub host" + chr(10)))):
        res = common.query_open_mr_result("feature/x")

    assert res.answered is True, res.reason
    assert res.mr is None


def test_glab_answering_none_is_not_undone_by_gh_failing_after_it() -> None:
    """A GitLab repo: glab says "no MR", then `gh` fails because it is not GitHub.

    The fallback exists for repos on the other host, and it must not be able to
    downgrade an answer that was already given. Getting this wrong would put a
    decline on every push of every GitLab repo with `gh` installed — the loud
    bug traded for the quiet one, in the direction that makes the tool useless.
    """
    def cli(cmd):
        if cmd[0] == "glab":
            return _proc(0, stdout="[]")
        return _proc(4, stderr="gh: could not determine base repository" +
                                chr(10))

    with mock.patch.object(common.shutil, "which", lambda n: f"/usr/bin/{n}"), \
         mock.patch.object(common.subprocess, "run", _runs(cli)), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        res = common.query_open_mr_result("feature/x")

    assert res.answered is True, res.reason
    assert res.mr is None


def test_no_cli_at_all_is_not_an_absence_either() -> None:
    with mock.patch.object(common.shutil, "which", return_value=None):
        res = common.query_open_mr_result("feature/x")

    assert res.answered is False
    assert "glab" in res.reason and "gh" in res.reason, res.reason


def test_a_found_pr_is_answered() -> None:
    with mock.patch.object(common.shutil, "which", _only("gh")), \
         mock.patch.object(common.subprocess, "run",
                           _runs(_proc(0, stdout=_PR_JSON))), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        res = common.query_open_mr_result("feature/x")

    assert res.answered is True
    assert res.mr is not None and res.mr["iid"] == 7
    assert res.reason == ""


def test_the_thin_wrapper_still_returns_the_dict() -> None:
    """`git-commit`'s post-commit hint keeps its old signature."""
    with mock.patch.object(common.shutil, "which", _only("gh")), \
         mock.patch.object(common.subprocess, "run",
                           _runs(_proc(0, stdout=_PR_JSON))), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        assert common.query_open_mr("feature/x") == {
            "source": "github", "iid": 7, "target": "main", "pipeline": None,
            "pipeline_id": None, "pipeline_url": None, "merge_status": None}
    assert common.query_open_mr("") is None


def test_the_glab_call_uses_flags_this_glab_actually_has() -> None:
    """The bug the disclosure uncovered on its first run — same line, same call.

    `glab mr list --state opened` is rejected outright by glab 1.86:

        $ glab mr list --source-branch x --state opened --output json
           ERROR
          Unknown flag: --state.
        rc=1

    So the GitLab arm has been failing at argument parsing, and the old code
    swallowed the non-zero exit and fell through to `gh` — which, on a GitLab
    repo, cannot answer either. `git-push` and `git-commit` have therefore
    never found an MR through glab on this version, and said nothing about it.
    Open is glab's default for `mr list` (`--closed` is the opt-out), so the
    flag is not merely wrong, it is unnecessary.

    Pinned as argv rather than by running glab, because the point is what this
    module sends, and a machine without glab must still fail this test if the
    flag comes back.
    """
    seen = []

    def cli(cmd):
        seen.append(cmd)
        return _proc(0, stdout="[]")

    with mock.patch.object(common.shutil, "which", _only("glab")), \
         mock.patch.object(common.subprocess, "run", _runs(cli)), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        common.query_open_mr_result("feature/x")

    assert len(seen) == 1, seen
    argv = seen[0]
    assert "--state" not in argv, (
        "glab 1.86 exits 1 with `Unknown flag: --state.` — the GitLab arm "
        f"cannot answer at all: {argv}")
    assert argv[:3] == ["glab", "mr", "list"], argv
    assert "--source-branch" in argv and "feature/x" in argv, argv
    assert "--output" in argv and "json" in argv, argv


# ---------------------------------------------------------------------------
# The render — where the collapse was actually paid for
# ---------------------------------------------------------------------------

def test_the_receipt_names_the_unknown() -> None:
    line = push._mr_unknown_line(
        common.MrLookup(None, "gh pr list timed out after 5s"))
    assert "UNKNOWN" in line, line
    assert "timed out after 5s" in line, line


def test_an_answered_lookup_adds_nothing_to_the_receipt() -> None:
    """The constraint that makes the disclosure readable when it appears."""
    assert push._mr_unknown_line(common.MrLookup(None)) == ""
    assert push._mr_unknown_line(common.MrLookup(
        {"source": "github", "iid": 7, "target": "main"})) == ""


def test_watch_does_not_claim_there_is_no_pr_when_it_does_not_know(capsys) -> None:
    """The sharpest instance: `:watch` told the user to open a PR that exists."""
    push._watch_advisory(common.MrLookup(None, "gh pr list timed out after 5s"),
                         {"watch"})
    out = capsys.readouterr().out

    assert "no open MR/PR for this branch yet" not in out, (
        "the lookup failed and the tool told the reader the branch has no "
        "MR/PR:\n" + out)
    assert "UNKNOWN" in out, out
    assert "timed out after 5s" in out, out


def test_watch_still_says_open_one_when_it_really_knows(capsys) -> None:
    push._watch_advisory(common.MrLookup(None), {"watch"})
    out = capsys.readouterr().out
    assert "no open MR/PR for this branch yet" in out, out


def test_advisories_disclose_the_unknown_and_do_not_raise(capsys) -> None:
    """A push is never blocked by a lookup that did not answer."""
    with mock.patch.object(push, "_uncommitted_leftovers", return_value=(0, "")):
        push._post_push_advisories(
            common.MrLookup(None, "gh could not be run (ENOENT)"),
            set(), "origin")
    out = capsys.readouterr().out
    assert "UNKNOWN" in out, out


# ---------------------------------------------------------------------------
# A repo with no forge remote at all — the fact is local, and a CLI that dies
# before it reads the remotes must not cost us an answer we already have
# ---------------------------------------------------------------------------

_GH_IN_ACTIONS = (
    "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN "
    "environment variable. Example:" + chr(10) +
    "  env:" + chr(10) +
    "    GH_TOKEN: ${{ github.token }}" + chr(10))


def _remote_v(url: str) -> str:
    """What `git remote -v` prints for a repo with one remote named origin."""
    return ("origin" + chr(9) + url + " (fetch)" + chr(10) +
            "origin" + chr(9) + url + " (push)" + chr(10))


def _lookup_against(url: str, cli):
    """The lookup, in a repo whose one remote is `url`, with `cli` for the CLI.

    Parametrised over the *shape* of the remote rather than driven through a
    real repo, because the shape is the whole subject: a Windows checkout puts
    its sandbox at `C:/Users/.../remote.git`, and the drive letter is why this
    is a test rather than a comment. Building a real repo would have pinned
    only the path shape of whichever machine ran it — which is exactly how the
    first version of this test passed on Linux and macOS and failed on Windows.
    """
    with mock.patch.object(common.shutil, "which", _only("gh")), \
         mock.patch.object(common.subprocess, "run", _runs_in_repo(cli)), \
         mock.patch.object(common.subprocess, "Popen",
                           _git_remote_popen_for(url)):
        return common.query_open_mr_result("feature")


def _runs_in_repo(cli):
    """`_git` no longer reaches `subprocess.run` (#2033) -- the "remote -v"
    branch moved to `_git_remote_popen_for` below, so this double only ever
    has to answer for the CLI."""
    def run(cmd, *_a, **_k):
        return cli if hasattr(cli, "returncode") else cli(cmd)
    return run


def _git_remote_popen_for(url: str):
    """`_git_remote_popen`, parametrised over the remote's own URL shape."""
    def popen(cmd, **_kw):
        assert cmd and cmd[0] == "git" and "remote" in cmd, cmd
        return _GitRemoteProc(_remote_v(url))
    return popen


@pytest.mark.parametrize("url", [
    "/tmp/st675_a6npf0jw/remote.git",
    "file:///tmp/st675_a6npf0jw/remote.git",
    "../sibling.git",
    "./remote.git",
    # The two the first version of this fix got wrong. A Windows sandbox repo
    # is at `C:/Users/runneradmin/AppData/Local/Temp/st675_.../remote.git`, and
    # a naive scp-style test — "a colon before the first slash means
    # `host:path`" — reads the drive letter as a hostname and calls it a forge.
    "C:/Users/runneradmin/AppData/Local/Temp/st675_l2gyyhvv/remote.git",
    "C:" + chr(92) + "Users" + chr(92) + "runneradmin" + chr(92) + "remote.git",
])
def test_a_repo_whose_only_remote_is_a_local_path_has_no_request_to_find(url) -> None:
    """No forge is configured, so there is no tracker a request could live on.

    That is a fact about the repository, readable from `git remote` without
    asking anyone — and it must stay readable when the CLI cannot answer. `gh`
    checks its own credentials *before* it looks at the remotes, so on a runner
    with `gh` installed and no token it exits 4 with a message about GH_TOKEN
    and never reaches the "none of the git remotes ... point to a known GitHub
    host" line that `NOT_THIS_HOST_PHRASES` was built to catch. The absence was
    then reported as an unknown, on a push where every other check ran, which
    is the #948 disclosure firing on the one case it was promised never to fire
    on (the healthy path is byte-identical).
    """
    res = _lookup_against(url, _proc(4, stderr=_GH_IN_ACTIONS))

    assert res.answered is True, (
        f"no remote of this repo points at a forge ({url}), so there is no "
        "tracker on which an MR/PR could be open — that is an answer, and it "
        f"was reported as an unknown: {res.reason}")
    assert res.mr is None


@pytest.mark.parametrize("url", [
    "https://github.com/Digital-Process-Tools/x.git",
    "ssh://git@github.com/Digital-Process-Tools/x.git",
    "git@github.com:Digital-Process-Tools/x.git",
    "git://gitlab.com/acme/x.git",
])
def test_a_forge_remote_whose_cli_cannot_answer_is_still_unknown(url) -> None:
    """The guard against fixing the loud bug by inventing a quiet one.

    Same unauthenticated `gh`, but the remote really is on a forge. Nothing
    local settles whether a PR is open, so the only truthful answer is that we
    do not know. If this ever goes green by way of the check above, that check
    has been widened past what it can actually establish — and widening it is
    the tempting way to make a Windows path stop being read as `host:path`.
    """
    res = _lookup_against(url, _proc(4, stderr=_GH_IN_ACTIONS))

    assert res.answered is False, (
        f"the repo is on a forge ({url}) and the only CLI that could have said "
        "whether a PR is open exited without answering — that is not an "
        "absence")
    assert "${{ github.token }}" not in res.reason, (
        "the reason handed to the reader is the last line of gh's message, a "
        "YAML fragment that names neither the tool's problem nor theirs: "
        f"{res.reason}")
    assert "authenticat" in res.reason.lower(), res.reason


def test_a_git_that_cannot_be_spawned_is_an_unknown_not_a_traceback() -> None:
    """A machine with no `git` on PATH must degrade, not crash (#948).

    The lookup asks `git remote -v` before it spends a CLI call, and `_git`
    deliberately lets an `OSError` escape — `presets/git/push.py` guards each of
    its own calls and pins that in #675. This call site did not, so on a PATH
    with no git the branch→MR lookup raised `FileNotFoundError` out of a
    receipt for a push that had already succeeded. That is #399/#640 again:
    losing the receipt is losing the answer.

    Not mocked. PATH is scrubbed to an empty directory, so `subprocess.run`
    really fails to start a process — the same interpreter-level failure #675
    uses, and the one shape that behaves identically on Windows and POSIX.
    """
    empty = tempfile.mkdtemp(prefix="st948_nopath_")
    prev = os.environ.get("PATH")
    try:
        os.environ["PATH"] = empty
        res = common.query_open_mr_result("feature")
    finally:
        if prev is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = prev
        shutil.rmtree(empty, ignore_errors=True)

    assert res.answered is False, (
        "git could not be started, so nothing was established about this "
        "branch — reporting that as an answer would be the #948 collapse "
        "rebuilt one layer up")
    assert res.mr is None
    assert "git" in res.reason.lower(), res.reason
