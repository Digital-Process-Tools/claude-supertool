"""#900 — `git-checkout`'s "Rebase in progress" must track the rebase, not its debris.

The warning existed to stop someone mid-checkout when a rebase is genuinely
stopped. It also fired on `.git/REBASE_HEAD`, and that ref is *not* a state
marker: git writes it while replaying a commit and, on the conflict →
`--continue` → completion path, never unlinks it. The rebase is over, the two
state directories are gone, `git status` reports nothing, and `git rebase
--abort` answers `fatal: no rebase in progress` — but the ref is still on disk,
so the warning fired on every subsequent checkout in that repo, forever, with no
remedy. Verified identical on git 2.39.5 and 2.46.2.

The counter-risk is the reason these tests are shaped the way they are: dropping
a term narrows what the check can see, and a *missed* rebase is worse than a
noisy one. So every stopped-rebase shape gets its own case — merge backend, am
backend (`--apply`), `--rebase-merges`, and the interactive `break` and `edit`
stops. The `break` case is the load-bearing one: it stops with **no**
`REBASE_HEAD` at all, which is the direct evidence that the ref was never the
signal holding this check up.

Assertions are on the rendered `git-checkout` output for a repo in each state —
never on which helper ran — so a check that did nothing would fail the four
must-warn cases, and the pre-fix check fails the stale-debris case.

Hermetic: repos under a tmp dir, no network, self-cleaning.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "git" / "checkout.py"
_spec = importlib.util.spec_from_file_location("git_checkout_900", PRESET)
assert _spec is not None and _spec.loader is not None
checkout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checkout)


REBASE_WARNING = "Rebase in progress"
MERGE_WARNING = "Merge in progress"

_HERMETIC = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
    "LANGUAGE": "",
}


def _run(args: list[str], cwd: str, extra_env: dict[str, str] | None = None):
    return subprocess.run(["git"] + args, cwd=cwd,
                          env={**os.environ, **_HERMETIC, **(extra_env or {})},
                          capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")


def _ok(args: list[str], cwd: str) -> str:
    res = _run(args, cwd)
    assert res.returncode == 0, f"git {' '.join(args)} failed: {res.stderr}"
    return res.stdout.strip()


def _commit(cwd: str, fname: str, body: str, msg: str) -> None:
    Path(cwd, fname).write_text(body, encoding="utf-8")
    _ok(["add", fname], cwd)
    _ok(["commit", "-m", msg], cwd)


@pytest.fixture
def repo():
    """A repo with `master`, a `topic` that conflicts with it, and a spare `other`.

    `topic` and `master` both rewrite `f.txt`, so a rebase of one onto the other
    genuinely stops — a rebase that cannot conflict would let the must-warn cases
    below pass while proving nothing.
    """
    made: list[str] = []

    def make() -> str:
        tmp = tempfile.mkdtemp(prefix="st900_")
        made.append(tmp)
        work = os.path.join(tmp, "repo")
        os.makedirs(work)
        _ok(["init", "-q", "-b", "master", "."], work)
        _commit(work, "f.txt", "base\n", "base")
        _ok(["branch", "other"], work)
        _ok(["checkout", "-q", "-b", "topic"], work)
        _commit(work, "f.txt", "topic\n", "topic")
        _ok(["checkout", "-q", "master"], work)
        _commit(work, "f.txt", "master\n", "master")
        _ok(["checkout", "-q", "topic"], work)
        return work

    yield make
    for tmp in made:
        subprocess.run(["rm", "-rf", tmp], check=False)


def _sequence_editor(tmp: str, transform: str) -> dict[str, str]:
    """A GIT_SEQUENCE_EDITOR that rewrites the todo list with `transform`.

    `transform` is python source operating on `t` (the todo text) and assigning
    the result back to `t`.
    """
    script = os.path.join(tmp, "seq_editor.py")
    Path(script).write_text(
        "import sys\n"
        "p = sys.argv[1]\n"
        "t = open(p).read()\n"
        f"{transform}\n"
        "open(p, 'w').write(t)\n",
        encoding="utf-8",
    )
    return {"GIT_SEQUENCE_EDITOR": f"{sys.executable} {script}"}


def _resolve_to_head(work: str) -> None:
    """Resolve the conflict as "keep ours", leaving the operation still in progress.

    Resolving to HEAD's own content is what makes the case reachable at all.
    `checkout.py` guards the switch on a clean worktree and returns *before* it
    ever evaluates the state warnings, so any resolution that leaves a staged
    delta — including the obvious `resolved` placeholder — is refused by
    supertool's own dirty-tree check and renders no warning to assert on. Only
    an index identical to HEAD reads as clean. This is also the realistic shape
    of the state the warning is for: the conflict is dealt with, `--continue`
    was never typed, and the user wanders off to another branch with
    `rebase-merge/`, `rebase-apply/` or `MERGE_HEAD` still on disk.
    """
    Path(work, "f.txt").write_text(_ok(["show", "HEAD:f.txt"], work) + chr(10),
                                   encoding="utf-8")
    _ok(["add", "f.txt"], work)
    assert not _ok(["status", "--porcelain"], work), "resolution left the tree dirty"


def _state(work: str) -> str:
    gd = os.path.join(work, ".git")
    return " ".join(
        f"{name}={'yes' if os.path.exists(os.path.join(gd, name)) else 'no'}"
        for name in ("REBASE_HEAD", "rebase-merge", "rebase-apply", "MERGE_HEAD")
    )


def _checkout(work: str, ref: str, monkeypatch) -> str:
    """Run the real op in `work` and return everything it rendered.

    The switch is asserted to have succeeded. `checkout.py` returns early when
    git refuses the switch, and the state warnings live *after* that point — so
    a failed switch renders no warning at all and would let every `not in`
    assertion below pass while proving nothing.
    """
    for key, val in _HERMETIC.items():
        monkeypatch.setenv(key, val)
    monkeypatch.chdir(work)
    monkeypatch.setattr(checkout.sys, "argv", ["checkout.py", ref])
    buf = io.StringIO()
    with redirect_stdout(buf):
        checkout.main()
    out = buf.getvalue()
    assert "# git-checkout:" in out, f"the switch itself did not happen:{chr(10)}{out}"
    return out


# ── the defect: debris left by a finished rebase must not warn ───────────────

def test_completed_rebase_after_conflict_leaves_no_warning(repo, monkeypatch) -> None:
    """conflict → resolve → `--continue` → done. This is #900 itself.

    git leaves `REBASE_HEAD` behind on exactly this path. Nothing a user can
    type clears it: `git rebase --abort` refuses, because there is no rebase.
    A warning that cannot be acted on is not a warning.
    """
    work = repo()
    assert _run(["rebase", "master"], work).returncode != 0, "rebase was meant to stop"
    Path(work, "f.txt").write_text("resolved\n", encoding="utf-8")
    _ok(["add", "f.txt"], work)
    _ok(["-c", "core.editor=true", "rebase", "--continue"], work)

    # The state this test is about — asserted, not assumed, so the test tells
    # you when a future git stops leaving the ref behind.
    gd = os.path.join(work, ".git")
    assert os.path.exists(os.path.join(gd, "REBASE_HEAD")), (
        "this git does not leave REBASE_HEAD after --continue; #900 is not "
        f"reproducible here ({_state(work)})")
    assert not os.path.exists(os.path.join(gd, "rebase-merge"))
    assert not os.path.exists(os.path.join(gd, "rebase-apply"))
    # git itself agrees there is nothing in progress.
    assert _run(["rebase", "--abort"], work).returncode != 0

    out = _checkout(work, "other", monkeypatch)
    assert REBASE_WARNING not in out, (
        f"warned on debris from a finished rebase ({_state(work)}):\n{out}")


def test_aborted_rebase_leaves_no_warning(repo, monkeypatch) -> None:
    """`--abort` does clean up REBASE_HEAD — contrary to #900's account of it.

    Recorded so the claim is pinned to observed behaviour rather than to the
    issue text, and so a regression in either direction is visible.
    """
    work = repo()
    assert _run(["rebase", "master"], work).returncode != 0
    _ok(["rebase", "--abort"], work)
    assert not os.path.exists(os.path.join(work, ".git", "REBASE_HEAD"))

    out = _checkout(work, "other", monkeypatch)
    assert REBASE_WARNING not in out, f"{_state(work)}:\n{out}"


def test_clean_rebase_leaves_no_warning(repo, monkeypatch) -> None:
    """A rebase that never conflicted writes no debris at all."""
    work = repo()
    _ok(["checkout", "-q", "other"], work)
    _commit(work, "side.txt", "side\n", "side")
    _ok(["rebase", "master"], work)

    out = _checkout(work, "master", monkeypatch)
    assert REBASE_WARNING not in out, f"{_state(work)}:\n{out}"


# ── the counter-risk: every genuinely stopped rebase must still warn ─────────

def test_stopped_merge_backend_rebase_warns(repo, monkeypatch) -> None:
    """The default backend, stopped on a conflict — the case the warning is for."""
    work = repo()
    assert _run(["rebase", "master"], work).returncode != 0
    _resolve_to_head(work)  # resolved but *not* continued: still in progress

    out = _checkout(work, "other", monkeypatch)
    assert REBASE_WARNING in out, f"missed a stopped rebase ({_state(work)}):\n{out}"


def test_stopped_am_backend_rebase_warns(repo, monkeypatch) -> None:
    """`--apply` uses `rebase-apply/`, a different directory from the default."""
    work = repo()
    assert _run(["rebase", "--apply", "master"], work).returncode != 0
    _resolve_to_head(work)
    assert os.path.exists(os.path.join(work, ".git", "rebase-apply"))

    out = _checkout(work, "other", monkeypatch)
    assert REBASE_WARNING in out, f"missed an am-backend rebase ({_state(work)}):\n{out}"


def test_stopped_rebase_merges_warns(repo, monkeypatch) -> None:
    """`--rebase-merges` builds a richer todo list but the same state directory."""
    work = repo()
    assert _run(["rebase", "--rebase-merges", "master"], work).returncode != 0
    _resolve_to_head(work)

    out = _checkout(work, "other", monkeypatch)
    assert REBASE_WARNING in out, f"missed a --rebase-merges rebase ({_state(work)}):\n{out}"


def test_interactive_break_stop_warns(repo, monkeypatch) -> None:
    """The load-bearing case: stopped at `break`, with **no** REBASE_HEAD.

    git has not replayed a commit yet, so the ref does not exist — yet the
    rebase is unambiguously in progress. Any future rewrite that leans on
    `REBASE_HEAD` to detect a rebase is blind here. This is the direct evidence
    that the two directories, not the ref, are what the check stands on.
    """
    work = repo()
    tmp = os.path.dirname(work)
    env = _sequence_editor(tmp, "t = 'break\\n' + t")
    res = _run(["rebase", "-i", "master"], work, env)
    assert res.returncode == 0, res.stderr
    assert os.path.exists(os.path.join(work, ".git", "rebase-merge"))
    assert not os.path.exists(os.path.join(work, ".git", "REBASE_HEAD")), (
        f"expected no REBASE_HEAD at a break stop, got {_state(work)}")

    out = _checkout(work, "other", monkeypatch)
    assert REBASE_WARNING in out, f"missed a break stop ({_state(work)}):\n{out}"


def test_interactive_edit_stop_warns(repo, monkeypatch) -> None:
    """Stopped at `edit`: conflict-free, clean tree, rebase still in progress."""
    work = repo()
    tmp = os.path.dirname(work)
    env = _sequence_editor(tmp, "t = t.replace('pick', 'edit', 1)")
    _run(["rebase", "-i", "other"], work, env)
    assert os.path.exists(os.path.join(work, ".git", "rebase-merge"))

    out = _checkout(work, "master", monkeypatch)
    assert REBASE_WARNING in out, f"missed an edit stop ({_state(work)}):\n{out}"


# ── the neighbouring line: MERGE_HEAD, re-derived rather than assumed ────────

def test_merge_state_never_survives_a_switch(repo, monkeypatch) -> None:
    """The merge warning is inert, and this is why — #900 assumed otherwise.

    #900 waves `MERGE_HEAD` through as "fine, git deletes it when the merge
    commit lands". True, and beside the point: git also deletes it on *any*
    checkout, including a no-op switch to the branch already checked out. So the
    two ways a stopped merge can meet `git-checkout` are (a) index still
    unmerged, where git refuses the switch and `checkout.py` returns before it
    reaches the warnings, and (b) index resolved, where the switch succeeds and
    takes `MERGE_HEAD` with it. Either way the line above the rebase check
    cannot render. It is harmless, so this change leaves it alone — but it is
    not load-bearing, and nobody should reason from it as if it were.

    Rebase state is the opposite, and that asymmetry is the point: `rebase-merge/`
    outlives the switch, which is why the rebase warning is worth having and
    worth getting right.
    """
    work = repo()
    _ok(["checkout", "-q", "master"], work)
    assert _run(["merge", "topic"], work).returncode != 0
    _resolve_to_head(work)
    assert os.path.exists(os.path.join(work, ".git", "MERGE_HEAD")), (
        "merge state was expected on disk before the switch")

    out = _checkout(work, "master", monkeypatch)  # a no-op switch, still enough
    assert not os.path.exists(os.path.join(work, ".git", "MERGE_HEAD")), (
        "git kept MERGE_HEAD across a checkout — the warning is reachable after "
        "all, and this case should become a must-warn test")
    assert MERGE_WARNING not in out, f"{_state(work)}:{chr(10)}{out}"


def test_landed_merge_leaves_no_warning(repo, monkeypatch) -> None:
    """git unlinks MERGE_HEAD when the merge commit lands — #900's claim, checked."""
    work = repo()
    _ok(["checkout", "-q", "master"], work)
    assert _run(["merge", "topic"], work).returncode != 0
    Path(work, "f.txt").write_text("resolved\n", encoding="utf-8")
    _ok(["add", "f.txt"], work)
    _ok(["commit", "-m", "merge"], work)
    assert not os.path.exists(os.path.join(work, ".git", "MERGE_HEAD"))

    out = _checkout(work, "other", monkeypatch)
    assert MERGE_WARNING not in out, f"{_state(work)}:\n{out}"
