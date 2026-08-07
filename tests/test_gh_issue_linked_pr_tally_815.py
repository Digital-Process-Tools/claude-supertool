"""`gh-issue` must say whether the linked PR is green (#815).

`#808 (OPEN)` was the whole story the op told about a PR that had a failed leg
at that moment. `OPEN` is true, and a triage reader concludes "the fix is in
flight" and moves on; the fix was in flight *and red*, which is a different
decision — one says wait, the other says go look.

**The cost question the issue asks to be answered explicitly.** It is not one
extra API call per linked PR. `closedByPullRequestsReferences` is already
fetched over GraphQL, and `commits(last:1) { commit { statusCheckRollup } }`
hangs off the same selection set, so the tally arrives in the request the op
already makes. Measured against the live API on 2026-08-07 (issues 1007, 803,
969: 20, 18 and 20 contexts respectively, one request each). That is why it is
default-on rather than behind `:full` — there is no per-PR cost to gate.

Three states, per #815 judgment call 2 and `docs/validators.md` §"Declining
instead of guessing":

* a PR whose head commit has **no run at all** must not render like one whose
  run is pending — this tracker has already had a PR whose workflow never
  triggered read as "not yet" for its entire first life;
* a tally the response did **not carry** must say UNKNOWN, because an omitted
  tally reads as "nothing to report", which is the reading that is wrong.

The arithmetic itself is not recomputed here: `_checks.summarize` is the same
function `gh-pr:N:status` renders, so the two ops cannot drift (#445/#454).
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "github" / "issue.py"
_spec = importlib.util.spec_from_file_location("github_issue_815", PRESET)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)

WEB_URL = "https://github.com/Digital-Process-Tools/claude-supertool/issues/803"


def _leg(name: str, status: str, conclusion: str) -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion}


def _pr_node(rollup: object, *, with_commits: bool = True) -> dict:
    node = {"number": 808, "title": "fix(gh-run)", "state": "OPEN",
            "headRefName": "fix/803-run-red-state-predicate"}
    if with_commits:
        node["commits"] = {"nodes": [{"commit": {"statusCheckRollup": rollup}}]}
    return node


def _linked_section(monkeypatch, gh_behaviour) -> str:
    """Render just the linked-PR section, with `gh api graphql` stubbed."""
    monkeypatch.setattr(issue, "_gh", gh_behaviour)
    buf = io.StringIO()
    with redirect_stdout(buf):
        issue._print_linked_prs(803, WEB_URL)
    return buf.getvalue()


def _render(monkeypatch, nodes: list[dict]) -> str:
    payload = json.dumps({"data": {"repository": {"issue": {
        "closedByPullRequestsReferences": {"nodes": nodes}}}}})
    return _linked_section(
        monkeypatch,
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )


RED_ROLLUP = {"contexts": {
    "totalCount": 18,
    "nodes": (
        [_leg(f"p{i}", "COMPLETED", "SUCCESS") for i in range(13)]
        + [_leg("pytest (windows-latest, 3.9)", "COMPLETED", "FAILURE")]
        + [_leg(f"q{i}", "QUEUED", "") for i in range(4)]
    ),
}}


def test_the_query_asks_for_the_rollup(monkeypatch) -> None:
    """The tally has to be *requested*, or every state below is UNKNOWN."""
    query = issue._closing_prs_query("o", "r", 803)
    assert "statusCheckRollup" in query, (
        "the linked-PR query does not ask for the check rollup, so the tally "
        f"can only come from a second call:\n{query}")
    assert "commits(last: 1" in query.replace("last:1", "last: 1"), query


def test_a_red_linked_pr_says_so(monkeypatch) -> None:
    """#815's worked example, verbatim."""
    out = _render(monkeypatch, [_pr_node(RED_ROLLUP)])
    assert "18 total: 13 passed, 1 failed, 4 pending" in out, (
        f"the tally the sibling op already computes is absent:\n{out}")
    assert "NOT ALL GREEN" in out, out


