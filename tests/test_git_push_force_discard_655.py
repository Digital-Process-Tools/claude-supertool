"""#655 — a force-push that destroyed commits could report destroying nothing.

`_discarded_by_force` had three return paths and two of them were a bare `[]`:
no pre-push remote SHA, and a `git log` that exited non-zero. The caller could
not tell either apart from "the check ran and nothing was discarded", so a
check that *could not run* rendered byte-for-byte like a clean force-push — on
the one operation in this op that destroys work irrecoverably, and the one
check whose subject is other people's commits.

The invariant these tests pin is the three-state contract from
`docs/validators.md`: silence means the check ran and found nothing, a listing
means it ran and found some, and an inability to check says so — in the body
*and* on the `[result]` line, which is the part that survives `| tail -3`.

Everything here is a real git repository: a bare "remote", a working clone, and
a second clone standing in for a colleague. No `_git` is mocked, because a
mocked failure pins the implementation rather than the contract (#649). The two
failures are produced by real mechanisms:

  * `log.date = <invalid>` in the repo config makes `git log` — and only
    `git log` — exit non-zero. `status`, `rev-parse`, `rev-list`, `diff` and
    `push` are all unaffected, verified in `_assert_only_log_is_broken`, so the
    push still lands and only the discard check is unable to answer. This is
    not contrived: any broken `log.*` config, local or inherited, does it.
  * unsetting `branch.<name>.remote`/`.merge` leaves the remote-tracking ref in
    place, so `--force-with-lease` still passes its lease check and the push
    still overwrites the remote — while `@{upstream}` no longer resolves and
    the pre-push SHA the check used to depend on is empty.

Every fixture asserts it actually took effect before anything is asserted about
the receipt: a `git log` that did not break, or a force-push that did not
force, would make these tests pass while testing nothing.
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
_spec = importlib.util.spec_from_file_location("git_push_655", PRESET)
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

# The config that breaks `git log` and nothing else.
_BROKEN_LOG_DATE = "not-a-date-format"


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
                          capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")


def _commit(cwd: str, fname: str, msg: str) -> None:
    Path(cwd, fname).write_text(msg, encoding="utf-8")
    assert _run(["add", fname], cwd).returncode == 0
    assert _run(["commit", "-m", msg], cwd).returncode == 0


class _Sandbox:
    """Bare remote + `mine` (repo under test) + `mate` (the colleague)."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st655_")
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
        # `mine` fetches, so its remote-tracking ref is current and the lease
        # will pass — the exact shape --force-with-lease does NOT protect.
        assert _run(["fetch", "origin"], self.mine).returncode == 0
        return _run(["rev-parse", "origin/feature"], self.mine).stdout.strip()

    def rewrite_over_remote(self) -> None:
        """Drop back to the shared base and build a different commit on it."""
        assert _run(["reset", "--hard", "HEAD"], self.mine).returncode == 0
        _commit(self.mine, "c.txt", "my rewrite")

    def add_commit_on_top(self) -> None:
        """Ordinary new work — a force-push here discards nothing."""
        _commit(self.mine, "d.txt", "more of my own work")

    def break_git_log(self) -> None:
        assert _run(["config", "log.date", _BROKEN_LOG_DATE],
                    self.mine).returncode == 0
        self._assert_only_log_is_broken()

    def _assert_only_log_is_broken(self) -> None:
        """The fixture must break the discard check and nothing else."""
        assert _run(["log", "--format=%h", "-1"], self.mine).returncode != 0, (
            "fixture did not break `git log` — this test would prove nothing")
        for ok in (["status", "--porcelain"], ["rev-parse", "HEAD"],
                   ["rev-list", "--count", "HEAD"], ["diff", "--name-only"]):
            assert _run(ok, self.mine).returncode == 0, (
                f"fixture broke more than `git log`: {' '.join(ok)}")

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

    # ── observation ──────────────────────────────────────────────────────

    def head(self) -> str:
        return _run(["rev-parse", "HEAD"], self.mine).stdout.strip()

    def remote_tip(self) -> str:
        return _run(["rev-parse", "feature"], self.remote).stdout.strip()

    def assert_force_push_landed(self, destroyed: str = "") -> None:
        """The remote really was overwritten — otherwise nothing is at stake."""
        assert self.remote_tip() == self.head(), (
            "the push did not land; a receipt about a push that never "
            "happened proves nothing")
        if destroyed:
            reachable = _run(["merge-base", "--is-ancestor", destroyed, "HEAD"],
                             self.mine).returncode == 0
            assert not reachable, (
                "the commit is still on the branch — nothing was discarded, "
                "so this test would prove nothing")

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


