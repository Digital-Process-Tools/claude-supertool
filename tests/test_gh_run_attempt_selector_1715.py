"""#1715 — a re-run buries the evidence, and no op could reach the attempt it is in.

`gh-branch` collapses to the highest `run_attempt` by design and says so; that
is what makes the merge gate honest. The consequence is that once a flake is
re-run green, the only record of *why* it was red lives in an attempt no op
reached. Recovering #1709's diagnosis needed
`gh api repos/O/R/actions/runs/31815095925/attempts/1/jobs` — leaving supertool
to get the job ids `gh-job` was then perfectly able to read.

Two properties are pinned here, and the second is why this was not mechanical.

* **A prior attempt's job list renders**, through `gh-run:ID:attempt=K`, with
  the same table and the same `job #<id>` cells the default render mints — so
  the ids reach `gh-job` without a raw call.
* **A prior attempt is HISTORICAL and the render says so.** Three states, not
  two (`docs/validators.md`, "Declining instead of guessing"): this attempt is
  the current one, a later attempt exists and supersedes it, or the run carried
  no readable `run_attempt` and whether it was re-run is *unread*. A stale red
  that does not announce itself as stale is read as the state of the run now.

The default render is unchanged in what it selects — latest, always — and gains
one line, because the absence of the earlier attempts' legs from that table is
exactly this repository's own defect: an absence produced by the tool, read as
an absence in the world.

**Nothing caller-typed reaches the argv.** The run id is digit-gated before
anything is fetched (`gh-job`'s #1145 guard, one op over), and the attempt is
matched against a digits-only pattern anchored at end-of-string, converted with
`int()` and re-rendered — so what `gh run view --attempt` receives is the tool's
own integer, never the caller's string. An attempt past the latest is refused by
name rather than bought as a 404.

The bar: would any of this pass if the code did nothing? Every assertion below
either names a job id that exists only in the superseded attempt, counts the
subprocess calls a refusal must NOT make, or reads a line the old render never
printed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


run = _load("presets/github/run.py", "github_run_1715")

RUN_ID = "31815095925"


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _job(name: str, ident: int, conclusion: str | None = "success") -> dict:
    return {"name": name, "status": "completed", "conclusion": conclusion,
            "databaseId": ident, "steps": []}


def _payload(attempt: object, jobs: list, conclusion: str | None = "success") -> dict:
    return {
        "databaseId": int(RUN_ID), "name": "tests", "status": "completed",
        "conclusion": conclusion, "event": "push", "headBranch": "fix/1709",
        "attempt": attempt,
        "url": f"https://github.com/o/r/actions/runs/{RUN_ID}",
        "jobs": jobs,
    }


#: attempt 1 went red and was re-run; attempt 2 is green. The two job lists are
#: disjoint in their ids on purpose — an assertion naming 111 cannot be
#: satisfied by a render that reached for the latest attempt.
ATTEMPT_1 = _payload(1, [_job("html-check", 111, "failure")], conclusion="failure")
ATTEMPT_2 = _payload(2, [_job("html-check", 222)])


class _Gh:
    """Fakes `gh run view`, keyed on the `--attempt` flag. Records every argv."""

    def __init__(self, by_attempt: dict) -> None:
        self.by_attempt = by_attempt
        self.calls: list[list[str]] = []

    def __call__(self, argv, *a, **kw):
        argv = [str(x) for x in argv]
        if argv[:3] == ["gh", "run", "view"]:
            self.calls.append(argv)
            k = None
            if "--attempt" in argv:
                k = argv[argv.index("--attempt") + 1]
            payload = self.by_attempt.get(k)
            if payload is None:
                return _Completed(
                    "", returncode=1,
                    stderr="failed to get run: HTTP 404: Not Found")
            return _Completed(json.dumps(payload))
        if argv[:2] == ["gh", "api"]:
            self.calls.append(argv)
            return _Completed(json.dumps({"jobs": []}))
        # git, and anything else `_branch_locale` reaches for.
        return _Completed("")


def _run(monkeypatch, capsys, tokens: list[str],
         by_attempt: dict | None = None) -> tuple[int, str, _Gh]:
    gh = _Gh(by_attempt if by_attempt is not None
             else {None: ATTEMPT_2, "1": ATTEMPT_1, "2": ATTEMPT_2})
    monkeypatch.setattr(run.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", ["run.py"] + tokens)
    code = run.main()
    return code, capsys.readouterr().out, gh


# ---------------------------------------------------------------------------
# the gap: a prior attempt's job ids
# ---------------------------------------------------------------------------

def test_a_prior_attempts_jobs_are_reachable(monkeypatch, capsys) -> None:
    """#1715 verbatim — the ids that only exist in the superseded attempt."""
    code, out, _ = _run(monkeypatch, capsys, [RUN_ID, "attempt=1"])

    assert code == 0, out
    assert "job #111" in out, (
        "attempt 1's job ids are still unreachable, so the route to gh-job is "
        "still a raw gh api call:\n" + out)
    assert "job #222" not in out, (
        "the render reached for the latest attempt while attempt 1 was "
        "asked for:\n" + out)