def test_a_green_linked_pr_is_distinguishable_from_a_red_one(monkeypatch) -> None:
    green = {"contexts": {"totalCount": 2, "nodes": [
        _leg("a", "COMPLETED", "SUCCESS"), _leg("b", "COMPLETED", "SUCCESS")]}}
    red = _render(monkeypatch, [_pr_node(RED_ROLLUP)])
    ok = _render(monkeypatch, [_pr_node(green)])
    assert red != ok, f"red and green linked PRs render identically:\n{ok}"
    assert "NOT ALL GREEN" not in ok, ok


def test_no_run_at_all_is_not_rendered_as_pending(monkeypatch) -> None:
    """`statusCheckRollup: null` — the PR whose workflow never triggered."""
    out = _render(monkeypatch, [_pr_node(None)])
    assert "pending" not in out.lower(), (
        f"a PR with no run at all is being rendered as one that is waiting:\n{out}")
    assert "no check runs" in out.lower(), out
    # Not a bare zero either: `0 passed, 0 failed, 0 pending` reads as
    # "accounted for, nothing outstanding" — #452's exact wrong reading.
    assert "0 passed, 0 failed, 0 pending" not in out, out


def test_a_tally_the_response_did_not_carry_says_unknown(monkeypatch) -> None:
    """The `commits` subtree absent — a partial GraphQL result, or an older schema."""
    out = _render(monkeypatch, [_pr_node(None, with_commits=False)])
    assert "UNKNOWN" in out, (
        "the tally could not be read and the line omits it, which reads as "
        f"'nothing to report':\n{out}")
    assert "no check runs" not in out.lower(), (
        f"'no runs' is a claim about the world; nothing here established it:\n{out}")


def test_a_truncated_context_list_is_disclosed(monkeypatch) -> None:
    """100 legs is the page size, not necessarily the leg count."""
    big = {"contexts": {"totalCount": 137, "nodes": [
        _leg(f"p{i}", "COMPLETED", "SUCCESS") for i in range(100)]}}
    out = _render(monkeypatch, [_pr_node(big)])
    assert "137" in out, (
        f"100 of 137 legs were read and the tally claims to be complete:\n{out}")


def test_the_existing_lines_are_untouched(monkeypatch) -> None:
    """The control: state, title and branch still render as before."""
    out = _render(monkeypatch, [_pr_node(RED_ROLLUP)])
    assert "#808 (OPEN) fix(gh-run)" in out, out
    assert "    branch: fix/803-run-red-state-predicate" in out, out


@pytest.mark.parametrize("boom", [
    FileNotFoundError(2, "No such file or directory: 'gh'"),
    PermissionError(13, "Permission denied: 'gh'"),
], ids=["gh-not-on-path", "gh-not-executable"])
def test_a_spawn_failure_says_unknown_rather_than_raising(
        monkeypatch, boom) -> None:
    """Found while auditing this change for Windows, not by a failing CI leg.

    `main()` guards its own `gh` call with `except FileNotFoundError`; this
    function did not, so on a machine without `gh` on PATH the section whose
    entire job is to say "I could not ask" raised instead. Windows produces
    `FileNotFoundError [WinError 2]` from `subprocess.run` where a POSIX shell
    may resolve differently, and `PermissionError` where POSIX raises
    `IsADirectoryError` — hence `OSError`, not one spelling (#997, #618/#627).
    """
    def _raise(*a, **k):
        raise boom

    out = _linked_section(monkeypatch, _raise)
    assert "unknown" in out.lower(), out
    assert "none" not in out.lower(), out


def test_zero_linked_prs_still_says_none(monkeypatch) -> None:
    """The control #780 left behind: an answered zero keeps its own word."""
    out = _render(monkeypatch, [])
    assert "Linked PRs: none" in out, out
    assert "UNKNOWN" not in out, out
