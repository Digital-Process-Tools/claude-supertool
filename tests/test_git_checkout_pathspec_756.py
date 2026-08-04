"""#756 — `git-checkout:PATHSPEC` must not silently restore files over your work.

`git checkout <arg>` is two operations wearing one name. With a ref it switches
branches; with a pathspec it restores those paths from the index, discarding
whatever was in the working tree. Git selects between them by what the string
happens to name — and the second one writes no reflog entry, no stash and no
object, so what it overwrites is gone with nothing anywhere to recover it from.

`checkout.py` validated that the argument did not start with `-` (#150, on the
grounds that a prompt-influenced REF is reachable) and then handed the string
straight to git. A pathspec needs no flag and no special characters; it is the
*absence* of anything suspicious that makes it work.

The six fixtures that existed before this file could not see it: not one of them
ever passed a file path. So these run a real git against a real working tree
with real uncommitted content in it, and assert on the bytes on disk afterwards
— the only assertion that can tell a refusal from a very tidy deletion.

Hermetic: a tmp repo per test, no network, no remote, self-cleaning.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "git" / "checkout.py"
_spec = importlib.util.spec_from_file_location("git_checkout_756", PRESET)
assert _spec is not None and _spec.loader is not None
checkout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checkout)


_HERMETIC = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}

PRECIOUS = "PRECIOUS UNCOMMITTED WORK\n"


def _ok(args: list[str], cwd: str) -> str:
    res = subprocess.run(["git"] + args, cwd=cwd,
                         env={**os.environ, **_HERMETIC},
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, f"git {' '.join(args)} failed: {res.stderr}"
    return res.stdout.strip()


class _Repo:
    """A repo with one committed file, one committed dir, and a `docs` branch.

    `docs` is deliberately both a branch name and a directory name — the one
    case where git itself has to choose, and where "whatever git picks" is not
    an answer a caller can rely on.
    """

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st756_")
        self.path = os.path.join(self.tmp, "repo")
        os.makedirs(self.path)
        _ok(["init", "-q", "-b", "master", "."], self.path)
        Path(self.path, "work.txt").write_text("original line\n", encoding="utf-8")
        os.makedirs(os.path.join(self.path, "docs"))
        Path(self.path, "docs", "note.txt").write_text("committed note\n",
                                                       encoding="utf-8")
        _ok(["add", "-A"], self.path)
        _ok(["commit", "-qm", "base"], self.path)
        _ok(["branch", "docs"], self.path)

    def dirty(self, relpath: str) -> None:
        Path(self.path, relpath).write_text(PRECIOUS, encoding="utf-8")

    def content(self, relpath: str) -> str:
        return Path(self.path, relpath).read_text(encoding="utf-8")

    def branch(self) -> str:
        return _ok(["rev-parse", "--abbrev-ref", "HEAD"], self.path)


@pytest.fixture
def repo():
    made: list[_Repo] = []

    def make() -> _Repo:
        box = _Repo()
        made.append(box)
        return box

    yield make
    for box in made:
        subprocess.run(["rm", "-rf", box.tmp], check=False)


def _checkout(box: _Repo, arg: str, monkeypatch) -> tuple[int, str]:
    """Run the real op inside the repo. Returns (rc, stdout)."""
    for key, val in _HERMETIC.items():
        monkeypatch.setenv(key, val)
    monkeypatch.chdir(box.path)
    monkeypatch.setattr(checkout.sys, "argv", ["checkout.py", arg])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = checkout.main()
    return rc, buf.getvalue()


# ── the data-loss path ───────────────────────────────────────────────────

def test_a_file_path_argument_does_not_destroy_uncommitted_work(
        repo, monkeypatch) -> None:
    """The reproduction from the issue, asserted on the bytes rather than the rc.

    Before the guard this passed `work.txt` to `git checkout`, git restored it
    from the index, and the op then reported `Working tree: clean` — true only
    because it had just made it so.
    """
    box = repo()
    box.dirty("work.txt")

    rc, out = _checkout(box, "work.txt", monkeypatch)

    assert box.content("work.txt") == PRECIOUS, (
        "the uncommitted edit was destroyed — there is no reflog entry, no "
        "stash and no object to recover it from"
    )
    assert rc == 1
    assert "Working tree: clean" not in out


def test_a_directory_argument_does_not_destroy_uncommitted_work(
        repo, monkeypatch) -> None:
    """`.` is the widest form of the same argument and takes the whole tree."""
    box = repo()
    box.dirty("work.txt")
    box.dirty("docs/note.txt")

    rc, out = _checkout(box, ".", monkeypatch)

    assert box.content("work.txt") == PRECIOUS
    assert box.content("docs/note.txt") == PRECIOUS
    assert rc == 1
    assert "Working tree: clean" not in out


def test_the_refusal_names_what_was_passed_and_what_to_do(
        repo, monkeypatch) -> None:
    """A refusal that does not say what to do instead just moves the problem."""
    box = repo()
    box.dirty("work.txt")

    rc, out = _checkout(box, "work.txt", monkeypatch)

    assert rc == 1
    assert "work.txt" in out, "the refusal must name the argument it refused"
    assert "path" in out.lower()
    assert "git checkout -- work.txt" in out, (
        "someone who genuinely wanted the restore must be told how to ask git "
        "for it directly, unambiguously, and with their eyes open"
    )


def test_a_clean_file_path_is_refused_too(repo, monkeypatch) -> None:
    """The guard is about what the argument *is*, not about what it would cost.

    Refusing only when there is something to lose would make the op's behaviour
    depend on working-tree state — the same class of surprise as the original.
    """
    box = repo()

    rc, out = _checkout(box, "work.txt", monkeypatch)

    assert rc == 1
    assert "work.txt" in out


# ── the ambiguous argument ───────────────────────────────────────────────

def test_a_name_that_is_both_a_branch_and_a_directory_switches_the_branch(
        repo, monkeypatch) -> None:
    """`docs` names a branch and a directory. The op must pick one, always.

    Ref wins, pinned by a `--` separator rather than left to git's DWIM, and
    the uncommitted content under the directory must survive the switch.
    """
    box = repo()
    box.dirty("docs/note.txt")

    rc, out = _checkout(box, "docs", monkeypatch)

    assert rc == 0
    assert box.branch() == "docs"
    assert box.content("docs/note.txt") == PRECIOUS


def test_the_ambiguous_case_says_it_was_ambiguous(repo, monkeypatch) -> None:
    """Deterministic is not enough on its own — the caller has to be told."""
    box = repo()

    rc, out = _checkout(box, "docs", monkeypatch)

    assert rc == 0
    assert "also names a path" in out
    assert "as a ref" in out


# ── the receipt ──────────────────────────────────────────────────────────

def test_identical_shas_are_reported_as_no_branch_change(
        repo, monkeypatch) -> None:
    """`master → master` at the same sha is the signal that nothing moved.

    In the original report it was printed unflagged directly above `Working
    tree: clean`, and the two together read as a successful no-op switch.
    """
    box = repo()

    rc, out = _checkout(box, "master", monkeypatch)

    assert rc == 0
    assert "no branch change occurred" in out


def test_a_real_switch_is_not_labelled_a_no_op(repo, monkeypatch) -> None:
    """The flag has to distinguish, or it is noise on every line."""
    box = repo()
    _ok(["checkout", "-q", "docs"], box.path)
    Path(box.path, "later.txt").write_text("later\n", encoding="utf-8")
    _ok(["add", "-A"], box.path)
    _ok(["commit", "-qm", "later"], box.path)

    rc, out = _checkout(box, "master", monkeypatch)

    assert rc == 0
    assert box.branch() == "master"
    assert "no branch change occurred" not in out


# ── the recovery paths the guard must not hijack (#649, #267, #277) ──────

def test_a_ref_that_is_not_a_path_still_reports_not_found(
        repo, monkeypatch) -> None:
    """A missing branch is not a pathspec, and must keep its own diagnosis.

    The guard fires on "names a path that git cannot resolve as a commit". A
    name that is neither must fall through untouched to the fetch recoveries.
    """
    box = repo()

    rc, out = _checkout(box, "no-such-branch-anywhere", monkeypatch)

    assert rc == 1
    assert "not found" in out
    assert "names a path" not in out


def test_an_ordinary_branch_switch_still_works(repo, monkeypatch) -> None:
    """The guard sits in front of the happy path; the happy path must survive."""
    box = repo()
    _ok(["branch", "feature"], box.path)

    rc, out = _checkout(box, "feature", monkeypatch)

    assert rc == 0
    assert box.branch() == "feature"
    assert "# git-checkout:" in out
