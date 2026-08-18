"""#1792 — a check run a later run of the same name superseded is not a red leg.

`gh-pr:N:status` counted **every** check run on the head sha. GitHub decides a
required check on the **latest** run carrying that name, so a pull request the
forge reports as `mergeable: true, mergeable_state: clean` rendered here as
`NOT ALL GREEN`, and the maintainer merge gate — told to read exactly this op's
arithmetic — refused it. The stale runs are unretractable: no workflow trigger
withdraws a check run that already concluded, so a PR entering that state can
never satisfy the gate by any action the maintainer takes.

**The collapse is not "latest per name", and that is the whole issue.** GitHub's
default code-scanning setup emits two distinct runs of one workflow per push and
both must pass (#1640) — and, measured against this repository's own commit
`d1bb0837` on 2026-08-18, those two runs emit check runs whose **names
collide**: two `Analyze (javascript-typescript)`, two `Analyze (python)`, in two
different check suites, started in the same second. Latest-per-name silently
drops one of each pair. The counter-case is real and it is in this repo.

So the discriminator here is **timing, not name**:

    a leg is superseded when another leg of the same name STARTED STRICTLY
    AFTER this one COMPLETED.

* the two code-scanning runs overlap in wall clock — neither started after the
  other finished — so both stay live and both must pass;
* the five `fragment` failures of the report completed at 22:23, and the run
  that passed started eight hours later — every one of them is superseded.

It is deliberately **narrower** than GitHub's own rule. Where two same-named
legs overlap in time and one failed, this still says NOT ALL GREEN where the
forge may say clean. That direction is chosen: a loud false alarm is cheap and a
quietly-swallowed failure is not.

Three states, not two (this repository's house defect): passed, failed, and
**failed-but-superseded**. The third used to render as the first — silently. It
now has its own tally term and its own named disclosure line, so a superseded
failure is never invisible; it is only not *blocking*.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent

PRESET = ROOT / "presets" / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr_1792", PRESET)
assert _spec is not None and _spec.loader is not None
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

sys.path.insert(0, str(ROOT / "presets"))
import _checks  # noqa: E402

# gh renders "this timestamp is not set" as a zero time rather than null.
GH_ZERO = "0001-01-01T00:00:00Z"

DETAILS = "https://github.com/o/r/actions/runs/{run}/job/{job}"


def _leg(name: str, conclusion: str, started: str, completed: str,
         job: str = "", run: str = "1") -> dict:
    """One `statusCheckRollup` CheckRun node, in the shape gh returns."""
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion,
        "startedAt": started,
        "completedAt": completed or GH_ZERO,
        "detailsUrl": DETAILS.format(run=run, job=job) if job else "",
    }


# --------------------------------------------------------------------------
# the two fixtures, both measured rather than invented
# --------------------------------------------------------------------------

def _reported() -> list:
    """jbkkz/requivo PR #18 on `7876e6af`, verbatim from #1792.

    Six check runs named `fragment` on one sha: five failures inside seven
    seconds of each other, and one success eight hours later. GitHub calls this
    PR `clean`.
    """
    fails = [
        ("95528525867", "2026-08-17T22:23:27Z", "2026-08-17T22:23:58Z"),
        ("95528526529", "2026-08-17T22:23:30Z", "2026-08-17T22:24:01Z"),
        ("95528529513", "2026-08-17T22:23:32Z", "2026-08-17T22:24:03Z"),
        ("95528530362", "2026-08-17T22:23:32Z", "2026-08-17T22:24:04Z"),
        ("95528531178", "2026-08-17T22:23:33Z", "2026-08-17T22:24:05Z"),
    ]
    legs = [_leg("fragment", "FAILURE", s, c, job=j) for j, s, c in fails]
    legs.append(_leg("fragment", "SUCCESS", "2026-08-18T06:33:20Z",
                     "2026-08-18T06:33:41Z", job="95619563949"))
    return legs


def _code_scanning() -> list:
    """The #1640 counter-case, read off this repo's `d1bb0837` on 2026-08-18.

    Two check suites, one workflow, colliding check-run names, overlapping wall
    clock. `gh api repos/Digital-Process-Tools/claude-supertool/commits/
    d1bb0837/check-runs` is the command that produced these five rows.
    """
    return [
        _leg("Analyze (actions)", "SUCCESS",
             "2026-08-13T22:23:23Z", "2026-08-13T22:24:03Z", job="94612460770"),
        _leg("Analyze (javascript-typescript)", "SUCCESS",
             "2026-08-13T22:23:05Z", "2026-08-13T22:23:43Z", job="94612460254"),
        _leg("Analyze (javascript-typescript)", "SUCCESS",
             "2026-08-13T22:23:05Z", "2026-08-13T22:24:19Z", job="94612460550"),
        _leg("Analyze (python)", "SUCCESS",
             "2026-08-13T22:23:05Z", "2026-08-13T22:25:52Z", job="94612460248"),
        _leg("Analyze (python)", "SUCCESS",
             "2026-08-13T22:23:05Z", "2026-08-13T22:26:13Z", job="94612460601"),
    ]


# --------------------------------------------------------------------------
# the unit: which legs are superseded
# --------------------------------------------------------------------------

def test_the_five_stale_failures_are_superseded() -> None:
    """#1792's measurement, as one assertion."""
    flags = _checks.github_superseded(_reported())
    assert flags == [True, True, True, True, True, False], (
        "the five `fragment` runs that concluded at 22:23 were superseded eight "
        f"hours later by the run that passed, and the last one is live: {flags!r}"
    )


