"""git-push's receipt must not turn an absence in the tool into a claim about the world (#661, #662, #663).

Three checks in `presets/git/push.py`, one defect class:

  * **#661** — `(branch created)` was inferred from `remote_before` being
    empty, and `remote_before` is empty whenever `@{upstream}` does not
    resolve. That is a fact about local config, not about the remote. Unset
    `branch.<name>.merge` and a `--force-with-lease` push still overwrites an
    existing branch (the lease reads the remote-tracking *ref*), so the
    receipt announced a *creation* on the one operation here that destroys
    work irrecoverably.
  * **#662** — the ahead/behind block guarded on `returncode == 0` with no
    `else`, and the uncommitted-leftovers check never read the return code at
    all. A `git status` that exited non-zero produced empty stdout, an empty
    list, and therefore silence — which in this receipt means "clean tree".
  * **#663** — the timeout advice named `ops.git-push.timeout`, which cannot
    lengthen `_PUSH_TIMEOUT`. Advice that cannot work is worse than none
    (#633): silence does not lie.

The invariant is the three-state contract from `docs/validators.md`
("Declining instead of guessing"): the check ran and found nothing, the check
ran and found something, the check could not run. Half of that contract is the
half that rots, so the silent case is pinned here too — an in-sync push with
working checks must still print nothing extra, or the absence of a warning
stops being a positive claim.

Everything is a real git repository and no `_git` is mocked (#649): a mocked
failure pins the implementation rather than the contract. The three failures
come from real, verified mechanisms, each asserted to have taken effect before
anything is asserted about the receipt:

  * `status.showUntrackedFiles = bogus` makes `git status` — and only `git
    status` — exit 128. `rev-parse`, `rev-list`, `log` and `push` are
    unaffected (`_assert_only_status_is_broken`).
  * unsetting `remote.origin.fetch` leaves the branch's upstream *config* in
    place while `@{upstream}` stops resolving, so `rev-list --left-right
    --count HEAD...@{upstream}` fails while the push still lands. This is the
    "odd remote layout" the code's own comment names.
  * unsetting `branch.<name>.remote`/`.merge` leaves the remote-tracking ref,
    so `--force-with-lease` still passes its lease check and still overwrites
    the remote — while the pre-push SHA the receipt used to reason from is
    empty.

The timeout is real too: `_PUSH_TIMEOUT` is set to 0 for one call, so the push
subprocess is genuinely killed by `subprocess.run`'s own clock. The constant is
the only thing substituted; the timeout path is executed for real.
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
_spec = importlib.util.spec_from_file_location("git_push_661", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


_HERMETIC_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Mate",
    "GIT_AUTHOR_EMAIL": "mate@t",
    "GIT_COMMITTER_NAME": "Mate",
    "GIT_COMMITTER_EMAIL": "mate@t",
    "GIT_TERMINAL_PROMPT": "0",
}

# The config that breaks `git status` and nothing else.
_BROKEN_STATUS_MODE = "bogus"


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
                          capture_output=True, text=True, timeout=60)


def _commit(cwd: str, fname: str, msg: str) -> None:
    Path(cwd, fname).write_text(msg, encoding="utf-8")
    assert _run(["add", fname], cwd).returncode == 0
    assert _run(["commit", "-m", msg], cwd).returncode == 0


class _Sandbox:
    """Bare remote + `mine` (repo under test) + `mate` (the colleague)."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st661_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.mine = os.path.join(self.tmp, "mine")
        self.mate = os.path.join(self.tmp, "mate")

        assert _run(["init", "--bare", "-b", "feature", "remote.git"],
                    self.tmp).returncode == 0
        assert _run(["clone", self.remote, "mine"], self.tmp).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        _commit(self.mine, "a.txt", "base on remote")
        assert _run(["push", "-u", "origin", "feature"], self.mine).returncode == 0

    # ── setup helpers ────────────────────────────────────────────────────

    def mate_pushes(self, msg: str = "mate work nobody told you about") -> str:
        """A colleague pushes a commit; `mine` fetches it. Returns its SHA."""
        assert _run(["clone", self.remote, "mate"], self.tmp).returncode == 0
        assert _run(["checkout", "feature"], self.mate).returncode == 0
        _commit(self.mate, "r.txt", msg)
        assert _run(["push", "origin", "feature"], self.mate).returncode == 0
        assert _run(["fetch", "origin"], self.mine).returncode == 0
        return _run(["rev-parse", "origin/feature"], self.mine).stdout.strip()

    def rewrite_over_remote(self) -> None:
        """Drop back to the shared base and build a different commit on it."""
        assert _run(["reset", "--hard", "HEAD"], self.mine).returncode == 0
        _commit(self.mine, "c.txt", "my rewrite")

    def add_commit_on_top(self) -> None:
        _commit(self.mine, "d.txt", "more of my own work")

    def start_a_branch_the_remote_has_never_seen(self) -> None:
        assert _run(["checkout", "-b", "brand-new"], self.mine).returncode == 0
        _commit(self.mine, "n.txt", "work on a genuinely new branch")
        assert _run(["rev-parse", "--verify", "refs/heads/brand-new"],
                    self.remote).returncode != 0, (
            "the remote already has this branch — a 'creation' test on it "
            "would prove nothing")

    def drop_upstream_config(self) -> None:
        """Leave the remote-tracking ref, remove the branch's upstream config.

        The lease reads the tracking ref, so the force-push still lands; only
        `@{upstream}` — and therefore the pre-push SHA — goes away.
        """
        _run(["config", "--unset", "branch.feature.remote"], self.mine)
        _run(["config", "--unset", "branch.feature.merge"], self.mine)
        assert _run(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                     "@{upstream}"], self.mine).returncode != 0, (
            "upstream still resolves — this test would prove nothing")
        assert _run(["rev-parse", "origin/feature"], self.mine).returncode == 0, (
            "the remote-tracking ref is gone; the lease would reject the push "
            "and nothing destructive would happen")

    def break_git_status(self) -> None:
        assert _run(["config", "status.showUntrackedFiles", _BROKEN_STATUS_MODE],
                    self.mine).returncode == 0
        self._assert_only_status_is_broken()

    def _assert_only_status_is_broken(self) -> None:
        assert _run(["status", "--porcelain"], self.mine).returncode != 0, (
            "fixture did not break `git status` — this test would prove nothing")
        for ok in (["rev-parse", "HEAD"], ["log", "--format=%h", "-1"],
                   ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
                   ["rev-parse", "--short", "origin/feature"]):
            assert _run(ok, self.mine).returncode == 0, (
                f"fixture broke more than `git status`: {' '.join(ok)}")

    def break_upstream_resolution(self) -> None:
        """Drop the fetch refspec: `@{upstream}` stops resolving, pushes land.

        The "odd remote layout" the receipt's own fallback comment names. The
        branch keeps its upstream *config*, so nothing is re-set by `-u`, and
        the ahead/behind check has nothing to resolve — for real.
        """
        assert _run(["config", "--unset", "remote.origin.fetch"],
                    self.mine).returncode == 0
        assert _run(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
                    self.mine).returncode != 0, (
            "the ahead/behind check still runs — this test would prove nothing")
        for ok in (["status", "--porcelain"], ["rev-parse", "HEAD"],
                   ["log", "--format=%h", "-1"]):
            assert _run(ok, self.mine).returncode == 0, (
                f"fixture broke more than the upstream lookup: {' '.join(ok)}")

    def leave_work_uncommitted(self) -> None:
        Path(self.mine, "forgotten.txt").write_text("not staged", encoding="utf-8")
        assert _run(["status", "--porcelain"], self.mine).stdout.strip(), (
            "nothing is actually uncommitted — this test would prove nothing")

    # ── observation ──────────────────────────────────────────────────────

    def head(self) -> str:
        return _run(["rev-parse", "HEAD"], self.mine).stdout.strip()

    def remote_tip(self, branch: str = "feature") -> str:
        return _run(["rev-parse", branch], self.remote).stdout.strip()

    def assert_push_landed(self, branch: str = "feature") -> None:
        assert self.remote_tip(branch) == self.head(), (
            "the push did not land; a receipt about a push that never "
            "happened proves nothing")

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


