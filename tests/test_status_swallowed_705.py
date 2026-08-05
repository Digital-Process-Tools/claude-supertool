"""#705 — two more places where a call that did not happen renders as an answer.

Both are the house defect (docs/validators.md, "Declining instead of guessing"):
three states, not two — answered, answered-with-a-finding, and *could not
answer*. #685 rewrote `presets/git/status.py` to carry an `INCOMPLETE_MARKER`
footer for exactly this, and two call sites never reached it.

  * The `glab`/`gh` lookup runs `subprocess.run` directly and swallows every
    failure into `pass`. A stalled network, an expired token and an
    unauthenticated CLI all produce the same output as the common, unremarkable
    case of the branch simply having no MR yet: no section, no footer.
  * `supertool._path_meta_suffix` drops the `m`/`?`/`!` marker when its
    `git status` fails. The marker's whole job is to say the file on disk
    differs from the index, so omitting it on failure inverts the reading —
    on every `read`, the most-used op in the tool.

Nothing here mocks the lookup: the failures are driven through PATH shims that
are real executables behaving the way a real `glab`, `gh` or `git` behaves when
it stalls or refuses (#649).
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).parent.parent
_STATUS_PATH = _ROOT / "presets" / "git" / "status.py"
_spec = importlib.util.spec_from_file_location("git_status_705", _STATUS_PATH)
assert _spec is not None and _spec.loader is not None
status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX /bin/sh shim")


# ---------------------------------------------------------------------------
# Shims: real executables, real exit codes, real stalls
# ---------------------------------------------------------------------------

def _bindir(tmp_path: Path, name: str = "shimbin") -> Path:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return d


def _real(tool: str) -> str:
    found = shutil.which(tool)
    assert found, f"{tool} must be on PATH for this suite"
    return found


def _write_shim(d: Path, name: str, body: str) -> None:
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


def _real_git_shim(d: Path) -> None:
    _write_shim(d, "git", f'exec {_real("git")} "$@"\n')


def _failing_git_shim(d: Path, subcommand: str, code: int, message: str) -> None:
    """A git that refuses one subcommand the way a locked index refuses it."""
    _write_shim(
        d, "git",
        f'if [ "$1" = "{subcommand}" ]; then echo "{message}" >&2; exit {code}; fi\n'
        f'exec {_real("git")} "$@"\n',
    )


def _stalling_git_shim(d: Path, subcommand: str) -> None:
    _write_shim(
        d, "git",
        f'if [ "$1" = "{subcommand}" ]; then {_real("sleep")} 300; fi\n'
        f'exec {_real("git")} "$@"\n',
    )


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("original\n")
    (repo / "clean.txt").write_text("untouched\n")
    _git(repo, "add", "tracked.txt", "clean.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _run_status(repo: Path, monkeypatch) -> str:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(status.sys, "argv", ["status.py"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert status.main() == 0
    return buf.getvalue()


# ===========================================================================
# 1. `_path_meta_suffix` — a marker that is absent for two different reasons
# ===========================================================================

def test_a_modified_file_does_not_render_as_clean_when_git_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """The defect, stated as the reading it produces.

    The comparison is against the rendering of a genuinely clean file, not
    against a message: whatever the failure renders as, it must not be a thing
    a reader can reach "this file matches the index" from.
    """
    repo = _repo(tmp_path)
    modified = repo / "tracked.txt"
    modified.write_text("changed\n")

    clean_reading = supertool._path_meta_suffix(str(repo / "clean.txt"), b"untouched\n")
    assert clean_reading == "", "a clean tracked file carries no marker"

    d = _bindir(tmp_path)
    _failing_git_shim(d, "status", 128, "fatal: unable to read index file")
    monkeypatch.setenv("PATH", str(d))
    broken_reading = supertool._path_meta_suffix(str(modified), b"changed\n")

    assert broken_reading != clean_reading, (
        "a git that could not answer rendered identically to a clean file"
    )


def test_a_stalled_git_status_does_not_render_the_file_as_clean(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path)
    modified = repo / "tracked.txt"
    modified.write_text("changed\n")

    d = _bindir(tmp_path)
    _stalling_git_shim(d, "status")
    monkeypatch.setenv("PATH", str(d))
    out = supertool._path_meta_suffix(str(modified), b"changed\n")

    assert out != "", "a stalled lookup rendered as 'nothing notable about this file'"


def test_the_decline_marker_is_not_one_of_the_states_it_replaces(
    tmp_path: Path, monkeypatch
) -> None:
    """`?`, `!` and `m` are answers. The decline must not be spelled as one.

    A decline rendered as `?` would say "untracked", which is a claim about
    the repository that nobody established — the original defect wearing a
    different character.
    """
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")
    d = _bindir(tmp_path)
    _failing_git_shim(d, "status", 128, "fatal: unable to read index file")
    monkeypatch.setenv("PATH", str(d))

    tokens = supertool._path_meta_suffix(str(repo / "tracked.txt"), b"changed\n").split()

    assert tokens, "no marker at all"
    assert "?" not in tokens, "a decline rendered as 'untracked'"
    assert "!" not in tokens, "a decline rendered as 'ignored'"
    assert "m" not in tokens, "a decline rendered as 'modified'"


def test_the_read_receipt_carries_the_decline_end_to_end(
    tmp_path: Path, monkeypatch
) -> None:
    """The suffix exists to be read on a `read` receipt, so pin it there."""
    repo = _repo(tmp_path)
    target = repo / "tracked.txt"
    target.write_text("changed\n")

    working = supertool.op_read(str(target))

    d = _bindir(tmp_path)
    _failing_git_shim(d, "status", 128, "fatal: unable to read index file")
    monkeypatch.setenv("PATH", str(d))
    broken = supertool.op_read(str(target))

    assert " m" in working.splitlines()[0], working.splitlines()[0]
    assert broken.splitlines()[0] != working.splitlines()[0]
    assert supertool.PATH_META_UNKNOWN in broken.splitlines()[0], broken.splitlines()[0]


def test_a_file_outside_any_repository_carries_no_decline(tmp_path: Path) -> None:
    """The noise guard.

    Most files supertool reads on some machines are not in a repository at
    all. "git status does not apply here" is an answer, not an absence of one,
    and a decline printed on every such read would be the permanent
    disclaimer #621 and #685 both refused.
    """
    loose = tmp_path / "loose.txt"
    loose.write_text("hello\n")
    assert supertool.PATH_META_UNKNOWN not in supertool._path_meta_suffix(
        str(loose), b"hello\n")


def test_a_machine_without_git_carries_no_decline(tmp_path: Path, monkeypatch) -> None:
    """docs/validators.md: a decline that can never resolve is noise on every
    receipt of every op. Nothing on this machine was going to answer."""
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")
    monkeypatch.setenv("PATH", str(_bindir(tmp_path, "emptybin")))

    assert supertool.PATH_META_UNKNOWN not in supertool._path_meta_suffix(
        str(repo / "tracked.txt"), b"changed\n")


def test_a_working_git_still_answers_normally(tmp_path: Path) -> None:
    """The control: a green suite must mean the decline fired on failures only."""
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")
    (repo / "new.txt").write_text("brand new\n")

    assert " m" in supertool._path_meta_suffix(str(repo / "tracked.txt"), b"changed\n")
    assert " ?" in supertool._path_meta_suffix(str(repo / "new.txt"), b"brand new\n")
    assert supertool.PATH_META_UNKNOWN not in supertool._path_meta_suffix(
        str(repo / "clean.txt"), b"untouched\n")


# ===========================================================================
# 2. The MR/PR lookup — a failure that reads as "there is no MR"
# ===========================================================================

def _mr_shims(tmp_path: Path, name: str, *, glab: str, gh: str) -> Path:
    """A fresh directory per shim set — never a rewrite of a live one.

    Overwriting a shell script that has already been exec'd from this process
    makes the next exec of it hang rather than fail, so a test that reuses one
    directory for two shim sets stalls its second run and reports it as the
    behaviour under test. It did, before this argument existed.
    """
    d = _bindir(tmp_path, name)
    _real_git_shim(d)
    _write_shim(d, "glab", glab)
    _write_shim(d, "gh", gh)
    return d


_GLAB_NO_MR = 'echo "no open merge request available for \'master\'" >&2\nexit 1\n'
_GH_NO_PR = 'echo "no pull requests found for branch \\"master\\"" >&2\nexit 1\n'
_GLAB_WRONG_HOST = (
    'echo "None of the git remotes configured for this repository point to a '
    'known GitLab host." >&2\nexit 1\n'
)
_GH_NO_REMOTE = 'echo "no git remotes found" >&2\nexit 1\n'
_GLAB_UNAUTHENTICATED = 'echo "error: 401 Unauthorized" >&2\nexit 1\n'
_GH_UNAUTHENTICATED = (
    'echo "gh: To use GitHub CLI, set the GH_TOKEN environment variable" >&2\nexit 4\n'
)


def test_a_refused_mr_lookup_is_disclosed_in_the_footer(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", str(_mr_shims(
        tmp_path, "refused", glab=_GLAB_UNAUTHENTICATED, gh=_GH_UNAUTHENTICATED)))
    out = _run_status(repo, monkeypatch)

    assert status.INCOMPLETE_MARKER in out, out
    note = next(l for l in out.splitlines() if status.INCOMPLETE_MARKER in l)
    assert "glab" in note, note
    assert "gh" in note, note


def test_a_stalled_mr_lookup_is_disclosed_in_the_footer(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path)
    sleep = _real("sleep")
    monkeypatch.setenv("PATH", str(_mr_shims(
        tmp_path, "stalled", glab=f"{sleep} 300\n", gh=f"{sleep} 300\n")))
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "1")
    out = _run_status(repo, monkeypatch)

    assert status.INCOMPLETE_MARKER in out, out
    assert "glab" in out


def test_a_refused_lookup_does_not_read_like_a_branch_with_no_mr(
    tmp_path: Path, monkeypatch
) -> None:
    """The whole issue in one assertion.

    Both runs print no MR section. Only one of them knows there is no MR, and
    the reports must not be the same document.
    """
    repo = _repo(tmp_path)

    monkeypatch.setenv("PATH", str(_mr_shims(
        tmp_path, "absent", glab=_GLAB_NO_MR, gh=_GH_NO_PR)))
    genuinely_absent = _run_status(repo, monkeypatch)

    monkeypatch.setenv("PATH", str(_mr_shims(
        tmp_path, "unauth", glab=_GLAB_UNAUTHENTICATED, gh=_GH_UNAUTHENTICATED)))
    could_not_tell = _run_status(repo, monkeypatch)

    assert "## MR" not in genuinely_absent and "## PR" not in genuinely_absent
    assert could_not_tell != genuinely_absent, (
        "a lookup that failed produced the same report as one that answered"
    )


def test_a_branch_with_no_mr_carries_no_footer(tmp_path: Path, monkeypatch) -> None:
    """The noise guard: the common case must stay silent, or the footer stops
    meaning anything on the run where it matters."""
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", str(_mr_shims(
        tmp_path, "nomr", glab=_GLAB_NO_MR, gh=_GH_NO_PR)))
    out = _run_status(repo, monkeypatch)

    assert status.INCOMPLETE_MARKER not in out, out


def test_the_other_hosts_cli_carries_no_footer(tmp_path: Path, monkeypatch) -> None:
    """A GitLab repo runs `gh` too, and a GitHub repo runs `glab`. Neither
    "no remote of mine here" is a lookup that failed — it is one that answered."""
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", str(_mr_shims(
        tmp_path, "wronghost", glab=_GLAB_WRONG_HOST, gh=_GH_NO_REMOTE)))
    out = _run_status(repo, monkeypatch)

    assert status.INCOMPLETE_MARKER not in out, out


def test_a_machine_without_the_clis_carries_no_footer(
    tmp_path: Path, monkeypatch
) -> None:
    d = _bindir(tmp_path, "gitonly")
    _real_git_shim(d)
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", str(d))
    out = _run_status(repo, monkeypatch)

    assert status.INCOMPLETE_MARKER not in out, out


def test_unparseable_lookup_output_is_disclosed(tmp_path: Path, monkeypatch) -> None:
    """Exit 0 and a body that is not JSON is not "there is no MR" either."""
    repo = _repo(tmp_path)
    monkeypatch.setenv("PATH", str(_mr_shims(
        tmp_path, "badjson", glab='echo "<html>proxy interstitial</html>"\nexit 0\n',
        gh=_GH_NO_PR)))
    out = _run_status(repo, monkeypatch)

    assert status.INCOMPLETE_MARKER in out, out
    assert "glab" in out


# ===========================================================================
# 3. The sweep — other calls in status.py that reach a render path unfootered
# ===========================================================================

def test_a_working_tree_that_could_not_be_read_is_disclosed(
    tmp_path: Path, monkeypatch
) -> None:
    """`git status` failing (a held index lock is enough) drops the entire
    working-tree section, and a report with no such section reads as a repo
    with nothing to say about it. `_git` records timeouts; a refusal was
    recorded by nobody."""
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")
    d = _bindir(tmp_path, "wtbin")
    _failing_git_shim(d, "status", 128, "fatal: Unable to create index.lock: File exists")
    monkeypatch.setenv("PATH", str(d))
    out = _run_status(repo, monkeypatch)

    assert "## Working tree" not in out
    assert status.INCOMPLETE_MARKER in out, out
    assert "status" in out


def test_a_stash_list_that_could_not_be_read_is_disclosed(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path)
    d = _bindir(tmp_path, "stashbin")
    _failing_git_shim(d, "stash", 128, "fatal: Unable to read the index")
    monkeypatch.setenv("PATH", str(d))
    out = _run_status(repo, monkeypatch)

    assert status.INCOMPLETE_MARKER in out, out
    assert "stash" in out


def test_a_healthy_repository_carries_no_footer(tmp_path: Path, monkeypatch) -> None:
    """The control for the whole file."""
    repo = _repo(tmp_path)
    d = _bindir(tmp_path, "healthybin")
    _real_git_shim(d)
    monkeypatch.setenv("PATH", str(d))
    out = _run_status(repo, monkeypatch)

    assert status.INCOMPLETE_MARKER not in out, out
    assert "## Working tree: clean" in out