def test_the_attempt_is_named_in_the_header(monkeypatch, capsys) -> None:
    _, out, _ = _run(monkeypatch, capsys, [RUN_ID, "attempt=1"])
    head = out.splitlines()[0]
    assert "attempt 1 of 2" in head, (
        "a render quoted on its own says nothing about which attempt it "
        "is:\n" + head)


def test_a_superseded_attempt_says_it_is_historical(monkeypatch, capsys) -> None:
    _, out, _ = _run(monkeypatch, capsys, [RUN_ID, "attempt=1"])
    assert "HISTORICAL" in out, (
        "a stale red renders exactly like the state of the run now:\n" + out)
    assert f"gh-run:{RUN_ID}" in out, (
        "nothing in the render says how to read the current attempt:\n" + out)


# ---------------------------------------------------------------------------
# the default render — unchanged in what it selects, plus one disclosure
# ---------------------------------------------------------------------------

def test_the_default_render_still_selects_the_latest_attempt(
        monkeypatch, capsys) -> None:
    code, out, gh = _run(monkeypatch, capsys, [RUN_ID])
    assert code == 0, out
    assert "job #222" in out and "job #111" not in out, out
    assert all("--attempt" not in c for c in gh.calls), gh.calls


def test_the_default_render_discloses_the_attempts_it_is_not_showing(
        monkeypatch, capsys) -> None:
    """The absence of attempt 1's legs must not read as an absence of legs."""
    _, out, _ = _run(monkeypatch, capsys, [RUN_ID])
    assert "Attempts: 2 of 2" in out, out
    assert f"gh-run:{RUN_ID}:attempt=1" in out, (
        "the table omits an entire attempt and offers no way to ask for "
        "it:\n" + out)


def test_a_run_that_was_never_re_run_says_so(monkeypatch, capsys) -> None:
    """Distinct from `2 of 2`: there is no earlier attempt at all here."""
    _, out, _ = _run(monkeypatch, capsys, [RUN_ID],
                     {None: _payload(1, [_job("html-check", 111)])})
    assert "never re-run" in out, out
    assert ":attempt=" not in out, (
        "a run with one attempt points at a selector with nothing to "
        "select:\n" + out)


def test_an_unreadable_attempt_field_is_unknown_not_never_re_run(
        monkeypatch, capsys) -> None:
    """The third state. A field nobody read must not answer the question."""
    _, out, _ = _run(monkeypatch, capsys, [RUN_ID],
                     {None: _payload(None, [_job("html-check", 111)])})
    assert "Attempts: UNKNOWN" in out, out
    assert "never re-run" not in out, (
        "an unread field was rendered as an established fact about the "
        "run:\n" + out)


def test_asking_for_the_latest_attempt_buys_no_second_call(
        monkeypatch, capsys) -> None:
    """The payload in hand already IS attempt 2 — re-fetching it is a wasted call."""
    code, out, gh = _run(monkeypatch, capsys, [RUN_ID, "attempt=2"])
    assert code == 0, out
    assert "Attempts: 2 of 2" in out, out
    assert "HISTORICAL" not in out, out
    assert [c for c in gh.calls if "--attempt" in c] == [], gh.calls


