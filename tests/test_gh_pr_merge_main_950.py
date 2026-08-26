"""gh-pr-merge end to end: the refusals actually stop the merge (#950).

The pure-function tests in `test_gh_pr_merge_950.py` pin what `gate()` decides.
These pin what `main()` *does* with that decision — which is a different claim,
and the more important one: a gate whose verdict is correct and whose caller
merges anyway is a gate that has silently stopped refusing. Every refusal here
asserts that `gh pr merge` was **never invoked**, not merely that the text said
no.

The partial-success path (merged, issue still open) is reached on purpose: it is
the receipt that must never render as a pass and it cannot be reached by
accident.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_merge.py"
_spec = importlib.util.spec_from_file_location("github_pr_merge_main", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _load_sibling(filename: str, name: str):
    """Import another test module by path.

    By path rather than by bare name: `tests/` is not guaranteed to be on
    `sys.path` under every invocation (`pytest tests/x.py` from the repo root
    and `python -m pytest` do not agree), and a plain import that works on one
    of them is a collection error on the other.
    """
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

REPO = "Digital-Process-Tools/claude-supertool"


def _leg(name: str, conclusion: str = "SUCCESS") -> dict:
    return {"name": name, "conclusion": conclusion, "status": "COMPLETED",
            "detailsUrl": "https://github.com/o/r/actions/runs/1/job/7"}


def _pr(**over) -> dict:
    d = {
        "number": 944, "title": "a change", "state": "OPEN", "isDraft": False,
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED", "baseRefName": "master",
        "headRefName": "fix/924", "headRefOid": "c" * 40,
        # An ordinary same-repo PR. Stated rather than omitted: an absent
        # field is `not established` and refuses every delete and every
        # printed delete command (#1281), which is a different case with its
        # own tests below.
        "isCrossRepository": False,
        "url": f"https://github.com/{REPO}/pull/944",
        "body": "Closes #924",
        "statusCheckRollup": [_leg("tests"), _leg("lint")],
    }
    d.update(over)
    return d


_DEFAULT = object()


class _Harness:
    """Routes every outward call the op makes, and records the writes.

    `bound` and `branch_stdout` take an explicit `_DEFAULT` sentinel rather than
    `None`/`""`, because `None` and the empty string are both *meaningful
    inputs* here — "the closing-issue list did not come back" and "gh-branch
    printed nothing" are two of the states under test, and an `or`-style default
    would silently swallow exactly the cases that matter.
    """

    def __init__(self, pr: dict, *, after: dict | None = None,
                 issue_states: dict | None = None,
                 bound=_DEFAULT,
                 declared: int | None = 2,
                 declared_names: list | None = None,
                 merge_rc: int = 0,
                 branch_stdout=_DEFAULT,
                 default_branch: str = "master",
                 behind_by: int = 0,
                 stack_prs: list | None = None,
                 fail_json: set | None = None):
        self.pr = pr
        # `""` is a real answer from `_repo_identity`, not a missing fixture:
        # the API reply can lack `defaultBranchRef.name` while still naming the
        # repository, and `main()` only bails on the repository half (#1292).
        self.default_branch = default_branch
        self.after = after if after is not None else {
            "state": "MERGED", "mergedAt": "2026-08-07T10:00:00Z",
            "mergeCommit": {"oid": "d" * 40}, "headRefName": pr["headRefName"]}
        self.issue_states = issue_states if issue_states is not None else {
            "#924": "CLOSED"}
        self.bound = ["#924"] if bound is _DEFAULT else bound
        self.declared = declared
        self.declared_names = declared_names or ["tests", "lint"]
        self.merge_rc = merge_rc
        self.branch_stdout = branch_stdout if branch_stdout is not _DEFAULT else (
            "# Is `master` green? — repo\n"
            "Branch master: GREEN\n"
            "Head: eeeeeee (eeeeeee) — 2m old\n"
            "Verdict: GREEN — every workflow concluded.\n"
            "Legs: 20 total: 20 passed, 0 failed, 0 pending\n")
        # #1257: how far the base branch has moved since the commit the checks
        # ran on. `0` is the fixture default because it is the case that says
        # nothing about the tally, not because it is the common one.
        self.behind_by = behind_by
        self.fail_json = fail_json or set()
        self.merge_calls: list = []
        self.readback_count = 0
        self.compare_calls: list = []
        # #1851: stacked-follow-up search. `()` — not `None` — is the ordinary
        # "nothing targets this branch" fixture default; `stack_prs` names the
        # rows a caller wants `gh pr list` to answer with, and `"stack"` in
        # `fail_json` makes the read fail outright, the third state.
        self.stack_prs = stack_prs if stack_prs is not None else []
        self.stack_calls: list = []

    # -- gh ---------------------------------------------------------------
    def gh(self, args, timeout=30):
        if args[:2] == ["pr", "merge"]:
            self.merge_calls.append(list(args))
            return subprocess.CompletedProcess(
                args, self.merge_rc, "", "" if self.merge_rc == 0 else "boom")
        raise AssertionError(f"unexpected raw gh call: {args}")

    def gh_json(self, args, timeout=30):
        if args[:2] == ["repo", "view"]:
            if "repo" in self.fail_json:
                return (None, "gh timed out")
            return ({"nameWithOwner": REPO,
                     "defaultBranchRef": ({"name": self.default_branch}
                                          if self.default_branch else {})}, "")
        # Over every argument, not `args[-1]` (#1679). `_repo_target.gh_args()`
        # appends `--repo OWNER/NAME` *after* the `--json` field list, so under
        # a `repo:` target the last argument is the slug and this branch never
        # matched: the PR read fell through to the read-back below, returned
        # the post-merge object, and `main()` refused a PR it had never read.
        # A harness rendering its own blindness as a product verdict — this
        # repo's defect class, inside the thing meant to detect it.
        if args[:2] == ["pr", "view"] and any("statusCheckRollup" in a
                                              for a in args):
            if "pr" in self.fail_json:
                return (None, "PR not found")
            return (self.pr, "")
        if args[:2] == ["pr", "view"]:
            self.readback_count += 1
            if "readback" in self.fail_json:
                return (None, "gh timed out")
            return (self.after, "")
        if args[:1] == ["api"] and "compare/" in args[1]:
            self.compare_calls.append(list(args))
            if "compare" in self.fail_json:
                return (None, "gh timed out")
            return ({"behind_by": self.behind_by,
                     "base_sha": "e" * 40,
                     "base_date": "2026-08-10T18:22:31Z"}, "")
        if args[:2] == ["api", "graphql"]:
            if self.bound is None:
                return (None, "graphql refused")
            nodes = [{"number": int(r.lstrip("#")),
                      "repository": {"nameWithOwner": REPO}}
                     for r in self.bound]
            return ({"data": {"repository": {"pullRequest": {
                "closingIssuesReferences": {"nodes": nodes}}}}}, "")
        if args[:2] == ["issue", "view"]:
            ref = "#" + args[2]
            state = self.issue_states.get(ref)
            if state is None:
                return (None, "rate limited")
            return ({"state": state}, "")
        if args[:2] == ["pr", "list"]:
            self.stack_calls.append(list(args))
            if "stack" in self.fail_json:
                return (None, "gh timed out")
            return (list(self.stack_prs), "")
        raise AssertionError(f"unexpected gh_json call: {args}")

    # -- the pr.py seam ---------------------------------------------------
    def pr_module(self):
        harness = self

        class _M:
            @staticmethod
            def _declared_for_commit(d):
                # 4th element is #1181's `reason`. This double is the seam
                # `test_gh_pr_merge_tally_seam_1181.py` exists to pin: it and
                # the caller can agree with each other while the real function
                # moves, so keep it in step with `pr.py`.
                return (harness.declared, harness.declared_names, [],
                        "" if harness.declared is not None
                        else "stubbed decline")

            @staticmethod
            def _actions_leg_names(rollup):
                return [c.get("name") for c in (rollup or [])
                        if isinstance(c, dict)]
        return _M()

    def subprocess_run(self, argv, **kw):
        return subprocess.CompletedProcess(argv, 0, self.branch_stdout, "")


def _install(monkeypatch, h: _Harness, *, argv: list):
    monkeypatch.setattr(m, "_gh", h.gh)
    monkeypatch.setattr(m, "_gh_json", h.gh_json)
    monkeypatch.setattr(m, "_load_pr_module", h.pr_module)
    monkeypatch.setattr(m.subprocess, "run", h.subprocess_run)
    monkeypatch.setattr(sys, "argv", ["pr_merge.py"] + argv)
    # The confirmation gate is exercised by its own test below; everywhere
    # else it is opted out through the documented env var rather than stubbed,
    # so these tests run the same code path an operator does.
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_CONFIRM", "1")


# ===========================================================================
# Every refusal must stop the merge, not merely describe it
# ===========================================================================

REFUSALS = [
    ("not open", {"state": "CLOSED"}, "CLOSED"),
    ("draft", {"isDraft": True}, "draft"),
    ("conflicting", {"mergeable": "CONFLICTING"}, "CONFLICTING"),
    ("mergeable unknown", {"mergeable": "UNKNOWN"}, "UNKNOWN"),
    ("merge state behind", {"mergeStateStatus": "BEHIND"}, "BEHIND"),
    ("merge state unrecognised", {"mergeStateStatus": "WAT"}, "WAT"),
    ("changes requested", {"reviewDecision": "CHANGES_REQUESTED"},
     "CHANGES_REQUESTED"),
    ("red leg", {"statusCheckRollup": [_leg("e2e", "FAILURE")]}, "not all green"),
    ("cancelled leg", {"statusCheckRollup": [_leg("e2e", "CANCELLED")]},
     "cancelled"),
    ("zero checks", {"statusCheckRollup": []}, "zero check runs"),
    ("unreadable rollup", {"statusCheckRollup": None}, "UNKNOWN"),
]


@pytest.mark.parametrize("label,over,needle",
                         REFUSALS, ids=[r[0] for r in REFUSALS])
def test_a_refusal_never_reaches_gh_pr_merge(monkeypatch, capsys, label, over,
                                             needle):
    h = _Harness(_pr(**over))
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 1, out
    assert h.merge_calls == [], f"{label}: gh pr merge WAS invoked"
    assert needle in out, out
    assert "[result] REFUSED" in out
    assert "nothing changed" in out


def test_an_unreconciled_tally_refuses_and_merges_nothing(monkeypatch, capsys):
    """`declared is None` — a doubt is not permission on a gate."""
    h = _Harness(_pr(), declared=None)
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert h.merge_calls == []
    # Not `... or "UNKNOWN" in out`: "UNKNOWN" appears in several unrelated
    # lines, so an `or` on it would keep this test green for the wrong reason.
    assert "TALLY UNVERIFIED" in out, out
    assert "how many the run declares could not be established" in out


def test_a_declared_shortfall_refuses_and_merges_nothing(monkeypatch, capsys):
    h = _Harness(_pr(), declared=14,
                 declared_names=["tests", "lint"] + [f"x{i}" for i in range(12)])
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert h.merge_calls == []
    assert "2 of 14" in out, out


def test_the_refusal_names_the_manual_route_and_no_bypass(monkeypatch, capsys):
    h = _Harness(_pr(mergeable="CONFLICTING"))
    _install(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    assert "no green-bypass" in out
    assert "gh pr merge 944 --squash" in out


# ===========================================================================
# The confirmation gate
# ===========================================================================

def test_without_force_the_gate_is_previewed_and_nothing_merges(monkeypatch,
                                                                capsys):
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=["944"])
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_CONFIRM", raising=False)
    monkeypatch.setattr(m._publish_safety, "_supertool_config", lambda: {})
    with pytest.raises(SystemExit) as e:
        m.main()
    assert e.value.code == 2
    assert h.merge_calls == [], "a merge happened without |force"
    out = capsys.readouterr()
    assert "Gate — passed" in out.out
    assert "requires explicit confirmation" in out.err


def test_force_suffix_is_accepted_and_merges(monkeypatch, capsys):
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=["944|force"])
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_CONFIRM", raising=False)
    monkeypatch.setattr(m._publish_safety, "_supertool_config", lambda: {})
    rc = m.main()
    assert rc == 0
    assert len(h.merge_calls) == 1


# ===========================================================================
# Argument handling
# ===========================================================================

def test_no_argument_is_usage_not_a_merge(monkeypatch, capsys):
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=[])
    assert m.main() == 1
    assert "usage" in capsys.readouterr().out
    assert h.merge_calls == []


def test_a_non_numeric_pr_is_refused(monkeypatch, capsys):
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=["master"])
    assert m.main() == 1
    assert h.merge_calls == []
    # A branch name where a number belongs must be named as such, not merged
    # against whatever `gh` would resolve it to.
    assert "usage: gh-pr-merge:NUMBER" in capsys.readouterr().out


def test_an_unrecognised_token_is_refused_rather_than_ignored(monkeypatch,
                                                              capsys):
    """A token nobody parsed is a token whose intent was silently dropped."""
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=["944", "sqaush"])
    assert m.main() == 1
    assert "sqaush" in capsys.readouterr().out
    assert h.merge_calls == []


@pytest.mark.parametrize("method", ["squash", "merge", "rebase"])
def test_each_merge_method_reaches_gh(monkeypatch, capsys, method):
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=["944", method])
    assert m.main() == 0
    assert h.merge_calls[0] == ["pr", "merge", "944", f"--{method}"]


def test_an_unreadable_pr_is_an_error_not_a_merge(monkeypatch, capsys):
    h = _Harness(_pr(), fail_json={"pr"})
    _install(monkeypatch, h, argv=["944"])
    assert m.main() == 1
    assert h.merge_calls == []
    assert "could not be read" in capsys.readouterr().out


# ===========================================================================
# The happy path, and then the partial that must not look like it
# ===========================================================================

def test_a_verified_merge_with_a_closed_issue_exits_zero(monkeypatch, capsys):
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "#924: CLOSED" in out
    assert "[result] MERGED and every linked issue verified closed" in out
    assert "Branch master: GREEN" in out


def test_merged_but_issue_still_open_does_not_exit_zero(monkeypatch, capsys):
    """The receipt this whole op exists for. PR #908's shape, end to end."""
    h = _Harness(_pr(body="Closes #899"), bound=[],
                 issue_states={"#899": "OPEN"})
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out

    assert rc == 1, "a merged-but-unclosed PR exited 0"
    assert len(h.merge_calls) == 1, "the merge itself must still have happened"
    assert "#899: OPEN — did NOT close" in out
    assert "NOT bound by GitHub" in out
    assert "gh issue close 899" in out
    assert "[result] MERGED, but linked issues NOT CLOSED" in out
    # It happened and it cannot be undone. Saying otherwise is the failure.
    assert "roll" not in out.lower().replace("rollup", "")
    assert "revert" not in out.lower()


