"""A check run is a second id namespace, and its finding lives in an annotation (#793).

`gh-pr:N:status` names a failing `CodeQL` leg. The natural follow-up,
`gh-job:<id>:fail`, 404s — CodeQL default setup reports through the **checks**
API, not through Actions, so its id is not an Actions job id. The message the
op printed for that 404 asserted:

    Job #92205186236 not found — the job endpoint returned 404 for this ID too,
    so no such job exists in this repo. Check the ID.

which is false in the only case it fires for a check run: the id exists, in a
namespace the op never asked. That is this repo's own defect class in its own
error path — "I could not find it by my route" published as "it is not there".

What is verified against reality and what is not
------------------------------------------------
**Verified live on 2026-08-05** against `Digital-Process-Tools/claude-supertool`
with an authenticated `gh` — the fixtures below were written from the API docs
first and then checked against the real payloads:

  * Check run **92205186236** — the one from the incident — resolves at
    `check-runs/<id>` with `name: CodeQL`, `status: completed`,
    `conclusion: failure`, `app.slug: github-advanced-security`, `head_sha`,
    `html_url`, and an `output.title`/`output.summary` pair. Its single
    annotation is `presets/github/issue.py`, `start_line: 125`,
    `annotation_level: failure`, title `Incomplete URL substring sanitization`
    — the exact line #793 quotes.
  * `actions/jobs/92205186236` 404s for that same id, and `gh-job` now names
    the check run instead of asserting the id does not exist.
  * `commits/<head-sha>/check-runs` on PR 792 returns 18 entries with ids, and
    `gh-pr:792:status` prints its leg names with **no** ids — which is why
    `gh-check:pr:N` exists.
  * A live *running* leg carries `status: in_progress` with an empty
    `conclusion` and zero annotations. That state was not in the first design
    and is what `test_..._on_an_unfinished_check_is_not_a_result` pins.

**One live finding contradicts the issue's framing, and the code says so.** The
two id spaces are not disjoint: GitHub creates a check run per Actions job
*sharing the integer*, so an Actions job id resolves at `check-runs/<id>` too
(verified on job 92249993194). The overlap runs one way only — an App's check
run has no Actions job behind it. So `test_gh_check_on_an_actions_job_id_points_
back_at_gh_job` guards a branch that is **unreachable against today's GitHub**;
it is kept because "check runs are a superset of jobs" is an observation, not a
documented contract, and the branch is what keeps the failure honest if it ever
stops holding.

Still fixture-only, never observed: the 404 response bodies for a nonexistent
check run, the `raw_details` field, `total_count` in the commits envelope, a
100-annotation page, and every transport-failure path (403/502/timeout).

Superseded in part by #827
--------------------------
The `gh-job` half of this file asserted that recognising a check-run id
produced a *message* and never an answer. #827 overturned that — `gh-job:ID`
now routes and renders, under `# Check run #N` with the routing named — on the
grounds that the objection in #821's reasoning is to *silence*, not to
answering, and a labelled header is not silent. The two tests below carry the
amendment inline. Everything else here, including all three of the states that
decline, is unchanged and still load-bearing.

Stubbing note (#731): the fake gh dispatches per endpoint and raises on any
call it does not recognise. That is load-bearing twice here — it is how the
"no extra request on the happy path" claim is enforced, and it is how
"this op never reads the code-scanning API" is enforced.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PRESETS = Path(__file__).parent.parent / "presets"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


job = _load("github/job.py", "github_job_793")


def _check_mod():
    """Loaded per test so the gh-job half of this suite runs before check.py exists."""
    return _load("github/check.py", "github_check_793")


_REAL_RUN = subprocess.run

NO_SUCH_STDERR = "gh: Not Found (HTTP 404)\n"
BLOB_NOT_FOUND_STDOUT = (
    '﻿<?xml version="1.0" encoding="utf-8"?><Error><Code>BlobNotFound</Code>'
    "<Message>The specified blob does not exist.</Message></Error>"
)

CHECK_ID = "92205186236"


def _check_run(**over: Any) -> dict[str, Any]:
    base = {
        "id": int(CHECK_ID),
        "name": "CodeQL",
        "status": "completed",
        "conclusion": "failure",
        "head_sha": "f00dcafe" * 5,
        "html_url": f"https://github.com/o/r/runs/{CHECK_ID}",
        "app": {"slug": "github-advanced-security", "name": "GitHub Advanced Security"},
        "output": {
            "title": "1 new alert including 1 high severity security vulnerability",
            "summary": "**1 new alert** including 1 high severity security vulnerability",
            "annotations_count": 1,
        },
    }
    base.update(over)
    return base


def _annotation(**over: Any) -> dict[str, Any]:
    base = {
        "path": "presets/github/issue.py",
        "start_line": 125,
        "end_line": 125,
        "annotation_level": "failure",
        "title": "Incomplete URL substring sanitization",
        "message": (
            "The string github.com may be at an arbitrary position in the "
            "sanitized URL."
        ),
        "raw_details": None,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# fake gh
# --------------------------------------------------------------------------

def _fake_gh(
    *,
    job_meta: dict[str, Any] | None = None,
    job_rc: int = 1,
    job_stderr: str = NO_SUCH_STDERR,
    check: dict[str, Any] | None = None,
    check_rc: int | None = None,
    check_stderr: str = NO_SUCH_STDERR,
    annotations: list[dict[str, Any]] | None = None,
    ann_rc: int = 0,
    ann_stderr: str = "",
    log_rc: int = 1,
    log_stdout: str = BLOB_NOT_FOUND_STDOUT,
    log_stderr: str = NO_SUCH_STDERR,
    head_sha: str | None = None,
    commit_checks: dict[str, Any] | None = None,
    seen: list[str] | None = None,
):
    """One branch per gh endpoint. Anything else is an AssertionError, not a stub."""
    if check_rc is None:
        check_rc = 0 if check is not None else 1

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if args and args[0] == "git":
            return _REAL_RUN(args, **kw)
        assert args and args[0] == "gh", f"unstubbed command: {args!r}"
        cmd = args[1] if len(args) > 1 else ""
        url = args[2] if len(args) > 2 else ""
        if seen is not None:
            seen.append(f"{cmd} {url}")
        if cmd == "api" and "/check-runs/" in url and url.split("?")[0].endswith("/annotations"):
            return subprocess.CompletedProcess(
                args, ann_rc, json.dumps(annotations or []), ann_stderr)
        if cmd == "api" and "/check-runs/" in url:
            return subprocess.CompletedProcess(
                args, check_rc, json.dumps(check) if check is not None else "",
                check_stderr)
        if cmd == "api" and "/check-runs" in url and "/commits/" in url:
            return subprocess.CompletedProcess(
                args, 0, json.dumps(commit_checks or {"total_count": 0, "check_runs": []}), "")
        if cmd == "api" and url.split("?")[0].endswith("/logs"):
            return subprocess.CompletedProcess(args, log_rc, log_stdout, log_stderr)
        if cmd == "api" and "/actions/jobs/" in url:
            return subprocess.CompletedProcess(
                args, job_rc, json.dumps(job_meta) if job_meta is not None else "",
                job_stderr)
        if cmd == "pr" and url == "view":
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"headRefOid": head_sha or ""}), "")
        if cmd == "run" and url == "view":
            return subprocess.CompletedProcess(
                args, 0,
                json.dumps({"headBranch": "b", "event": "push", "pullRequests": []}), "")
        raise AssertionError(f"unstubbed gh call: {args!r}")

    return fake_run


def _run(mod, monkeypatch, capsys, fake, argv) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(mod.subprocess, "run", fake)
    rc = mod.main()
    return rc, capsys.readouterr().out


# ==========================================================================
# 1. gh-job — the false absence
# ==========================================================================

def test_gh_job_on_a_check_run_id_does_not_claim_the_id_does_not_exist(
        monkeypatch, capsys) -> None:
    """The bug, stated as a test: the id exists, in the other namespace.

    **Amended by #827.** This asserted `rc == 1` — the op recognised the check
    run and printed a signpost to `gh-check`. #827 is the decision that the
    signpost was the wrong deliverable: the user should not have to know
    GitHub keeps CI in two id namespaces, so the op now reads the check run
    and renders it. What this test was actually filed to guard is untouched
    and is what it still asserts — the op must never publish "I could not find
    it by my route" as "it is not there".
    """
    rc, out = _run(job, monkeypatch, capsys,
                   _fake_gh(check=_check_run()), ("job.py", CHECK_ID, "fail"))
    assert rc == 0
    assert "no such job exists in this repo" not in out
    assert "check run" in out.lower()
    assert "CodeQL" in out
    assert f"gh-check:{CHECK_ID}" in out


def test_gh_job_never_renders_check_run_content_under_a_job_header(
        monkeypatch, capsys) -> None:
    """The invariant that survives #827, and the reason routing is safe at all.

    #793's version of this test forbade the *content*; that was the enforcement
    mechanism for the real rule, not the rule. The rule is that an op's header
    must name the API that answered — a `# Job #N` above a check run's body is
    a probe that silently changed which API answered. Rendering the check run
    under `# Check run #N`, with the routing named on the next line, breaks
    nothing that sentence was protecting.
    """
    rc, out = _run(job, monkeypatch, capsys,
                   _fake_gh(check=_check_run(), annotations=[_annotation()]),
                   ("job.py", CHECK_ID, "fail"))
    assert rc == 0
    assert "# Job #" not in out
    assert out.lstrip().startswith(f"# Check run #{CHECK_ID}")


def test_gh_job_on_a_genuinely_unknown_id_still_blames_the_id(
        monkeypatch, capsys) -> None:
    """State three stays distinguishable — and now says both routes were tried."""
    rc, out = _run(job, monkeypatch, capsys,
                   _fake_gh(check=None), ("job.py", "99999999999", "fail"))
    assert rc == 1
    assert "Check the ID" in out
    assert "check run" in out.lower()
    # ...but it must not claim to have *found* one. The message may name CodeQL
    # as the class of thing it looked for; it may not say one is there.
    assert "does exist" not in out


def test_gh_job_declines_when_the_check_probe_itself_could_not_answer(
        monkeypatch, capsys) -> None:
    """A 500/auth failure on the probe is not evidence of absence either way."""
    rc, out = _run(job, monkeypatch, capsys,
                   _fake_gh(check=None, check_rc=1,
                            check_stderr="gh: HTTP 502 Bad Gateway\n"),
                   ("job.py", CHECK_ID, "fail"))
    assert rc == 1
    low = out.lower()
    assert "no such job exists in this repo" not in out
    assert "could not" in low or "unknown" in low
    assert "502" in out


def test_gh_job_happy_path_makes_no_check_runs_call(monkeypatch, capsys) -> None:
    """Zero extra requests on any path but the one that was already wrong."""
    seen: list[str] = []
    fake = _fake_gh(
        job_meta={"name": "pytest", "status": "completed", "conclusion": "failure",
                  "run_id": 1, "completed_at": "2026-08-01T00:00:00Z"},
        job_rc=0, log_rc=0, log_stdout="hello\nERROR: boom\n", log_stderr="",
        seen=seen)
    rc, out = _run(job, monkeypatch, capsys, fake, ("job.py", "123", "fail"))
    assert rc == 0
    assert not any("/check-runs" in s for s in seen), seen


# ==========================================================================
# 2. gh-check — the annotation triple
# ==========================================================================

def test_gh_check_prints_the_annotation_triple(monkeypatch, capsys) -> None:
    check = _check_mod()
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(check=_check_run(), annotations=[_annotation()]),
                   ("check.py", CHECK_ID))
    assert rc == 0
    assert "presets/github/issue.py:125" in out
    assert "Incomplete URL substring sanitization" in out
    assert "arbitrary position in the sanitized URL" in out


def test_gh_check_names_the_namespace_it_read(monkeypatch, capsys) -> None:
    """An op that can be reached with either kind of id must say which answered."""
    check = _check_mod()
    _, out = _run(check, monkeypatch, capsys,
                  _fake_gh(check=_check_run(), annotations=[_annotation()]),
                  ("check.py", CHECK_ID))
    assert "checks API" in out
    assert "Check run" in out


def test_gh_check_caps_many_annotations_and_says_how_many_it_hid(
        monkeypatch, capsys) -> None:
    check = _check_mod()
    many = [_annotation(start_line=n, title=f"Finding {n}") for n in range(1, 13)]
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(check=_check_run(), annotations=many),
                   ("check.py", CHECK_ID))
    assert rc == 0
    assert "12" in out
    assert "+7 more" in out
    assert "Finding 1" in out
    assert "Finding 12" not in out
    # Header AND footer — a reader cut off by the consumer never reaches a footer.
    head, _, tail = out.partition("Finding 1")
    assert "+7 more" in head and "+7 more" in tail


def test_gh_check_zero_annotations_is_not_an_all_clear(monkeypatch, capsys) -> None:
    check = _check_mod()
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(check=_check_run(), annotations=[]),
                   ("check.py", CHECK_ID))
    assert rc == 0
    low = out.lower()
    assert "0 annotations" in low or "no annotations" in low
    assert "not an all-clear" in low or "not a clean bill" in low
    assert "failure" in out


def test_gh_check_zero_annotations_on_an_unfinished_check_is_not_a_result(
        monkeypatch, capsys) -> None:
    """Observed live: a running leg carries `in_progress` and no conclusion.

    Annotations are written *while* a check runs, so 0 there is a reading taken
    mid-flight. Rendering it in the vocabulary of a finished clean check is the
    same all-clear this issue is about, one state further back.
    """
    check = _check_mod()
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(check=_check_run(status="in_progress", conclusion=None,
                                             output={}),
                            annotations=[]),
                   ("check.py", CHECK_ID))
    assert rc == 0
    assert "in_progress" in out
    assert "not finished" in out.lower() or "mid-flight" in out.lower()
    assert "nothing was flagged" not in out.lower()


def test_gh_check_zero_annotations_on_a_passing_check_reads_as_passing(
        monkeypatch, capsys) -> None:
    """The warning above must not fire on a check that actually succeeded."""
    check = _check_mod()
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(check=_check_run(conclusion="success", output={}),
                            annotations=[]),
                   ("check.py", CHECK_ID))
    assert rc == 0
    assert "success" in out
    assert "not an all-clear" not in out.lower()


def test_gh_check_on_an_actions_job_id_points_back_at_gh_job(
        monkeypatch, capsys) -> None:
    check = _check_mod()
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(check=None,
                            job_meta={"name": "pytest", "status": "completed",
                                      "conclusion": "failure", "run_id": 1},
                            job_rc=0),
                   ("check.py", "40123456789"))
    assert rc == 1
    assert "gh-job:40123456789" in out
    assert "Actions job" in out


def test_gh_check_on_an_unknown_id_says_both_namespaces_were_tried(
        monkeypatch, capsys) -> None:
    check = _check_mod()
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(check=None), ("check.py", "99999999999"))
    assert rc == 1
    assert "Check the ID" in out
    assert "Actions job" in out


def test_gh_check_declines_when_the_annotations_call_fails(
        monkeypatch, capsys) -> None:
    """A failed fetch must never render as an empty annotation list."""
    check = _check_mod()
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(check=_check_run(), ann_rc=1,
                            ann_stderr="gh: HTTP 403 Forbidden\n"),
                   ("check.py", CHECK_ID))
    assert rc == 1
    assert "403" in out
    assert "0 annotations" not in out.lower()


def test_gh_check_never_reads_the_code_scanning_api(monkeypatch, capsys) -> None:
    """#793's empty `code-scanning/alerts?ref=…` must not be renderable here."""
    check = _check_mod()
    seen: list[str] = []
    _run(check, monkeypatch, capsys,
         _fake_gh(check=_check_run(), annotations=[_annotation()], seen=seen),
         ("check.py", CHECK_ID))
    assert not any("code-scanning" in s for s in seen), seen