def _verdict(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "no output at all"
    assert lines[-1].startswith("[result] "), (
        f"the verdict is not the last line (#623/#638):{chr(10)}{out}")
    return lines[-1]


def _reads_as_unknown(out: str) -> bool:
    return "UNKNOWN" in out and "discard check" in out.lower()


# ── the load-bearing case: the check fails while commits ARE discarded ────

def test_failed_discard_check_does_not_read_as_a_clean_force_push(box) -> None:
    """#655's whole point.

    A colleague's commit is genuinely destroyed AND the discard check cannot
    run. Before the fix both `git log` failing and "nothing was discarded"
    returned `[]`, so this rendered identically to a clean force-push: the
    receipt reassured precisely where it should have warned.
    """
    destroyed = box.mate_pushes()
    box.rewrite_over_remote()
    box.break_git_log()

    rc, out = box.drive_push("force-with-lease")

    box.assert_force_push_landed(destroyed)
    assert rc == 0, f"the push landed; the verdict must not claim failure:{chr(10)}{out}"
    assert _reads_as_unknown(out), (
        f"#655: the discard check failed and the receipt does not say so — "
        f"this reads as a clean force-push:{chr(10)}{out}")
    assert _verdict(out) != _clean_verdict_for(box), (
        "the verdict is indistinguishable from a force-push that discarded "
        "nothing")
    assert "UNKNOWN" in _verdict(out), (
        f"the warning does not survive `| tail -3` — the caller who reads the "
        f"verdict alone is still told this was clean:{chr(10)}{out}")


def _clean_verdict_for(box: _Sandbox) -> str:
    """The verdict text a clean, fully-checked force-push would have printed."""
    return (f"[result] PUSHED  feature -> origin/feature @ {box.head()[:7]}  "
            "(verified)")


def test_failed_discard_check_names_the_reason_and_a_way_to_look(box) -> None:
    """"Could not check" is only useful if it says why and what to run."""
    box.mate_pushes()
    box.rewrite_over_remote()
    box.break_git_log()

    _, out = box.drive_push("force-with-lease")

    assert _BROKEN_LOG_DATE in out or "unknown date format" in out, (
        f"the reason the check failed was swallowed:{chr(10)}{out}")
    assert "git log" in out or "git reflog" in out, (
        f"no command the caller can run to look themselves:{chr(10)}{out}")


# ── silence stays a positive claim ───────────────────────────────────────

def test_a_force_push_that_discards_nothing_stays_silent(box) -> None:
    """The check ran and found nothing → no warning, no scary default.

    This is the test that stops the fix from overcorrecting: a working check
    with nothing to report must print nothing extra, so that the absence of a
    warning keeps meaning "checked, clean" rather than "who knows".
    """
    box.add_commit_on_top()

    rc, out = box.drive_push("force-with-lease")

    box.assert_force_push_landed()
    assert rc == 0
    assert not _reads_as_unknown(out), (
        f"a clean force-push was warned about — silence is no longer a "
        f"positive claim:{chr(10)}{out}")
    assert "discarded" not in out.lower(), (
        f"nothing was discarded and the receipt says otherwise:{chr(10)}{out}")
    assert "UNKNOWN" not in _verdict(out)


def test_an_ordinary_push_never_mentions_discards(box) -> None:
    """No `:force-with-lease`, no discard machinery — not even a reassurance.

    A warning that fires on ordinary pushes is noise, and noise is how the
    real one stops being read.
    """
    box.add_commit_on_top()

    rc, out = box.drive_push()

    assert rc == 0
    assert "discard" not in out.lower(), (
        f"the discard check spoke on an ordinary push:{chr(10)}{out}")


def test_an_ordinary_push_is_silent_even_when_git_log_is_broken(box) -> None:
    """The failure must not leak onto the non-destructive path."""
    box.add_commit_on_top()
    box.break_git_log()

    rc, out = box.drive_push()

    assert rc == 0
    assert not _reads_as_unknown(out), (
        f"a broken `git log` warned about discards on a push that could not "
        f"discard anything:{chr(10)}{out}")


# ── the working case keeps working ───────────────────────────────────────

def test_a_force_push_that_discards_commits_names_them(box) -> None:
    destroyed = box.mate_pushes("mate work nobody told you about")
    box.rewrite_over_remote()

    rc, out = box.drive_push("force-with-lease")

    box.assert_force_push_landed(destroyed)
    assert rc == 0
    assert "mate work nobody told you about" in out, (
        f"the destroyed commit was not listed:{chr(10)}{out}")
    assert "Mate" in out, f"the author of the destroyed work is not named:{chr(10)}{out}"
    assert not _reads_as_unknown(out), (
        f"the check ran fine and the receipt claims it did not:{chr(10)}{out}")


def test_discarded_commits_survive_to_the_verdict_line(box) -> None:
    """#623's rule applied to this signal: the tail must carry it too."""
    box.mate_pushes()
    box.rewrite_over_remote()

    _, out = box.drive_push("force-with-lease")

    assert "1" in _verdict(out) and "discard" in _verdict(out).lower(), (
        f"a destroyed commit is invisible to a caller reading the verdict "
        f"alone:{chr(10)}{out}")


# ── the third state: no pre-push SHA, but the push still destroyed work ──

def test_no_upstream_config_still_reports_the_commits_it_destroyed(box) -> None:
    """The `not old_remote_sha` path is reachable AND harmful — established.

    `--force-with-lease` leases against the *remote-tracking ref*, while the
    op's pre-push SHA comes from `@{upstream}`, which needs
    `branch.<name>.remote`/`.merge`. Remove only the latter and the push still
    overwrites the remote while the op has no SHA to compare against — the
    caller destroyed a colleague's commit and the old code, guarded by
    `and remote_before` at the call site, said nothing at all.

    git itself reports the SHA it overwrote on the `--porcelain` channel
    (`+ <old>...<new> (forced update)`), which is the same machine-readable
    source #641 established for the non-fast-forward decision.
    """
    destroyed = box.mate_pushes("colleague commit erased with no upstream cfg")
    box.rewrite_over_remote()
    box.drop_upstream_config()

    rc, out = box.drive_push("force-with-lease")

    box.assert_force_push_landed(destroyed)
    assert rc == 0
    assert "colleague commit erased with no upstream cfg" in out, (
        f"#655: a force-push destroyed a colleague's commit and the receipt "
        f"never mentioned it:{chr(10)}{out}")


def test_no_upstream_config_and_no_way_to_check_says_unknown(box) -> None:
    """Both sources gone → the loud third state, not silence."""
    destroyed = box.mate_pushes()
    box.rewrite_over_remote()
    box.drop_upstream_config()
    box.break_git_log()

    rc, out = box.drive_push("force-with-lease")

    box.assert_force_push_landed(destroyed)
    assert rc == 0
    assert _reads_as_unknown(out), (
        f"no pre-push SHA and no working `git log`, and the receipt reads as "
        f"a clean force-push:{chr(10)}{out}")
    assert "UNKNOWN" in _verdict(out)


# ── unit level: the three states, against a real repository ──────────────

def _check_in(box: _Sandbox, sha: str):
    prev_cwd = os.getcwd()
    os.chdir(box.mine)
    try:
        return push._discarded_by_force(sha)
    finally:
        os.chdir(prev_cwd)


def test_discarded_by_force_reports_a_checked_empty_result(box) -> None:
    commits, why = _check_in(box, box.head())
    assert commits == [], why
    assert why == "", "a successful check must not carry a reason"


def test_discarded_by_force_reports_what_it_found(box) -> None:
    old = box.mate_pushes("found me")
    box.rewrite_over_remote()
    commits, why = _check_in(box, old)
    assert why == ""
    assert commits is not None and len(commits) == 1
    assert "found me" in commits[0]


def test_discarded_by_force_declines_when_git_log_fails(box) -> None:
    """The state that used to be indistinguishable from `[]`."""
    old = box.mate_pushes()
    box.rewrite_over_remote()
    box.break_git_log()
    commits, why = _check_in(box, old)
    assert commits is None, "a failed check returned a list, as if it had run"
    assert why, "a decline with no reason is not a decline"


def test_discarded_by_force_declines_on_an_empty_sha() -> None:
    commits, why = push._discarded_by_force("")
    assert commits is None
    assert why


# ── the porcelain fallback, on git's own output ──────────────────────────

def test_forced_update_old_sha_reads_gits_own_report() -> None:
    line = ("To /tmp/remote.git" + chr(10) +
            "+\tHEAD:refs/heads/feature\t5fed41a...e9b8922 (forced update)" +
            chr(10) + "Done" + chr(10))
    assert push._forced_update_old_sha(line, "feature") == "5fed41a"


def test_forced_update_old_sha_ignores_a_plain_fast_forward() -> None:
    line = ("To /tmp/remote.git" + chr(10) +
            " \trefs/heads/feature:refs/heads/feature\tfdd75b3..ab4880e" +
            chr(10) + "Done" + chr(10))
    assert push._forced_update_old_sha(line, "feature") == ""


def test_forced_update_old_sha_ignores_another_ref(box=None) -> None:
    line = ("+\tHEAD:refs/heads/other\t5fed41a...e9b8922 (forced update)"
            + chr(10))
    assert push._forced_update_old_sha(line, "feature") == ""


def test_forced_update_old_sha_on_a_new_branch_is_empty() -> None:
    line = "*\tHEAD:refs/heads/feature\t[new branch]" + chr(10)
    assert push._forced_update_old_sha(line, "feature") == ""


def test_forced_update_without_a_parseable_sha_yields_nothing() -> None:
    """A forced update whose summary we cannot read is not a SHA.

    Returning the raw summary here would hand `git log` a garbage revision and
    convert an unreadable answer into a *failed* check — the same wrong state
    by a longer route.
    """
    line = "+\tHEAD:refs/heads/feature\tforced update" + chr(10)
    assert push._forced_update_old_sha(line, "feature") == ""


def test_ref_status_still_answers_only_for_a_rejection(box=None) -> None:
    """#641's filter survived the refactor onto `_ref_line`.

    `_ref_line` reports every per-ref line whatever its flag, because the
    forced-update reader needs the `+` ones. `_ref_status` must keep answering
    for `!` alone — it feeds the rejection hints and the non-fast-forward
    decision, and a successful ref's summary is not a rejection reason.
    """
    ok_line = " \trefs/heads/feature:refs/heads/feature\tabc..def" + chr(10)
    assert push._ref_line(ok_line, "feature") == (" ", "abc..def")
    assert push._ref_status(ok_line, "feature") == ""

    rejected = "!\trefs/heads/feature:refs/heads/feature\t[rejected] (stale info)"
    assert push._ref_status(rejected, "feature") == "[rejected] (stale info)"


# ── the third state, at the seam, without a repository ───────────────────

def test_force_aftermath_declines_when_git_reported_no_ref_line(capsys) -> None:
    """Nothing to read and no SHA to compare — the loud state, not silence."""
    note = push._force_aftermath("", "", "origin", "feature")

    out = capsys.readouterr().out
    assert "UNKNOWN" in out and "DISCARD CHECK DID NOT RUN" in out
    assert "git reflog show origin/feature" in out, (
        f"no command the caller can settle it with:{chr(10)}{out}")
    assert "UNKNOWN" in note, "the verdict would not carry the warning"


def test_force_aftermath_declines_on_an_unreadable_forced_update(capsys) -> None:
    note = push._force_aftermath(
        "", "+\tHEAD:refs/heads/feature\tforced update" + chr(10),
        "origin", "feature")

    assert "UNKNOWN" in capsys.readouterr().out
    assert "UNKNOWN" in note


def test_force_aftermath_is_silent_when_git_says_it_did_not_force(capsys) -> None:
    """A fast-forward under `:force-with-lease` overwrote nothing.

    Silence here is decided from git's own per-ref flag, not assumed — which
    is what lets the absence of a warning stay a positive claim even on the
    path where the pre-push SHA is missing.
    """
    note = push._force_aftermath(
        "", " \trefs/heads/feature:refs/heads/feature\tabc..def" + chr(10),
        "origin", "feature")

    assert capsys.readouterr().out == "", "a non-forcing push was warned about"
    assert note == ""
