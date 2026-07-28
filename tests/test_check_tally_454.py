"""#454 — the check tally must account for every check it was handed.

The old tally knew three buckets (SUCCESS / FAILURE / in-progress) and
silently discarded everything else, so a run that had concluded `failure`
with two CANCELLED legs printed `10 passed, 0 failed, 0 pending` — a line
that reads as "everything is accounted for and nothing is outstanding".

These tests assert on the *emitted text*, never on a returned count: a test
asserting `passed == 10` passes on the broken code.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PRESETS = Path(__file__).parent.parent / "presets"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load("github_pr_454", PRESETS / "github" / "pr.py")
checks_mod = _load("supertool_checks_454", PRESETS / "_checks.py")


def _fake_run(stdout: str, returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=""
    )


def _payload(rollup: list[dict], **overrides: Any) -> str:
    base = {
        "number": 452,
        "title": "fix: something",
        "state": "OPEN",
        "author": {"login": "max"},
        "headRefName": "fix/454",
        "baseRefName": "master",
        "labels": [],
        "milestone": None,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "reviews": [],
        "mergeCommit": None,
        "additions": 10,
        "deletions": 1,
        "changedFiles": 2,
        "statusCheckRollup": rollup,
        "url": "https://github.com/foo/bar/pull/452",
        "body": "",
        "comments": [],
    }
    base.update(overrides)
    return json.dumps(base)


def _run_pr(monkeypatch, capsys, rollup: list[dict], slim: bool = True, **ov) -> str:
    payload = _payload(rollup, **ov)
    monkeypatch.setattr(
        pr.subprocess, "run", lambda *a, **kw: _fake_run(payload, returncode=0)
    )
    argv = ["pr.py", "452"] + (["status"] if slim else [])
    monkeypatch.setattr(sys, "argv", argv)
    assert pr.main() == 0
    return capsys.readouterr().out


def _checks_line(out: str) -> str:
    for line in out.splitlines():
        if line.lower().startswith("checks:"):
            return line
    raise AssertionError(f"no checks line in output:\n{out}")


_TERM_RE = re.compile(r"(\d+) ([a-z][a-z0-9_ ]*?)(?=,|$| ⚠)")


def _parse_tally(line: str) -> tuple[int, dict[str, int]]:
    """Pull `N total` and every `k <label>` term out of an emitted checks line."""
    body = line.split(":", 1)[1].strip()
    m = re.match(r"(\d+) total[:—-]\s*(.*)", body)
    assert m, f"checks line does not declare a total: {line!r}"
    total = int(m.group(1))
    terms: dict[str, int] = {}
    for count, label in _TERM_RE.findall(m.group(2)):
        terms[label.strip()] = terms.get(label.strip(), 0) + int(count)
    return total, terms


def _success(n: int) -> list[dict]:
    return [
        {"name": f"pytest-{i}", "status": "COMPLETED", "conclusion": "SUCCESS"}
        for i in range(n)
    ]


# --- Pin 1: a CANCELLED leg must be named and must not read as all-clear ----

def test_cancelled_legs_are_named_and_counted_slim(monkeypatch, capsys) -> None:
    """The exact PR #452 rollup: 10 SUCCESS + 2 CANCELLED on a failed run."""
    rollup = _success(10) + [
        {"name": "pytest (macos, 3.9)", "status": "COMPLETED", "conclusion": "CANCELLED"},
        {"name": "pytest (macos, 3.12)", "status": "COMPLETED", "conclusion": "CANCELLED"},
    ]
    line = _checks_line(_run_pr(monkeypatch, capsys, rollup))
    assert "cancelled" in line.lower(), line
    total, terms = _parse_tally(line)
    assert total == 12, line
    assert sum(terms.values()) == 12, line
    assert terms.get("passed") == 10, line
    # The old line — the whole bug — must be gone.
    assert "10 passed, 0 failed, 0 pending" != line.split(":", 1)[1].strip()
    assert checks_mod.NOT_GREEN in line, line


def test_cancelled_legs_are_named_in_full_mode(monkeypatch, capsys) -> None:
    rollup = _success(10) + [
        {"name": "pytest (macos, 3.9)", "status": "COMPLETED", "conclusion": "CANCELLED"},
        {"name": "pytest (macos, 3.12)", "status": "COMPLETED", "conclusion": "CANCELLED"},
    ]
    out = _run_pr(monkeypatch, capsys, rollup, slim=False)
    line = _checks_line(out)
    assert "cancelled" in line.lower(), line
    total, terms = _parse_tally(line)
    assert sum(terms.values()) == total == 12, line


def test_mergeable_is_qualified_when_checks_are_not_all_green(monkeypatch, capsys) -> None:
    """`Mergeable: yes` must not sit unqualified beside a non-green run."""
    rollup = _success(10) + [
        {"name": "pytest", "status": "COMPLETED", "conclusion": "CANCELLED"},
    ]
    out = _run_pr(monkeypatch, capsys, rollup, slim=False)
    merge_line = next(l for l in out.splitlines() if l.startswith("Mergeable:"))
    assert merge_line.strip() != "Mergeable: yes", merge_line
    assert "conflict" in merge_line.lower(), merge_line
    assert "check" in merge_line.lower(), merge_line


def test_mergeable_stays_plain_when_everything_passed(monkeypatch, capsys) -> None:
    out = _run_pr(monkeypatch, capsys, _success(3), slim=False)
    merge_line = next(l for l in out.splitlines() if l.startswith("Mergeable:"))
    assert "NOT" not in merge_line.upper(), merge_line


# --- Pin 2: re-queued checks must not look like a repo with no CI ----------

