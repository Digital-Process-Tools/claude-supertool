"""#1274 -- the symlink-capability count is a floor, and must say so.

#1232 fixed the loud instance: the terminal line read `0 symlink-dependent
tests did NOT run` while four tests were failing for exactly that reason. What
is left is the quiet one. The line counts skips carrying ``_symlink.TOKEN``,
and three of the five mechanisms in
``tests/test_symlink_gating_register_1232.py`` do not produce one:

  * ``P`` -- a collection-time marker for an unrelated reason (``needs_nofollow``
    is ``not hasattr(os, "O_NOFOLLOW")``, which is every Windows runner). It
    fires before the body, so no token skip is ever produced.
  * ``E`` -- the call sits inside an ``except OSError`` arm. That is not a skip
    at all; the test runs and takes its fallback.
  * ``D`` -- a hand-rolled ``_can_symlink()`` probe with its own
    ``pytest.skip`` message, in three files. A hand-rolled skip is precisely a
    skip the count cannot see.

``D`` is removable and is removed -- those sites now call the shared
``require_symlink()``. ``P`` and ``E`` are not: forcing the token onto a
``needs_nofollow`` skip would assert that a test was held back by the missing
privilege when nothing ever asked, which is a fresh invented claim rather than
a fixed one. So the number stays a subset, and the reporting layer names the
subset instead of presenting it as a total.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import _symlink

TESTS = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_1274", str(TESTS / (name + ".py")))
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


def _summary(monkeypatch, probe, reasons):
    conftest = _load("conftest")
    monkeypatch.setattr(conftest._symlink, "_PROBE", probe)
    reporter = _Reporter(reasons)
    conftest.pytest_terminal_summary(reporter, 0, None)
    return chr(10).join(reporter.lines)


REASONS = [
    _symlink.TOKEN + ": cannot create a symlink here -- forced",
    "this platform has no O_NOFOLLOW, so the guard cannot be enforced",
    "posix only",
]


def test_the_unavailable_line_states_the_denominator_it_counted(monkeypatch):
    """`1` on its own reads as a total. `1 of 3` cannot."""
    out = _summary(monkeypatch, (False, "forced absent by #1274"), REASONS)
    assert "1 of 3 skipped" in out, out


def test_the_summary_says_which_skips_are_not_in_the_number(monkeypatch):
    """The whole issue: a plausible non-zero number prompts nobody to check it.

    So the line has to carry its own population -- the two mechanisms that keep
    a symlink call site off a privilege-less runner without producing a token
    skip, and where the full list lives.
    """
    out = _summary(monkeypatch, (False, "forced absent by #1274"), REASONS)
    assert "O_NOFOLLOW" in out, out
    assert "except OSError" in out, out
    assert "tests/test_symlink_gating_register_1232.py" in out, out


def test_the_available_line_names_its_population_too(monkeypatch):
    """`windows-latest` has the privilege today, so this is the branch anyone
    here can actually observe -- and it is the branch that prints `0` next to
    twelve tests skipped by `needs_nofollow`."""
    out = _summary(monkeypatch, (True, ""), REASONS)
    assert "1 of 3 skipped" in out, out
    assert "tests/test_symlink_gating_register_1232.py" in out, out


def test_no_call_site_hand_rolls_its_own_privilege_probe():
    """Mechanism `D` is gone from the tree, and the classifier still knows it.

    Asserted here rather than by deleting `D` from the register: a classifier
    that could no longer recognise a hand-rolled probe would relabel the next
    one `UNGATED` or `P`, and this would pass vacuously.
    """
    register = _load("test_symlink_gating_register_1232")
    hand_rolled = dict(
        (key, [line for line, mech in sites if mech == register.D])
        for key, sites in register._call_sites().items())
    hand_rolled = dict((k, v) for k, v in hand_rolled.items() if v)
    assert not hand_rolled, (
        "these sites gate themselves with a local probe and their own "
        "pytest.skip message, so their skip does not carry " + _symlink.TOKEN
        + " and the terminal count cannot see them (#1143, #1274). Call "
        "`_symlink.require_symlink()` instead: " + repr(sorted(hand_rolled)))