def test_concurrent_runs_of_one_name_are_never_superseded() -> None:
    """#1640's counter-case. Latest-per-name would drop two of these five.

    This is the must-not-fire half of the pair, and it is the reason the
    discriminator is timing rather than name.
    """
    legs = _code_scanning()
    flags = _checks.github_superseded(legs)
    assert flags == [False] * 5, (
        "two runs of one workflow, overlapping in wall clock, with names that "
        "collide — neither started after the other finished, so neither "
        f"supersedes anything: {flags!r}"
    )


def test_a_concurrent_failure_is_still_red() -> None:
    """The positive control for the test above, in the same fixture.

    `[False] * 5` also passes when nothing is ever classified, so the same five
    legs are re-run with one of the colliding pair failed: the tally has to stay
    red. If this and the test above are both green, the classifier looked.
    """
    legs = _code_scanning()
    legs[3]["conclusion"] = "FAILURE"
    line = _checks.summarize_github(legs)
    assert "1 failed" in line and _checks.NOT_GREEN in line, (
        "one of two overlapping same-named legs failed. Collapsing to "
        f"latest-per-name would have hidden it: {line!r}"
    )
    assert "superseded" not in line, (
        f"nothing here was superseded and the line claims otherwise: {line!r}")


@pytest.mark.parametrize("bad", [GH_ZERO, "", None, "not-a-timestamp"],
                         ids=["gh-zero-time", "empty", "null", "garbage"])
def test_a_leg_with_no_completion_stamp_is_never_superseded(bad) -> None:
    """The third state: a leg whose timing cannot be read stays live and loud.

    Both spellings gh uses for "not set" — absent and zero-time — and both must
    fail *toward* counting the leg. Dropping it would be this fix manufacturing
    the silence it exists to remove.
    """
    legs = [
        _leg("build", "FAILURE", "2026-08-17T22:23:27Z", ""),
        _leg("build", "SUCCESS", "2026-08-18T06:33:20Z",
             "2026-08-18T06:33:41Z"),
    ]
    legs[0]["completedAt"] = bad
    flags = _checks.github_superseded(legs)
    assert flags == [False, False], (
        f"completedAt={bad!r} cannot be read, so whether this leg was "
        f"superseded is UNKNOWN and it must stay counted: {flags!r}"
    )
    line = _checks.summarize_github(legs)
    assert "1 failed" in line and _checks.NOT_GREEN in line, line


def test_a_legacy_commit_status_carries_no_timing_and_stays_counted() -> None:
    """A `StatusContext` has no startedAt/completedAt at all."""
    legs = [
        {"__typename": "StatusContext", "context": "ci/external",
         "state": "FAILURE", "targetUrl": "https://ci.example/1"},
        {"__typename": "StatusContext", "context": "ci/external",
         "state": "SUCCESS", "targetUrl": "https://ci.example/2"},
    ]
    assert _checks.github_superseded(legs) == [False, False]
    assert _checks.NOT_GREEN in _checks.summarize_github(legs)


# --------------------------------------------------------------------------
# the tally: three states, and the arithmetic still sums
# --------------------------------------------------------------------------

def test_the_reported_pr_stops_reading_red() -> None:
    line = _checks.summarize_github(_reported())
    assert "0 failed" in line, (
        f"every failure on this sha was superseded eight hours ago: {line!r}")
    assert "5 superseded" in line, (
        f"five legs left the failed count and the line does not say where they "
        f"went: {line!r}")
    assert _checks.NOT_GREEN not in line, (
        f"GitHub calls this PR `clean` and the op still blocks the merge: "
        f"{line!r}")