def test_gh_check_full_page_of_annotations_discloses_it_read_one_page(
        monkeypatch, capsys) -> None:
    check = _check_mod()
    page = [_annotation(start_line=n) for n in range(1, 101)]
    _, out = _run(check, monkeypatch, capsys,
                  _fake_gh(check=_check_run(), annotations=page),
                  ("check.py", CHECK_ID))
    low = out.lower()
    assert "first page" in low or "one page" in low
    assert "100" in out


# ==========================================================================
# 3. gh-check:pr:N — getting from the *name* to an id
# ==========================================================================

def test_gh_check_pr_lists_check_runs_with_their_ids(monkeypatch, capsys) -> None:
    check = _check_mod()
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(head_sha="abc123",
                            commit_checks={"total_count": 2, "check_runs": [
                                _check_run(),
                                _check_run(id=1, name="tests", conclusion="success"),
                            ]}),
                   ("check.py", "pr", "792"))
    assert rc == 0
    assert "CodeQL" in out
    assert CHECK_ID in out
    assert "gh-check:" in out


def test_gh_check_pr_with_no_check_runs_does_not_read_as_no_checks(
        monkeypatch, capsys) -> None:
    """An empty list here means "none on the head commit", not "none anywhere"."""
    check = _check_mod()
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(head_sha="abc123",
                            commit_checks={"total_count": 0, "check_runs": []}),
                   ("check.py", "pr", "792"))
    assert rc == 0
    low = out.lower()
    assert "head" in low
    assert "abc123" in out
    assert "0 check runs" in low or "no check runs" in low


def test_gh_check_pr_without_a_head_sha_declines(monkeypatch, capsys) -> None:
    check = _check_mod()
    rc, out = _run(check, monkeypatch, capsys,
                   _fake_gh(head_sha=""), ("check.py", "pr", "792"))
    assert rc == 1
    assert "0 check runs" not in out.lower()


def test_gh_check_usage_error_names_both_forms(monkeypatch, capsys) -> None:
    check = _check_mod()
    monkeypatch.setattr(sys, "argv", ["check.py"])
    rc = check.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "pr:" in out


@pytest.mark.parametrize("bad", ["abc", "12x", "-5"])
def test_gh_check_rejects_a_non_numeric_id_before_calling_gh(
        monkeypatch, capsys, bad: str) -> None:
    check = _check_mod()
    seen: list[str] = []
    rc, out = _run(check, monkeypatch, capsys, _fake_gh(seen=seen),
                   ("check.py", bad))
    assert rc == 1
    assert seen == []