def test_merged_but_issue_state_unreadable_is_unknown_not_success(monkeypatch,
                                                                  capsys):
    h = _Harness(_pr(), issue_states={})
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "rate limited" in out
    assert "[result] MERGED, linked issue state unknown" in out


def test_an_unbound_but_already_closed_ref_is_still_named(monkeypatch, capsys):
    h = _Harness(_pr(body="Closes #899"), bound=[],
                 issue_states={"#899": "CLOSED"})
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT bound by GitHub" in out


def test_a_pr_closing_nothing_still_merges_and_says_so(monkeypatch, capsys):
    h = _Harness(_pr(body="no keyword here"), bound=[], issue_states={})
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "[result] MERGED, no linked issue declared" in out


def test_an_unreadable_bound_list_downgrades_to_unknown(monkeypatch, capsys):
    h = _Harness(_pr(), bound=None)
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "closing-issue list could not be read" in out


# ===========================================================================
# Merge read-back — a zero exit is not a merge
# ===========================================================================

def test_a_zero_exit_with_an_unmerged_readback_is_not_a_success(monkeypatch,
                                                                capsys):
    h = _Harness(_pr(), after={"state": "OPEN", "mergedAt": None,
                               "mergeCommit": None})
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "[result] MERGE UNVERIFIED" in out
    assert "nothing rolled back" in out