# ---------------------------------------------------------------------------
# what reaches the argv
# ---------------------------------------------------------------------------

def test_only_the_tools_own_integer_reaches_the_argv(monkeypatch, capsys) -> None:
    """`01` is a valid attempt and is re-rendered, never relayed."""
    code, out, gh = _run(monkeypatch, capsys, [RUN_ID, "attempt=01"])
    assert code == 0, out
    fetch = [c for c in gh.calls if "--attempt" in c]
    assert len(fetch) == 1, gh.calls
    assert fetch[0][fetch[0].index("--attempt") + 1] == "1", fetch[0]


def test_a_non_numeric_attempt_is_refused_before_anything_is_fetched(
        monkeypatch, capsys) -> None:
    code, out, gh = _run(monkeypatch, capsys, [RUN_ID, "attempt=1;rm"])
    assert code == 1, out
    assert gh.calls == [], (
        "a mangled attempt reached a subprocess:\n" + repr(gh.calls))
    assert "attempt" in out.lower(), out


def test_attempt_zero_is_refused(monkeypatch, capsys) -> None:
    """GitHub numbers attempts from 1; 0 is a mangled token, not a request."""
    code, out, gh = _run(monkeypatch, capsys, [RUN_ID, "attempt=0"])
    assert code == 1, out
    assert gh.calls == [], gh.calls


def test_an_attempt_past_the_latest_is_refused_by_name(
        monkeypatch, capsys) -> None:
    """Named, not bought as a 404 — the op already holds the real count."""
    code, out, gh = _run(monkeypatch, capsys, [RUN_ID, "attempt=3"])
    assert code == 1, out
    assert "2" in out, out
    assert [c for c in gh.calls if "--attempt" in c] == [], (
        "a second call was spent on an attempt the first call proved does not "
        "exist:\n" + repr(gh.calls))


def test_a_non_numeric_run_id_is_refused_before_anything_is_fetched(
        monkeypatch, capsys) -> None:
    """`gh-job`'s #1145 guard, one op over — and now it gates a fetch argv."""
    code, out, gh = _run(monkeypatch, capsys, ["31815095925ep"])
    assert code == 1, out
    assert gh.calls == [], (
        "a mangled run id reached a subprocess:\n" + repr(gh.calls))


def test_an_unrecognised_token_is_refused_rather_than_dropped(
        monkeypatch, capsys) -> None:
    """Core stopped refusing this the moment the cmd widened to {args} (#873)."""
    code, out, gh = _run(monkeypatch, capsys, [RUN_ID, "verbose"])
    assert code == 1, out
    assert gh.calls == [], gh.calls
    assert "attempt=" in out, out


# ---------------------------------------------------------------------------
# absences stay absences
# ---------------------------------------------------------------------------

def test_an_unreadable_attempt_fetch_is_refused_not_half_rendered(
        monkeypatch, capsys) -> None:
    code, out, _ = _run(monkeypatch, capsys, [RUN_ID, "attempt=1"],
                        {None: ATTEMPT_2})
    assert code == 1, out
    assert "job #222" not in out, (
        "the attempt fetch failed and the latest attempt was rendered under "
        "the heading of the one that was asked for:\n" + out)


def test_a_superseded_attempt_is_not_reconciled_against_filter_all(
        monkeypatch, capsys) -> None:
    """`filter=all` counts every attempt's rows, so it is not this tally's peer.

    Reconciling one attempt's legs against the union across all of them
    manufactures a shortfall, which is how a real disclosure gets ignored.
    """
    _, out, gh = _run(monkeypatch, capsys, [RUN_ID, "attempt=1"])
    assert [c for c in gh.calls if c[:2] == ["gh", "api"]] == [], (
        "the historical render bought a second leg count that cannot agree "
        "with it:\n" + repr(gh.calls))