def test_the_superseded_failure_is_named_not_swallowed() -> None:
    """The must-render half. #1792: the third state must not render as the first."""
    entries = _checks.github_named_superseded(_reported())
    lines = _checks.superseded_disclosure(entries)
    text = "\n".join(lines)
    assert lines, (
        "five legs failed and were dropped out of the red count with no line "
        "saying so — that is the first state wearing the third one's clothes")
    assert "superseded" in text and "failed" in text, text
    assert "fragment" in text, text
    assert "95528525867" in text, (
        f"the reader's next move is `gh-job:<id>:fail` and no id is offered: "
        f"{text!r}")


def test_a_green_pr_grows_no_superseded_term() -> None:
    """The control: nothing superseded, nothing new on the line."""
    line = _checks.summarize_github(_code_scanning())
    assert line == "5 total: 5 passed, 0 failed, 0 pending", line


def test_the_terms_still_sum_to_the_total() -> None:
    """`_checks`' reason to exist: every term after `N total` sums back to N."""
    line = _checks.summarize_github(_reported())
    total = int(re.match(r"(\d+) total:", line).group(1))
    terms = re.findall(r"(\d+) [a-z_;]+", line.split(":", 1)[1])
    assert sum(int(t) for t in terms) == total == 6, (
        f"the terms no longer account for every leg handed in: {line!r}")


def test_every_leg_superseded_is_not_a_green() -> None:
    """Unreachable by construction — a chain always leaves one live leg — but a
    zero-live tally must never render as a pass if it ever becomes reachable."""
    line = _checks.summarize([], superseded=3)
    assert _checks.NOT_GREEN in line, (
        f"nothing live decided anything and the line reads as clean: {line!r}")


# --------------------------------------------------------------------------
# the render, both forms
# --------------------------------------------------------------------------

def _render(monkeypatch, capsys, rollup: list, slim: bool) -> str:
    payload = {
        "number": 1792, "title": "t", "state": "OPEN", "author": {"login": "a"},
        "headRefName": "fix/1792", "baseRefName": "master", "labels": [],
        "milestone": None, "reviewDecision": None, "reviews": [],
        "mergeCommit": None, "mergeable": "MERGEABLE", "isDraft": False,
        "url": "https://github.com/o/r/pull/1792", "body": "", "comments": [],
        "additions": 1, "deletions": 0, "changedFiles": 1, "assignees": [],
        "createdAt": "2026-08-17T22:00:00Z", "updatedAt": "2026-08-18T06:34:00Z",
        "headRefOid": "7" * 40, "statusCheckRollup": rollup,
    }
    monkeypatch.setattr(
        pr, "_gh",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr=""))
    monkeypatch.setattr(pr, "_reconcile_checks", lambda d: ("", []))
    monkeypatch.setattr(pr, "_local_branch_check", lambda s: "")
    monkeypatch.setattr(pr, "_fetch_review_threads_detailed",
                        lambda *a, **k: ([], ""))
    monkeypatch.setattr(sys, "argv",
                        ["pr.py", "1792"] + (["status"] if slim else []))
    assert pr.main() == 0
    return capsys.readouterr().out


def _checks_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("checks: ") or line.startswith("Checks: "):
            return line
    raise AssertionError(f"no checks line in:\n{out}")


@pytest.mark.parametrize("slim", [True, False], ids=["status", "full"])
def test_the_op_no_longer_blocks_the_clean_pr(monkeypatch, capsys, slim) -> None:
    """#1792 end to end, through the op the merge gate is told to read."""
    out = _render(monkeypatch, capsys, _reported(), slim)
    line = _checks_line(out)
    assert _checks.NOT_GREEN not in line, line
    assert "5 superseded" in line, line
    assert "superseded" in out and "95528525867" in out, (
        f"the five failures vanished from the output entirely:\n{out}")
    assert "NOT ALL GREEN" not in out, (
        f"the marker survived somewhere else on the dashboard:\n{out}")


# --------------------------------------------------------------------------
# the gate — the op that actually refused the merge
# --------------------------------------------------------------------------

_MERGE_SPEC = importlib.util.spec_from_file_location(
    "github_pr_merge_1792", ROOT / "presets" / "github" / "pr_merge.py")