def test_a_failed_readback_is_unverified_with_its_reason(monkeypatch, capsys):
    h = _Harness(_pr(), fail_json={"readback"})
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "gh timed out" in out
    assert "[result] MERGE UNVERIFIED" in out


def test_a_nonzero_gh_merge_is_surfaced_not_swallowed(monkeypatch, capsys):
    h = _Harness(_pr(), merge_rc=1,
                 after={"state": "OPEN", "mergedAt": None, "mergeCommit": None})
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "boom" in out
    assert "[result] MERGE UNVERIFIED" in out


def test_cleanup_is_named_after_a_verified_merge_and_never_run(monkeypatch,
                                                               capsys):
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    # "this invocation", not "this op" (#1670): the op does clean up, on
    # `|cleanup`, and the header used to deny it 69 lines above the pointer
    # that offers it. That the offered token is a real route is pinned by
    # property rather than by wording in
    # `test_gh_pr_merge_cleanup_pointer_1670.py`.
    assert "Cleanup — not run by this invocation" in out
    assert "fix/924" in out
    assert "Deliberately not chained" in out


# ---------------------------------------------------------------------------
# #1281 also reaches the commands this op *prints* — the default path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("over,expect", [
    ({"isCrossRepository": True}, "is true"),
    ({}, "did not come back"),
])
def test_a_head_not_in_this_repository_gets_no_printed_delete_command(
        monkeypatch, capsys, over, expect):
    """A printed command is run by the reader, so it is the same delete.

    Without `|cleanup` the op prints `gh api -X DELETE …/refs/heads/<head>`
    and `git branch -d <head>` for the reader to paste. Both name the head
    branch against **this** repository, so a fork PR whose branch is called
    `master` handed over the incident by copy-paste rather than by the arm
    that #1281 was filed about.
    """
    pr = _pr()
    if not over:
        pr.pop("isCrossRepository")
    else:
        pr.update(over)
    h = _Harness(pr)
    _install(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    assert "gh api -X DELETE" not in out
    assert "git branch -d" not in out
    assert expect in out


def test_a_head_named_after_the_default_branch_gets_no_printed_command(
        monkeypatch, capsys):
    h = _Harness(_pr(headRefName="master"))
    _install(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    assert "gh api -X DELETE" not in out
    assert "git branch -d" not in out
    assert "default branch" in out


def test_an_unread_default_branch_gets_no_printed_delete_command(
        monkeypatch, capsys):
    """`run_cleanup` refuses all three items here; the printed arm did not.

    The file's own comment calls a printed delete "the same decision made by
    the reader", and `run_cleanup` requires four facts before any delete: the
    merge, the repository, the default branch, and the ref identity. The
    printed arm tested `x_repo is not False or head == default_branch`, which
    an **empty** `default_branch` satisfies neither half of — `""` is not the
    head — so the guard fell through and printed a ready-to-paste
    `gh api -X DELETE .../refs/heads/fix/924` without ever establishing that
    the head is not this repository's default branch (#1292).

    Empty is a reachable state, not a hypothetical: `_repo_identity` returns
    it whenever the API answer lacks `defaultBranchRef.name`, `main()` bails
    only on the repository half, and `:1255` already renders
    `default_branch or '?'` — the file elsewhere knows the value can be
    missing.
    """
    h = _Harness(_pr(), default_branch="")
    _install(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    assert "gh api -X DELETE" not in out
    assert "git branch -d" not in out
    assert "default branch could not be read" in out


def test_the_printed_arm_and_run_cleanup_agree_on_an_unread_default_branch():
    """The parity claim itself, stated once rather than inferred twice.

    #1292 is not that one arm was wrong in isolation — it is that two arms
    implementing the same decision had drifted. `run_cleanup` refuses; this
    pins that it does, next to the test above pinning that the printed arm now
    does too, so a future edit to either one has to face the pair.
    """
    rows = m.run_cleanup("fix/924", merged=True, cross_repo=False,
                         default_branch="", head_oid="c" * 40)
    assert rows and all(state == m.CLEAN_REFUSED for _, state, _ in rows)
    assert all("default branch could not be read" in detail
               for _, _, detail in rows)


def test_an_ordinary_same_repo_head_still_gets_its_commands(
        monkeypatch, capsys):
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    assert "gh api -X DELETE" in out and "git branch -d fix/924" in out


def test_cleanup_is_withheld_when_the_merge_is_not_confirmed(monkeypatch,
                                                             capsys):
    h = _Harness(_pr(), after={"state": "OPEN", "mergedAt": None,
                               "mergeCommit": None})
    _install(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    assert "Skipped — the merge is not confirmed" in out
    assert "git worktree remove" not in out


# ===========================================================================
# The default branch, which the PR's own green said nothing about
# ===========================================================================

def test_a_red_default_branch_reaches_the_verdict_line(monkeypatch, capsys):
    h = _Harness(_pr(), branch_stdout=(
        "Branch master: NOT GREEN\n"
        "Head: fffffff (fffffff) — 1m old\n"
        "Verdict: NOT GREEN — 1 leg did not pass.\n"
        "Legs: 20 total: 19 passed, 1 failed, 0 pending\n"))
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 0, "a red default branch is reported, not an op failure"
    assert "Default branch: NOT GREEN" in out
    assert "Run for this merge commit:" in out


def test_a_gh_branch_that_does_not_answer_is_unknown(monkeypatch, capsys):
    h = _Harness(_pr(), branch_stdout="")
    _install(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    assert "returned nothing readable" in out
    assert "Default branch: unknown" in out


def test_gh_branch_timing_out_is_unknown_not_green(monkeypatch, capsys):
    h = _Harness(_pr())

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 90)
    _install(monkeypatch, h, argv=["944"])
    monkeypatch.setattr(m.subprocess, "run", boom)
    m.main()
    out = capsys.readouterr().out
    assert "gh-branch did not return" in out
    assert "Default branch: unknown" in out


def test_an_unresolvable_default_branch_is_unknown(monkeypatch, capsys):
    state, lines = m._default_branch_report("", REPO, "d" * 40)
    assert state == m.UNKNOWN
    assert "could not be resolved" in "\n".join(lines)


# ===========================================================================
# plumbing
# ===========================================================================

def test_gh_json_reports_a_missing_binary_rather_than_raising(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(m, "_gh", boom)
    data, err = m._gh_json(["repo", "view"])
    assert data is None and "gh not found" in err


def test_gh_json_reports_a_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("gh", 30)
    monkeypatch.setattr(m, "_gh", boom)
    data, err = m._gh_json(["repo", "view"])
    assert data is None and "timed out" in err


def test_gh_json_reports_invalid_json(monkeypatch):
    monkeypatch.setattr(m, "_gh", lambda a, timeout=30:
                        subprocess.CompletedProcess(a, 0, "not json", ""))
    data, err = m._gh_json(["repo", "view"])
    assert data is None and "invalid JSON" in err


def test_gh_json_surfaces_a_nonzero_exit(monkeypatch):
    monkeypatch.setattr(m, "_gh", lambda a, timeout=30:
                        subprocess.CompletedProcess(a, 1, "", "not found\n"))
    data, err = m._gh_json(["repo", "view"])
    assert data is None and "not found" in err


def test_bound_refs_declines_on_an_unparseable_repo(monkeypatch):
    assert m._bound_refs("944", "") is None


def test_bound_refs_declines_on_a_malformed_payload(monkeypatch):
    monkeypatch.setattr(m, "_gh_json", lambda a, timeout=30: ({"data": {}}, ""))
    assert m._bound_refs("944", REPO) is None


def test_bound_refs_renders_a_cross_repo_number_with_its_slug(monkeypatch):
    monkeypatch.setattr(m, "_gh_json", lambda a, timeout=30: (
        {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
            "nodes": [{"number": 5, "repository": {"nameWithOwner": "o/other"}},
                      {"number": 9, "repository": {"nameWithOwner": REPO}}]}}}}},
        ""))
    assert m._bound_refs("944", REPO) == ["o/other#5", "#9"]


def test_issue_lookup_reports_a_missing_state_field(monkeypatch):
    monkeypatch.setattr(m, "_gh_json", lambda a, timeout=30: ({}, ""))
    state, err = m._issue_lookup(REPO)("#1")
    assert state == "" and "no state" in err


def test_repo_identity_declines_rather_than_inventing_a_repo(monkeypatch):
    monkeypatch.setattr(m, "_gh_json", lambda a, timeout=30: (None, "no auth"))
    repo, branch, err = m._repo_identity()
    assert repo == "" and branch == "" and err == "no auth"


# ===========================================================================
# The head branch is named by whoever opened the PR (#965, #694)
#
# Both halves of that rule reach this op and they are not the same half:
# the `Merge:` header *displays* the ref, so it is flattened; the cleanup
# block *prints a command the reader runs*, so it is quoted. Applying either
# rule at the other site is wrong in a way local tests would not show — a
# flattened ref inside `gh api -X DELETE` is a command that silently targets
# a different branch.
# ===========================================================================

_forged = _load_sibling("test_forged_branch_line_965.py", "forged_965_for_merge")
HOSTILE_BRANCH = _forged.HOSTILE_BRANCH


def test_a_hostile_head_branch_cannot_forge_a_line_in_the_receipt(monkeypatch,
                                                                  capsys):
    """#965's shape, at the op that asserts a merge verdict."""
    h = _Harness(_pr(headRefName=HOSTILE_BRANCH))
    _install(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    _forged.assert_no_forged_line(out)
    _forged.assert_nothing_censored(out, "evil")


@pytest.mark.parametrize("hostile,ids", [
    ("fix/924; rm -rf ~", "shell metacharacter"),
    (HOSTILE_BRANCH, "line separator"),
    ("-B", "option-shaped"),
], ids=["shell", "separator", "option"])
def test_a_hostile_head_branch_gets_no_delete_command_at_all(monkeypatch,
                                                             capsys, hostile,
                                                             ids):
    """The cleanup block is a command the *reader* runs, and neither treatment
    makes this one both correct and safe — so it is declined, not emitted.

    Quoting stops the shell and leaves a U+2028 rendering three lines;
    flattening fixes the render and re-points the delete at a branch that does
    not exist. `_refname` calls this the convenience case, and a convenience
    that might be wrong is worth less than saying so.
    """
    h = _Harness(_pr(headRefName=hostile))
    _install(monkeypatch, h, argv=["944"])
    m.main()
    cleanup = capsys.readouterr().out.split("## Cleanup")[1]
    assert "gh api -X DELETE" not in cleanup, cleanup
    assert "git branch -d" not in cleanup, cleanup
    assert "No delete command is printed" in cleanup, cleanup
    assert "Delete it from the PR page" in cleanup, cleanup
    # Declined, not censored: the reader still gets the name, on one line.
    _forged.assert_no_forged_line(cleanup)
    assert "still exists" in cleanup, cleanup


def test_an_ordinary_head_branch_still_gets_its_commands(monkeypatch, capsys):
    """The common case is untouched — a refusal on every PR is one nobody reads."""
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=["944"])
    m.main()
    cleanup = capsys.readouterr().out.split("## Cleanup")[1]
    assert "git branch -d fix/924" in cleanup, cleanup
    assert "gh api -X DELETE" in cleanup, cleanup
    assert "'fix/924'" not in cleanup, cleanup
    assert "No delete command is printed" not in cleanup, cleanup


# ===========================================================================
# The harness has to be able to *see* a `repo:`-targeted run (#1679)
# ===========================================================================

TARGET = "other/thing"


def test_the_pr_read_is_routed_under_a_repo_target(monkeypatch):
    """`gh_args()` appends `--repo OWNER/NAME` after the `--json` field list.

    The rollup branch used to dispatch on `args[-1]`, which under a target is
    the slug and never the fields — so the PR read fell through to the
    read-back branch, returned the post-merge object and produced a refusal.
    Asserted on the routing itself rather than through `main()`, because the
    thing that broke is which branch of the harness a call lands in.
    """
    h = _Harness(_pr())
    args = ["pr", "view", "944", "--json", "number,statusCheckRollup",
            "--repo", TARGET]
    data, err = h.gh_json(args)
    assert err == "", err
    assert data is h.pr, data
    assert h.readback_count == 0, "the PR read was counted as a read-back"


def test_a_targeted_main_run_reaches_the_merge(monkeypatch, capsys):
    """A green PR under `repo:` merges, and does not refuse.

    No test in this file exercised a targeted `main()` before #1679: every one
    of them ran with no target, so the harness's blindness to `--repo` was
    invisible. Under a target the op used to read the *post-merge* object as
    though it were the PR, see `MERGED`, and refuse — a harness rendering its
    own blindness as a product verdict.
    """
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)
    h = _Harness(_pr())
    _install(monkeypatch, h, argv=["944"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert h.merge_calls, out
    assert "## Gate — passed" in out, out
    assert "ERROR" not in out, out
    # The read-back is a second `pr view`; the gate read is not one of them.
    assert h.readback_count == 1, out
