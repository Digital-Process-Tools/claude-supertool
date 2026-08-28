"""#2073 -- the `classify` live controls skip on every CI runner, and until
now that skip carried no token, so `conftest`'s skip census reported zero
against every reason it knew while all four went unaccounted for.

Same shape as `tests/test_live_gh_gating_1568.py` and
`tests/test_symlink_count_population_1274.py`: a countable-skip module
(`tests/_classify_live.py`) sits in front of the one call that can decline,
and this file pins two things separately -- the gate itself (does it skip,
carrying the token, exactly when `claude` is absent, and NOT when it is
present) and the census (does `conftest` turn that into a stated count, at
zero and at the full population, rather than a silent absence).

Positive and negative control in the same fixture throughout: a mechanism
that only ever prints when there is something to report cannot be told apart
from one that never runs, and the loud half of this repo's own defect is
exactly a check that skips something and produces nothing sayable about it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import _classify_live

TESTS = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_2073", str(TESTS / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Report:
    def __init__(self, reason):
        self.longrepr = ("f.py", 1, reason)


class _Reporter:
    def __init__(self, reasons):
        self.stats = {"skipped": [_Report(r) for r in reasons]}
        self.lines = []

    def write_line(self, line):
        self.lines.append(line)


def _summary(reasons):
    conftest = _load("conftest")
    reporter = _Reporter(reasons)
    conftest.pytest_terminal_summary(reporter, 0, None)
    return chr(10).join(reporter.lines)


# --- the gate: skips carrying the token exactly when claude is absent ------


def test_require_claude_skips_carrying_the_token_when_absent(monkeypatch):
    monkeypatch.setattr(_classify_live.shutil, "which", lambda name: None)
    with pytest.raises(pytest.skip.Exception) as caught:
        _classify_live.require_claude()
    assert _classify_live.TOKEN in str(caught.value), caught.value


def test_require_claude_does_not_skip_when_present(monkeypatch):
    """The positive control: with a `claude` on PATH the gate must be a
    no-op, or the negative above proves nothing about the branch that
    matters -- the mechanism must not degrade a run where the binary really
    is there into a standing skip."""
    monkeypatch.setattr(_classify_live.shutil, "which",
                         lambda name: "/usr/local/bin/claude")
    _classify_live.require_claude()  # must not raise


# --- the census: the count is printed, at zero and at the full population -


REASONS_NONE = [
    "some other reason entirely",
    "not the classify token at all",
]

REASONS_ALL_FOUR = [_classify_live.TOKEN + ": claude is not on PATH here"] * 4 + [
    "an unrelated skip reason",
]


def test_the_census_prints_zero_rather_than_staying_silent():
    """The whole point: a run where all four ran must say `0 of N`, not
    omit the line -- silence and a true zero must not render the same way."""
    out = _summary(REASONS_NONE)
    assert _classify_live.TOKEN in out, out
    assert "0 of 2 skipped" in out, out


def test_the_census_counts_all_four_when_the_binary_is_absent():
    out = _summary(REASONS_ALL_FOUR)
    assert "4 of 5 skipped" in out, out


def test_the_verdict_line_names_its_denominator():
    line = _classify_live.verdict_line(2, 9)
    assert _classify_live.TOKEN in line
    assert "2 of 9" in line


def test_every_test_in_the_live_file_is_gated_through_the_shared_call():
    """A hand-rolled `pytest.skip` at any of the four call sites would skip
    without the token and vanish from the count exactly the way `_symlink`'s
    mechanism `D` did (#1274) -- so pin that all four go through
    `_classify_live.require_claude`, not a private helper."""
    import ast

    src = (TESTS / "test_classify_live_2046.py").read_text()
    tree = ast.parse(src)
    test_funcs = [n for n in tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name.startswith("test_")]
    assert len(test_funcs) == 4, (
        "population changed -- update this pin and the issue's own count: "
        + repr([f.name for f in test_funcs]))
    for fn in test_funcs:
        calls = [ast.dump(n.func) for n in ast.walk(fn)
                 if isinstance(n, ast.Call)]
        assert any("require_claude" in c for c in calls), (
            fn.name + " does not call require_claude() -- its skip, if any, "
            "would not carry " + _classify_live.TOKEN)