import tempfile  # noqa: E402  (kept next to nothing else that needs it)


@pytest.fixture
def box():
    s = _Sandbox()
    try:
        yield s
    finally:
        s.close()


def _verdict(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "no output at all"
    assert lines[-1].startswith("[result] "), (
        f"the verdict is not the last line (#623/#638):{chr(10)}{out}")
    return lines[-1]


# ══ #661 — "(branch created)" must be git's claim, not our inference ══════

def test_a_force_push_over_an_existing_branch_is_not_called_a_creation(box) -> None:
    """#661's whole point.

    The branch exists on the remote, `@{upstream}` does not resolve, and the
    force-push overwrites it. The receipt used to read `(branch created)` —
    the least alarming possible story, on the operation that destroys work
    irrecoverably. Someone who force-pushed over a colleague's branch reads
    that and stops looking.
    """
    destroyed = box.mate_pushes()
    box.rewrite_over_remote()
    box.drop_upstream_config()

    rc, out = box.drive_push("force-with-lease")

    box.assert_push_landed()
    assert _run(["merge-base", "--is-ancestor", destroyed, "HEAD"],
                box.mine).returncode != 0, (
        "nothing was actually overwritten — this test would prove nothing")
    assert rc == 0, f"the push landed; the verdict must not claim failure:{chr(10)}{out}"
    assert "branch created" not in out, (
        f"#661: an existing branch was force-overwritten and the receipt says "
        f"it was created:{chr(10)}{out}")


def test_an_ordinary_push_onto_an_existing_branch_is_not_called_a_creation(box) -> None:
    """The variant with nothing else to catch it.

    No `:force-with-lease`, so the discard machinery from #655 never runs and
    says nothing. `(branch created)` was the receipt's only statement about the
    remote, and the branch had been there all along.
    """
    box.add_commit_on_top()
    box.drop_upstream_config()

    rc, out = box.drive_push()

    box.assert_push_landed()
    assert rc == 0
    assert "branch created" not in out, (
        f"#661: an existing branch was fast-forwarded and the receipt says it "
        f"was created:{chr(10)}{out}")
    assert "already existed" in out, (
        f"the receipt does not say the branch was already there:{chr(10)}{out}")


def test_an_overwritten_branch_says_what_the_remote_was(box) -> None:
    """Not merely 'not a creation' — the receipt names the SHA it overwrote.

    git reported it on the porcelain channel; declining to print it would
    replace one uninformative line with another.
    """
    box.mate_pushes()
    overwritten = _run(["rev-parse", "--short", "origin/feature"],
                       box.mine).stdout.strip()
    box.rewrite_over_remote()
    box.drop_upstream_config()

    _, out = box.drive_push("force-with-lease")

    assert overwritten in out, (
        f"the SHA git said it overwrote ({overwritten}) is nowhere in the "
        f"receipt:{chr(10)}{out}")
    assert "force-updated" in out, (
        f"the receipt does not say the branch was force-updated:{chr(10)}{out}")


def test_a_genuinely_new_branch_is_still_reported_as_created(box) -> None:
    """The half that rots.

    `[new branch]` is git's own answer and the claim stays — otherwise the fix
    trades a false claim for a useless one, and every first push starts
    reading as an unknown.
    """
    box.start_a_branch_the_remote_has_never_seen()

    rc, out = box.drive_push()

    box.assert_push_landed("brand-new")
    assert rc == 0
    assert "branch created" in out, (
        f"a genuinely new branch is no longer reported as created:{chr(10)}{out}")
    assert "UNKNOWN" not in out, (
        f"a fully-evidenced creation was reported as unknown:{chr(10)}{out}")


def test_push_outcome_reads_gits_own_per_ref_summary() -> None:
    """The classifier, on the exact bytes git emits. No repository needed."""
    created = "*\tHEAD:refs/heads/feature\t[new branch]" + chr(10)
    assert push._push_outcome(created, "feature") == ("created", "", "")

    forced = ("+\tHEAD:refs/heads/feature\t5fed41a...e9b8922 (forced update)"
              + chr(10))
    assert push._push_outcome(forced, "feature")[:2] == ("forced", "5fed41a")

    fast_forward = (" \trefs/heads/feature:refs/heads/feature\tfdd75b3..ab4880e"
                    + chr(10))
    assert push._push_outcome(fast_forward, "feature")[:2] == ("updated", "fdd75b3")

    up_to_date = "=\trefs/heads/feature:refs/heads/feature\t[up to date]" + chr(10)
    assert push._push_outcome(up_to_date, "feature")[0] == "uptodate"


def test_push_outcome_declines_when_git_said_nothing() -> None:
    """No per-ref line is an absence of information, never a creation.

    The *reason* is asserted, not merely its existence: "git said nothing about
    this ref" and "git said something this grammar does not cover" are
    different problems with different next steps, and a decline that cannot
    tell them apart hands the caller a shrug.
    """
    kind, old, why = push._push_outcome("", "feature")
    assert kind == "unknown" and old == ""
    assert "no per-ref status line" in why, (
        f"the decline does not say git reported nothing for this ref: {why}")

    other_ref = "*\tHEAD:refs/heads/other\t[new branch]" + chr(10)
    kind, _, why = push._push_outcome(other_ref, "feature")
    assert kind == "unknown"
    assert "no per-ref status line" in why, (
        f"a line for another ref is not a line for ours: {why}")

    unreadable = "+\tHEAD:refs/heads/feature\tforced update" + chr(10)
    kind, _, why = push._push_outcome(unreadable, "feature")
    assert kind == "unknown"
    assert "without the SHA it overwrote" in why, (
        f"the decline does not name what was unreadable: {why}")


def test_a_forced_update_needs_three_dots_to_be_a_forced_update() -> None:
    """The grammar, not a guess.

    `<old>...<new>` is a forced update and `<old>..<new>` is a fast-forward;
    reading the second as the first would hand a caller a SHA off a summary
    that does not mean what the code thinks it means. A `+` flag whose summary
    is not in the forced shape is `unknown`, not a parse attempt.
    """
    two_dots_under_a_force_flag = ("+\tHEAD:refs/heads/feature\tabc1234..def5678"
                                   + chr(10))
    kind, _, why = push._push_outcome(two_dots_under_a_force_flag, "feature")
    assert kind == "unknown", (
        "a two-dot summary was read as a forced update — the two shapes are "
        "the only thing separating 'overwrote' from 'fast-forwarded'")
    assert why
    assert push._forced_update_old_sha(two_dots_under_a_force_flag,
                                       "feature") == ""


def test_an_unreadable_outcome_declines_loudly_and_carries_to_the_verdict(capsys) -> None:
    """The third state at the seam: no evidence either way, said out loud.

    A receipt is read from the bottom (#623), so the doubt has to reach the
    `[result]` line or a caller reading only the verdict still walks away with
    the reassuring reading.
    """
    moved, note = push._report_first_seen_remote("abc1234", "", "feature",
                                                 "origin/feature")

    out = capsys.readouterr().out
    assert moved is True
    assert "UNKNOWN" in out, f"the decline is not loud:{chr(10)}{out}"
    assert "branch created" not in out
    assert "git reflog show origin/feature" in out, (
        f"no command the caller can settle it with:{chr(10)}{out}")
    assert "UNKNOWN" in note, "the verdict would not carry the doubt"


def test_the_rebase_recovery_receipt_reads_gits_answer_too(box) -> None:
    """The non-fast-forward recovery path feeds the same receipt.

    It re-pushes after rebasing, and it used to do so without `--porcelain` —
    so the receipt it then printed had no per-ref line to read and would have
    had to decline on a push whose outcome git had stated plainly. Same
    branch, same inference, one code path over.
    """
    box.mate_pushes()
    box.add_commit_on_top()
    box.drop_upstream_config()

    rc, out = box.drive_push()

    assert rc == 0, f"the rebase recovery did not complete:{chr(10)}{out}"
    box.assert_push_landed()
    assert "rebased onto remote" in out, (
        f"this did not go through the recovery path — the test proves "
        f"nothing:{chr(10)}{out}")
    assert "branch created" not in out
    assert "UNKNOWN" not in out, (
        f"git stated the outcome on the porcelain channel and the receipt "
        f"declined anyway:{chr(10)}{out}")
    assert "already existed" in out


# ══ #662a — a failed ahead/behind check is not "in sync" ══════════════════

def test_a_failed_ahead_behind_check_says_so(box) -> None:
    """`if returncode == 0:` with no `else` printed nothing at all.

    Nothing is exactly what an in-sync push prints, so a check that could not
    run was indistinguishable from one that ran and found agreement.
    """
    box.add_commit_on_top()
    box.break_upstream_resolution()

    rc, out = box.drive_push()

    box.assert_push_landed()
    assert rc == 0
    assert "vs upstream" in out, (
        f"#662: the ahead/behind check failed and the receipt is silent about "
        f"it — silence here means 'in sync':{chr(10)}{out}")
    assert "UNKNOWN" in out
    assert "rev-list" in out, (
        f"the command that settles it is not named:{chr(10)}{out}")
    assert "128" in out and "not stored as a remote-tracking branch" in out, (
        f"the decline reports neither git's exit code nor its reason, so the "
        f"caller cannot tell a broken repo from an odd layout:{chr(10)}{out}")


def test_an_in_sync_push_still_says_in_sync(box) -> None:
    """The working case is untouched — the decline must not fire on it."""
    box.add_commit_on_top()

    rc, out = box.drive_push()

    assert rc == 0
    assert "vs upstream: in sync" in out, (
        f"the ordinary ahead/behind line was lost:{chr(10)}{out}")
    assert "vs upstream: UNKNOWN" not in out


# ══ #662b — a failed `git status` is not a clean tree ═════════════════════

def test_a_failed_leftovers_check_does_not_read_as_a_clean_tree(box) -> None:
    """`git status --porcelain` exited non-zero and nobody looked.

    Empty stdout became `[]` became silence became "nothing uncommitted" — and
    the warning that exists to catch work you forgot to commit went quiet on
    precisely the run where git was not answering.
    """
    box.add_commit_on_top()
    box.leave_work_uncommitted()
    box.break_git_status()

    rc, out = box.drive_push()

    box.assert_push_landed()
    assert rc == 0
    assert "UNKNOWN" in out, (
        f"#662: `git status` failed with work genuinely left behind, and the "
        f"receipt reads as a clean tree:{chr(10)}{out}")
    assert "git status --porcelain` exited 128" in out, (
        f"the decline reports no exit code, so a caller cannot tell a broken "
        f"config from a missing feature:{chr(10)}{out}")
    assert _BROKEN_STATUS_MODE in out, (
        f"the reason the check failed was swallowed:{chr(10)}{out}")


def test_leftovers_are_still_counted_when_the_check_works(box) -> None:
    box.add_commit_on_top()
    box.leave_work_uncommitted()

    rc, out = box.drive_push()

    assert rc == 0
    assert "NOT in this push" in out, (
        f"the leftovers warning was lost:{chr(10)}{out}")
    assert "UNKNOWN" not in out


def test_a_clean_tree_stays_silent(box) -> None:
    """Silence keeps meaning "checked, nothing left behind"."""
    box.add_commit_on_top()

    rc, out = box.drive_push()

    assert rc == 0
    assert "NOT in this push" not in out, (
        f"a clean tree was warned about:{chr(10)}{out}")
    assert "UNCOMMITTED" not in out.upper() or "NOT in this push" in out


def test_uncommitted_leftovers_declines_rather_than_reporting_clean(box) -> None:
    """The seam, directly: three states, and the decline names why."""
    box.leave_work_uncommitted()
    box.break_git_status()

    prev = os.getcwd()
    os.chdir(box.mine)
    os.environ.update({k: v for k, v in _HERMETIC_ENV.items() if v is not None})
    try:
        changes, why = push._uncommitted_leftovers()
    finally:
        os.chdir(prev)

    assert changes is None, (
        "a failed check returned a list, as if it had run")
    assert why, "a decline with no reason is not a decline"


# ══ #663 — the timeout advice must name the budget that actually cut ══════

def test_timeout_advice_does_not_name_a_knob_that_cannot_move_the_budget(
        box, monkeypatch) -> None:
    """`ops.git-push.timeout` cannot lengthen `_PUSH_TIMEOUT` (#633/#663).

    The push subprocess is genuinely killed here — only the constant is
    substituted, the timeout path runs for real. The remote already matches
    HEAD, so this is the PUSHED branch, the one that hands the caller advice.
    """
    box.add_commit_on_top()
    rc, _ = box.drive_push()
    assert rc == 0, "the setup push must land, or the timeout path is not the one under test"

    monkeypatch.setattr(push, "_PUSH_TIMEOUT", 0)
    rc, out = box.drive_push()

    assert rc == 0, f"the remote matches HEAD; this is a landed push:{chr(10)}{out}"
    assert "timed out" in out.lower() or "outlasted" in out.lower(), (
        f"the timeout path was not exercised:{chr(10)}{out}")
    assert "_PUSH_TIMEOUT" in out, (
        f"#663: the advice does not name the budget that actually cut:{chr(10)}{out}")
    assert "presets/git/push.py" in out, (
        f"the advice names no place the caller can look:{chr(10)}{out}")
    assert "raise ops.git-push.timeout in .supertool.json" not in out, (
        f"#663: the receipt still tells the caller to raise a knob that cannot "
        f"lengthen this budget:{chr(10)}{out}")


def test_timeout_advice_points_at_something_the_caller_can_actually_do(
        box, monkeypatch) -> None:
    """"Edit the source" is not advice on its own.

    On this path the push *landed*; the only cost of the timeout is a
    truncated receipt, and re-running the op prints it in full. That is a
    lever the caller has right now, without changing any configuration.
    """
    box.add_commit_on_top()
    box.drive_push()

    monkeypatch.setattr(push, "_PUSH_TIMEOUT", 0)
    _, out = box.drive_push()

    assert "git-push" in out and "re-run" in out.lower(), (
        f"the caller is told what cut them and nothing they can do:{chr(10)}{out}")
    assert _verdict(out).startswith("[result] PUSHED"), (
        f"a landed push must not be reported as a failure:{chr(10)}{out}")
