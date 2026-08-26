"""`.oss/statusline.py` is tracked and shipped, but wiring it is a per-maintainer
choice, not something this repository imposes on every clone (#1964).

The file arrives through `/oss:scaffold --apply` (#1956), which writes it into the
maintainer's checkout so a fix reaches it by being rewritten upstream, in the oss
plugin -- see `.github/scripts/coverage_gate.py`'s `NOT_MEASURED` entry for that
file. PR #1956 claimed the tracked `.claude/settings.json` already pointed a
`statusLine` key at it; that was read out of an uncommitted working copy and was
never true at any committed revision. The result was the inverse of the intended
fix: a tracked file with no pointer at all, shipped as dormant code every plugin
user's clone carries and nothing in their own configuration will ever invoke.

#1964 settles which of two states is correct rather than leaving the third
(shipped, silently orphaned) in place:

* commit the `statusLine` key, so the tracked file is tracked-and-wired, or
* leave it deliberately unwired, and say so somewhere a reader will meet it.

This repository chose the second: a `statusLine` key in a tracked
`.claude/settings.json` would run a status line in every contributor's session
the moment they clone, without them opting in, and a display choice is not the
same kind of imposition as the tracked `PreToolUse` guard that already runs on
every clone -- the guard protects the repository's own commands; a status line
is cosmetic and per-maintainer.

This test pins that decision so the gap #1964 was filed about cannot reopen
silently: `.oss/statusline.py` must be *either* referenced by something tracked
that will actually run it, *or* documented as an opt-in step a maintainer takes
deliberately. Losing both -- the tracked reference or the doc -- is exactly the
state #1964 found.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _settings_wires_statusline() -> bool:
    settings_path = REPO_ROOT / ".claude" / "settings.json"
    if not settings_path.is_file():
        return False
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    return "statusLine" in settings


def _contributing_documents_statusline_as_opt_in() -> bool:
    text = (REPO_ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")
    return (
        "statusline.py" in text
        and "opt-in" in text.lower()
        and "settings.local.json" in text
    )


def test_statusline_is_tracked():
    """Sanity check for the fixture the other two assertions depend on: if this
    ever goes false, `.oss/statusline.py` was deleted or renamed and the test
    below is vacuous rather than failing for the right reason."""
    assert (REPO_ROOT / ".oss" / "statusline.py").is_file()


def test_statusline_is_wired_or_documented_as_opt_in():
    """The third state -- shipped, and nothing tracked says whether that is on
    purpose -- must not be reachable. Either the tracked settings wire it, or
    the docs say plainly that wiring it is a maintainer's own opt-in step."""
    wired = _settings_wires_statusline()
    documented = _contributing_documents_statusline_as_opt_in()
    assert wired or documented, (
        ".oss/statusline.py is tracked but neither wired in .claude/settings.json "
        "nor documented in docs/contributing.md as an opt-in step (#1964) -- this "
        "is the exact silent-orphan state #1964 was filed to close"
    )


def test_settings_json_does_not_wire_statusline():
    """This repository's decision, pinned: `.claude/settings.json` stays free of
    a `statusLine` key, because that key would run on every clone the moment it
    is checked out, with no opt-in step for the contributor who did not ask for
    it. Flipping this decision means also flipping the docs test above and
    updating this test in the same PR, not letting the two drift apart."""
    assert not _settings_wires_statusline()
