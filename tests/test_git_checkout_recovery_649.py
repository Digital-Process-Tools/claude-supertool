"""#649 — `git-checkout`'s recoveries, pinned by behaviour instead of by message.

`checkout.py` has three recovery paths, and every one of them rewrites local
state: `fetch --all --prune` drops remote-tracking refs, `checkout -b --track`
creates a branch, `checkout -B <ref> FETCH_HEAD` creates *and moves* one. All
three were selected by scanning git's human error message for `pathspec` /
`did not match any`.

That message is translated. Under `LANGUAGE=fr` git answers `le specificateur
de chemin 'x' ne correspond a aucun fichier connu de git`; the substrings never
match and all three recoveries silently stop firing. This is #641's defect one
preset over: a state-changing decision taken on a channel the code does not
control — there, a pre-push hook's prose; here, whichever language git was
built to speak to this user.

The mocked fixtures that covered these paths could not see it. Each one handed
`_git` the English sentence and then asserted the code parsed the English
sentence, so they proved the implementation matched itself — and they passed in
every locale, because there was no real git in them to translate anything.
That is the shape #649 is about, so the replacements here run a real git
against a real bare remote, assert on *where the caller ends up*, and are
parametrised over the locale rather than blind to it.

None of the assertions below name a git message. The C-locale run is free to
check the phrasing supertool itself prints; the `fr` run checks only state and
the one message invariant #267 established.

Hermetic: a bare remote plus clones under a tmp dir, no network, self-cleaning.
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
_spec = importlib.util.spec_from_file_location("git_checkout_649", PRESET)
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
}


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git"] + args, cwd=cwd,
                          env={**os.environ, **_HERMETIC, "LANGUAGE": "", "LC_ALL": "C"},
                          capture_output=True, text=True, timeout=60)


def _ok(args: list[str], cwd: str) -> str:
    res = _run(args, cwd)
    assert res.returncode == 0, f"git {' '.join(args)} failed: {res.stderr}"
    return res.stdout.strip()


def _commit(cwd: str, fname: str, msg: str) -> None:
    Path(cwd, fname).write_text(msg, encoding="utf-8")
    _ok(["add", fname], cwd)
    _ok(["commit", "-m", msg], cwd)


def _git_speaks_french() -> bool:
    """True when this git has the fr catalog — i.e. the bug is reproducible here.

    Without it, `LANGUAGE=fr` yields English and the fr parametrisation would
    silently become a duplicate of the C one: a test that passes while testing
    nothing. Skipping loudly is the honest alternative.
    """
    with tempfile.TemporaryDirectory(prefix="st649_probe_") as tmp:
        subprocess.run(["git", "init", "-q", "."], cwd=tmp,
                       env={**os.environ, **_HERMETIC}, capture_output=True,
                       text=True, timeout=30)
        env = {**os.environ, **_HERMETIC, "LANGUAGE": "fr"}
        env.pop("LC_ALL", None)  # LC_ALL=C outranks LANGUAGE and un-translates git
        res = subprocess.run(["git", "checkout", "no-such-ref"], cwd=tmp, env=env,
                             capture_output=True, text=True, timeout=30)
        return "pathspec" not in (res.stderr + res.stdout)


LOCALES = [
    pytest.param("", id="C"),
    pytest.param(
        "fr", id="fr",
        marks=pytest.mark.skipif(not _git_speaks_french(),
                                 reason="this git has no fr message catalog — "
                                        "the locale defect is not reproducible here"),
    ),
]


class _Remote:
    """A bare remote, a `work` clone that publishes to it, and `mine` under test."""

    def __init__(self, single_branch: bool = False) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st649_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.work = os.path.join(self.tmp, "work")
        self.mine = os.path.join(self.tmp, "mine")

        _ok(["init", "--bare", "-q", "remote.git"], self.tmp)
        _ok(["symbolic-ref", "HEAD", "refs/heads/master"], self.remote)
        _ok(["clone", "-q", self.remote, "work"], self.tmp)
        _ok(["checkout", "-q", "-b", "master"], self.work)
        _commit(self.work, "a.txt", "base")
        _ok(["push", "-q", "-u", "origin", "master"], self.work)

        clone = ["clone", "-q"]
        if single_branch:
            clone += ["--single-branch", "--branch", "master"]
        _ok(clone + [self.remote, "mine"], self.tmp)

    def publish(self, branch: str, msg: str) -> str:
        """Push a branch to the remote *after* `mine` was cloned. Returns its SHA.

        The branch also rewrites `a.txt`, so a local uncommitted edit to that
        file genuinely blocks the switch — a branch that only *adds* files can
        be switched to with a dirty tree and would make the dirty-tree tests
        below assert nothing.
        """
        _ok(["checkout", "-q", "-B", branch, "master"], self.work)
        _commit(self.work, "a.txt", f"a.txt on {branch}")
        _commit(self.work, f"{branch}.txt", msg)
        _ok(["push", "-q", "origin", branch], self.work)
        sha = _ok(["rev-parse", "HEAD"], self.work)
        _ok(["checkout", "-q", "master"], self.work)
        return sha

    def head(self) -> str:
        return _ok(["rev-parse", "HEAD"], self.mine)

    def branch(self) -> str:
        return _ok(["rev-parse", "--abbrev-ref", "HEAD"], self.mine)

    def has_ref(self, ref: str) -> bool:
        return _run(["rev-parse", "--verify", "--quiet", ref], self.mine).returncode == 0


@pytest.fixture
def remote_factory(monkeypatch):
    made: list[_Remote] = []

    def make(single_branch: bool = False) -> _Remote:
        box = _Remote(single_branch=single_branch)
        made.append(box)
        return box

    yield make
    for box in made:
        subprocess.run(["rm", "-rf", box.tmp], check=False)


def _checkout(box: _Remote, ref: str, locale: str, monkeypatch) -> tuple[int, str]:
    """Run the real op inside `mine`, under `locale`. Returns (rc, stdout)."""
    for key, val in _HERMETIC.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("LANGUAGE", locale)
    if locale:
        # LC_ALL=C outranks LANGUAGE for gettext — leaving it set would quietly
        # turn the fr run back into a second English run.
        monkeypatch.delenv("LC_ALL", raising=False)
    else:
        monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.chdir(box.mine)
    monkeypatch.setattr(checkout.sys, "argv", ["checkout.py", ref])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = checkout.main()
    return rc, buf.getvalue()


# ── the predicate the recoveries now turn on ─────────────────────────────

@pytest.mark.parametrize("locale", LOCALES)
def test_ref_missing_answers_from_the_exit_code_not_the_message(
        remote_factory, monkeypatch, locale) -> None:
    """The whole point: the answer is identical in every language."""
    box = remote_factory()
    monkeypatch.setenv("LANGUAGE", locale)
    monkeypatch.chdir(box.mine)
    assert checkout._ref_missing("master") is False
    assert checkout._ref_missing("no-such-branch") is True
    # `-` is @{-1}; no fetch can conjure it, so it is never a recovery case.
    assert checkout._ref_missing("-") is False


# ── recovery 1: the branch was pushed after we cloned ────────────────────

@pytest.mark.parametrize("locale", LOCALES)
def test_a_branch_published_after_the_clone_is_reached(
        remote_factory, monkeypatch, locale) -> None:
    """The caller asked for a branch this clone has never heard of, and lands on
    it. Nothing here says how — auto-fetch, DWIM, tracking branch are the
    implementation's business."""
    box = remote_factory()
    sha = box.publish("feature", "published after the clone")

    rc, out = _checkout(box, "feature", locale, monkeypatch)

    assert rc == 0, out
    assert box.branch() == "feature"
    assert box.head() == sha