assert _MERGE_SPEC is not None and _MERGE_SPEC.loader is not None
pr_merge = importlib.util.module_from_spec(_MERGE_SPEC)
_MERGE_SPEC.loader.exec_module(pr_merge)


def _pr_row(rollup: list) -> dict:
    return {
        "number": 18, "headRefOid": "7" * 40, "statusCheckRollup": rollup,
        "mergeable": "MERGEABLE", "reviewDecision": "", "isDraft": False,
        "state": "OPEN",
    }


def test_the_merge_gate_stops_refusing_the_clean_pr() -> None:
    """#1792's actual cost. `gh-pr:N:status` is what a maintainer reads; this
    is what refuses, and the refusal was unfixable by any action they had."""
    body = " ".join(pr_merge._check_findings(_pr_row(_reported()), None, ()))
    assert "not all green" not in body.lower(), (
        f"the gate still refuses a pull request GitHub calls `clean`: {body!r}")


def test_the_merge_gate_still_refuses_a_live_failure() -> None:
    """The positive control, in the same fixture."""
    legs = _reported()
    legs[-1]["conclusion"] = "FAILURE"
    body = " ".join(pr_merge._check_findings(_pr_row(legs), None, ()))
    assert "REFUSED" in body and "not all green" in body.lower(), (
        f"the live run of `fragment` failed and the gate cleared it: {body!r}")


def test_the_merge_gate_names_the_superseded_legs_on_the_passing_path() -> None:
    """A gate that clears a merge without saying which legs stopped counting is
    the third state rendering as the first, one layer up from the tally."""
    body = " ".join(pr_merge._check_findings(_pr_row(_reported()), None, ()))
    assert "superseded" in body and "95528525867" in body, (
        f"five legs failed, stopped blocking, and left no trace: {body!r}")


def test_every_tally_the_merge_op_prints_comes_from_one_arithmetic() -> None:
    """The gate and the banner it prints must not compute the tally twice.

    `## Gate — passed` used to render `_checks.summarize(github_states(...))`
    — the pre-#1792 shape — immediately before an irreversible merge that
    `_check_findings` had already authorised on the *live* set. The banner then
    read `⚠ NOT ALL GREEN` under a heading saying the gate passed: the
    arithmetic that cleared the merge disagreeing with the arithmetic printed
    beside it, on the last line a reader sees before the merge happens.

    A source check rather than a render, deliberately: `main()` needs a live
    `gh` and the defect is a *second* call site, so what has to be pinned is
    that there is only one.
    """
    src = inspect.getsource(pr_merge)
    stray = [ln.strip() for ln in src.splitlines()
             if "_checks.summarize(" in ln]
    assert not stray, (
        "pr_merge.py renders a tally through `_checks.summarize()`, which does "
        "not know about superseded legs — every tally this op prints has to "
        f"come from `summarize_github()` or the gate contradicts itself: {stray!r}"
    )


@pytest.mark.parametrize("slim", [True, False], ids=["status", "full"])
def test_the_leg_unit_note_follows_the_failed_count(
        monkeypatch, capsys, slim) -> None:
    """#1050's note reads a `failed` count that #1792 changed the meaning of.

    `(those are LEGS ...)` is printed only when something is in the failed
    bucket, and it explains a number on the tally line. Fed the full state set
    it fired next to `0 failed` — a note about a count that is not there, which
    is the second defect on the same line rather than the one this file was
    opened for. Both halves asserted in one place: silent when nothing live
    failed, present when something did.
    """
    note = "those are LEGS"
    clean = _render(monkeypatch, capsys, _reported(), slim)
    assert note not in clean, (
        f"the tally says `0 failed` and a note explains the failed count "
        f"underneath it:\n{clean}")

    legs = _reported()
    legs[-1]["conclusion"] = "FAILURE"
    red = _render(monkeypatch, capsys, legs, slim)
    assert note in red, (
        f"a live leg failed and #1050's unit note went missing with it:\n{red}")


@pytest.mark.parametrize("slim", [True, False], ids=["status", "full"])
def test_a_live_failure_still_blocks(monkeypatch, capsys, slim) -> None:
    """The positive control, same op, same fixture shape.

    Without this, the test above passes just as well against a render that
    stopped printing the marker at all.
    """
    legs = _reported()
    legs[-1]["conclusion"] = "FAILURE"
    line = _checks_line(_render(monkeypatch, capsys, legs, slim))
    assert _checks.NOT_GREEN in line, (
        f"the live run of `fragment` failed and the op cleared the PR: {line!r}")
    assert "1 failed" in line, line