def test_requeued_checks_are_distinguishable_from_no_ci(monkeypatch, capsys) -> None:
    """`gh run rerun --failed` → 12 QUEUED legs. Must not print 0/0/0."""
    queued = [
        {"name": f"pytest-{i}", "status": "QUEUED", "conclusion": None}
        for i in range(12)
    ]
    requeued_line = _checks_line(_run_pr(monkeypatch, capsys, queued))
    none_line = _checks_line(_run_pr(monkeypatch, capsys, []))

    assert requeued_line != none_line
    total, terms = _parse_tally(requeued_line)
    assert total == 12, requeued_line
    assert sum(terms.values()) == 12, requeued_line
    assert terms.get("pending") == 12, requeued_line


def test_no_checks_says_so_in_words(monkeypatch, capsys) -> None:
    """A PR genuinely without CI prints prose, never a zeroed tally."""
    line = _checks_line(_run_pr(monkeypatch, capsys, []))
    assert "none" in line.lower(), line
    assert "0 passed" not in line, line
    assert "total" not in line.lower(), line


def test_no_checks_full_mode_says_so_in_words(monkeypatch, capsys) -> None:
    line = _checks_line(_run_pr(monkeypatch, capsys, [], slim=False))
    assert "none" in line.lower(), line
    assert "0 passed" not in line, line


# --- Pin 3: the invariant — printed counts sum to the checks present ------

# Every state either platform can hand us, plus one that does not exist yet.
_STATES = [
    "SUCCESS", "FAILURE", "CANCELLED", "TIMED_OUT", "SKIPPED", "NEUTRAL",
    "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE", "QUEUED", "IN_PROGRESS",
    "WAITING", "REQUESTED", "PENDING", "EXPECTED", "MANUAL", "CREATED",
    "SOME_STATE_GITHUB_ADDS_IN_2027",
]


@pytest.mark.parametrize("state", _STATES)
def test_counts_sum_to_checks_present(monkeypatch, capsys, state: str) -> None:
    """For N checks in any mix of states, the printed counts sum to N."""
    rollup = _success(3) + [
        {"name": f"leg-{i}", "status": "COMPLETED", "conclusion": state}
        for i in range(2)
    ]
    line = _checks_line(_run_pr(monkeypatch, capsys, rollup))
    total, terms = _parse_tally(line)
    assert total == 5, line
    assert sum(terms.values()) == 5, f"{state} vanished from the tally: {line!r}"


@pytest.mark.parametrize("state", _STATES)
def test_only_all_success_reads_as_green(monkeypatch, capsys, state: str) -> None:
    """Any non-SUCCESS state must carry the not-green marker."""
    rollup = _success(3) + [{"name": "leg", "status": "COMPLETED", "conclusion": state}]
    line = _checks_line(_run_pr(monkeypatch, capsys, rollup))
    if state == "SUCCESS":
        assert checks_mod.NOT_GREEN not in line, line
    else:
        assert checks_mod.NOT_GREEN in line, line


def test_state_carried_only_on_status_field_is_still_counted(monkeypatch, capsys) -> None:
    """Legacy commit statuses carry `state`, not `conclusion`/`status`."""
    rollup = _success(2) + [{"context": "ci/legacy", "state": "ERROR"}]
    line = _checks_line(_run_pr(monkeypatch, capsys, rollup))
    total, terms = _parse_tally(line)
    assert sum(terms.values()) == total == 3, line


def test_state_free_entry_is_counted_as_unknown(monkeypatch, capsys) -> None:
    """A rollup entry with no usable state must still be visible, not dropped."""
    rollup = _success(2) + [{"name": "mystery"}]
    line = _checks_line(_run_pr(monkeypatch, capsys, rollup))
    total, terms = _parse_tally(line)
    assert sum(terms.values()) == total == 3, line
    assert "unknown" in line.lower(), line


# --- Boards: a cancelled pipeline must be reachable by the failing filter --

mrs = _load("gitlab_mrs_454", PRESETS / "gitlab" / "mrs.py")
prs = _load("github_prs_454", PRESETS / "github" / "prs.py")


def _mr(iid: int, pipeline: str) -> dict:
    return {
        "iid": iid,
        "title": f"MR {iid}",
        "source_branch": "feat",
        "target_branch": "master",
        "updated_at": "2026-07-28T10:00:00Z",
        "_pipeline": pipeline,
        "_failed_jobs": [],
        "_approved": True,
        "_changes": 10,
    }


@pytest.mark.parametrize("status", ["failed", "canceled"])
def test_gl_mrs_failing_filter_and_footer_see_red_pipelines(status: str) -> None:
    """`gl-mrs:failed` must surface a canceled pipeline, not only `failed`."""
    board = [_mr(1, status), _mr(2, "success")]
    kept = [m for m in board if mrs._is_failing(m)]
    assert [m["iid"] for m in kept] == [1], status
    footer = mrs._footer(board, set(), True)
    assert "1 failing" in footer, footer


@pytest.mark.parametrize("status", ["failed", "canceled"])
def test_gl_mrs_sorts_red_pipelines_first(status: str) -> None:
    board = [_mr(1, "success"), _mr(2, status)]
    rendered = mrs._render_table(board, set(), True)
    first = rendered.splitlines()[0]
    assert "!2" in first, rendered


def test_gh_prs_failing_filter_sees_cancelled_checks() -> None:
    """The GitHub board already treats CANCELLED as red — keep it that way."""
    p = {"statusCheckRollup": [
        {"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "b", "status": "COMPLETED", "conclusion": "CANCELLED"},
    ]}
    prs._annotate([p])
    assert p["_checks"] == "failed"
