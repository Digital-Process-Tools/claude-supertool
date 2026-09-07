"""`gh-pr-merge|cleanup` names which issues it verified closed, so a
caller-side lane record can be released in the same call (#2337).

`cleanup` already reaps the merged branch's worktree and remote branch, but
had no way to tell a caller-provided release hook which issues the merge
closed -- a calling loop's own per-issue lane bookkeeping (`claude-oss`'s
`.oss-lanes/<issue>.json`) was never released, silently narrowing what a
later dispatch-selection pass could see as free until that caller's own TTL
expired.

The fix is the explicit-list shape the issue's own "What would settle it"
leans toward, not a generic callback: `gh-pr-merge` has no concept of
`claude-oss`'s lane registry and should not grow one. `release_candidates()`
reuses the same `verdicts` list already computed for the `## Linked issues`
section -- the information this needs was already inside the op at the
moment `cleanup` runs, per the issue body -- filtered to same-repo issues
verified `CLOSED`. A cross-repo ref (`owner/repo#N`) never names an issue in
the caller's own tracker, so it is excluded rather than guessed at.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_merge.py"
_spec = importlib.util.spec_from_file_location(
    "gh_pr_merge_release_candidates_2337", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _sibling(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_main = _sibling("test_gh_pr_merge_main_950.py", "gh_pr_merge_main_for_2337")


# ---------------------------------------------------------------------------
# `release_candidates()` in isolation -- the pure filter
# ---------------------------------------------------------------------------

def test_a_closed_same_repo_issue_is_named() -> None:
    verdicts = [("#924", "CLOSED", "")]
    assert m.release_candidates(verdicts) == ["924"]


def test_an_open_same_repo_issue_is_excluded() -> None:
    """The must-fire's paired must-not-fire: an issue this merge did NOT
    close must never be handed to a caller as safe to release."""
    verdicts = [("#899", "OPEN", "")]
    assert m.release_candidates(verdicts) == []


def test_an_unknown_state_issue_is_excluded() -> None:
    verdicts = [("#899", m.UNKNOWN, "state not returned")]
    assert m.release_candidates(verdicts) == []


def test_a_cross_repo_closed_issue_is_excluded() -> None:
    """A caller's own per-issue lane bookkeeping is keyed to issues in its
    own repository -- a ref naming another repository's issue is never one
    of the caller's own records, however this merge closed it."""
    verdicts = [("other/repo#12", "CLOSED", "")]
    assert m.release_candidates(verdicts) == []


def test_a_mix_names_only_the_same_repo_closed_ones() -> None:
    verdicts = [("#924", "CLOSED", ""), ("#899", "OPEN", ""),
               ("other/repo#12", "CLOSED", "")]
    assert m.release_candidates(verdicts) == ["924"]


# ---------------------------------------------------------------------------
# end to end through `main()|cleanup` -- the same helpers 2225 stubs, so no
# real `git`/`gh api` call is made by a test asserting about the receipt
# ---------------------------------------------------------------------------

def _install_main(monkeypatch, h, *, argv):
    monkeypatch.setattr(m, "_gh", h.gh)
    monkeypatch.setattr(m, "_gh_json", h.gh_json)
    monkeypatch.setattr(m, "_load_pr_module", h.pr_module)
    monkeypatch.setattr(m.subprocess, "run", h.subprocess_run)
    monkeypatch.setattr(_main.sys, "argv", ["pr_merge.py"] + argv)
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_CONFIRM", "1")


def _stub_cleanup_items(monkeypatch):
    monkeypatch.setattr(m, "_cleanup_remote_branch",
                        lambda head, oid: ("remote branch", m.CLEAN_DONE,
                                           "stubbed"))
    monkeypatch.setattr(m, "_cleanup_worktree",
                        lambda head: ("local worktree", m.CLEAN_DONE,
                                     "stubbed"))
    monkeypatch.setattr(m, "_cleanup_local_branch",
                        lambda head: ("local branch", m.CLEAN_DONE,
                                     "stubbed"))


def test_a_verified_closed_issue_is_named_as_a_release_candidate(
        monkeypatch, capsys) -> None:
    """The must-fire case: `cleanup` on a merge that closed #924 names #924
    as a release candidate, in the same call -- not a manual second step."""
    _stub_cleanup_items(monkeypatch)
    h = _main._Harness(_main._pr())  # default body "Closes #924", CLOSED
    _install_main(monkeypatch, h, argv=["944", "cleanup"])
    m.main()
    out = capsys.readouterr().out
    cleanup = out.split("## Cleanup")[1]
    assert "[release] 924" in cleanup, cleanup
    assert "#2337" in cleanup, cleanup


def test_an_ordinary_cleanup_closing_nothing_names_none(
        monkeypatch, capsys) -> None:
    """The paired must-not-fire: a cleanup that closed nothing must not
    invent a release candidate -- an absence here is a real absence, not a
    check that never ran."""
    _stub_cleanup_items(monkeypatch)
    h = _main._Harness(_main._pr(body="no keyword here"), bound=[],
                       issue_states={})
    _install_main(monkeypatch, h, argv=["944", "cleanup"])
    m.main()
    out = capsys.readouterr().out
    cleanup = out.split("## Cleanup")[1]
    assert "[release] none" in cleanup, cleanup


def test_an_unclosed_issue_is_never_offered_as_a_release_candidate(
        monkeypatch, capsys) -> None:
    """A merge that did not actually close the issue it declares must never
    hand that issue over as safe to release -- the exact failure this whole
    mechanism exists to prevent."""
    _stub_cleanup_items(monkeypatch)
    h = _main._Harness(_main._pr(body="Closes #899"), bound=[],
                       issue_states={"#899": "OPEN"})
    _install_main(monkeypatch, h, argv=["944", "cleanup"])
    m.main()
    out = capsys.readouterr().out
    cleanup = out.split("## Cleanup")[1]
    assert "[release] none" in cleanup, cleanup
    release_line = [ln for ln in cleanup.splitlines() if "[release]" in ln][0]
    assert "899" not in release_line, release_line


def test_an_unverified_merge_never_offers_a_release_candidate(
        monkeypatch, capsys) -> None:
    """Self-review finding: `[release]` used to print unconditionally,
    unlike every other line in `## Cleanup`, which is gated on
    `m_state == MERGED`. An issue can read CLOSED for a reason unrelated to
    this merge attempt (a duplicate, a manual close, an earlier attempt) --
    naming it as something THIS merge verified closed, while the merge
    itself is unconfirmed and every reap item reads `skipped`, is exactly
    the premature-release failure this whole mechanism exists to prevent."""
    _stub_cleanup_items(monkeypatch)
    h = _main._Harness(_main._pr(), merge_rc=1,
                       after={"state": "OPEN", "mergedAt": None,
                              "mergeCommit": None},
                       issue_states={"#924": "CLOSED"})
    _install_main(monkeypatch, h, argv=["944", "cleanup"])
    m.main()
    out = capsys.readouterr().out
    cleanup = out.split("## Cleanup")[1]
    assert "[release] none" in cleanup, cleanup
    assert "924" not in cleanup.split("[release]")[1].split(chr(10))[0]


def test_a_changelog_fragment_exists() -> None:
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(2337)