@pytest.mark.parametrize("locale", LOCALES)
def test_a_ref_that_exists_nowhere_fails_but_still_refreshed_the_remote(
        remote_factory, monkeypatch, locale) -> None:
    """Declining is right; declining *without looking* is not.

    The old fixture proved the fetch happened by inspecting the mock's call
    list, which pins the exact argv. This proves it from the outcome: a branch
    published after the clone is visible afterwards, so a fetch demonstrably
    ran — however it was spelled.
    """
    box = remote_factory()
    box.publish("sibling", "published after the clone")
    assert not box.has_ref("refs/remotes/origin/sibling")

    rc, out = _checkout(box, "no-such-branch", locale, monkeypatch)

    assert rc == 1
    assert box.branch() == "master"
    assert box.has_ref("refs/remotes/origin/sibling"), \
        "should have refreshed the remote before giving up"


def test_a_ref_that_exists_nowhere_says_so(remote_factory, monkeypatch) -> None:
    """The wording supertool itself chooses — ours, so safe to pin (C run only)."""
    box = remote_factory()
    rc, out = _checkout(box, "no-such-branch", "", monkeypatch)
    assert rc == 1
    assert "not found" in out and "after fetch" in out


# ── recovery 2: single-branch clone, no origin/<branch> to DWIM from ──────

@pytest.mark.parametrize("locale", LOCALES)
def test_a_narrowed_refspec_clone_still_reaches_the_branch(
        remote_factory, monkeypatch, locale) -> None:
    """#267. `fetch --all` on a `--single-branch` clone never creates
    `origin/feature`, so the ordinary retry cannot work — the op has to fetch
    the ref explicitly. Asserted as arrival, not as a command sequence."""
    box = remote_factory(single_branch=True)
    sha = box.publish("feature", "unreachable via the narrowed refspec")

    rc, out = _checkout(box, "feature", locale, monkeypatch)

    assert rc == 0, out
    assert box.branch() == "feature"
    assert box.head() == sha


@pytest.mark.parametrize("locale", LOCALES)
def test_a_dirty_tree_blocking_the_narrowed_recovery_is_not_reported_as_missing(
        remote_factory, monkeypatch, locale) -> None:
    """#267's invariant, and the one message claim worth pinning in any locale.

    The fetch reached the ref; only the switch was blocked. Saying "not found"
    there sends the caller after the wrong problem — and it is a claim the op
    makes in its own words, so it is checkable whatever language git speaks.
    """
    box = remote_factory(single_branch=True)
    box.publish("feature", "exists on the remote")
    Path(box.mine, "a.txt").write_text("uncommitted local edit", encoding="utf-8")

    rc, out = _checkout(box, "feature", locale, monkeypatch)

    assert rc == 1
    assert box.branch() == "master"
    assert "not found" not in out.lower(), out
    assert Path(box.mine, "a.txt").read_text(encoding="utf-8") == "uncommitted local edit"


def test_a_dirty_tree_blocking_the_narrowed_recovery_names_the_blocker(
        remote_factory, monkeypatch) -> None:
    """C run only: naming *uncommitted changes* means reading git's prose, which
    is exactly what does not survive a translation. Kept deliberately — the hint
    is worth having where it works, and the test above holds the line that
    matters everywhere."""
    box = remote_factory(single_branch=True)
    box.publish("feature", "exists on the remote")
    Path(box.mine, "a.txt").write_text("uncommitted local edit", encoding="utf-8")

    rc, out = _checkout(box, "feature", "", monkeypatch)

    assert rc == 1
    assert "uncommitted changes" in out.lower() or "stash" in out.lower()
