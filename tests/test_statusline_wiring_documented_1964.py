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

**This repository chose the second and then reversed it.** The original reading
was that a status line is cosmetic and per-maintainer, so wiring it in a tracked
file imposes a display choice on everybody who clones. What that reading missed
is what the untracked alternative costs: `.claude/settings.local.json` is
per-*machine* state, so routing the key there means every developer who wants to
work on this repository re-derives the same wiring by hand, and none of them can
see that the others did. Configuration that every developer of a repository
should have is precisely what a tracked settings file is for -- and the command,
`python3 "$CLAUDE_PROJECT_DIR"/.oss/statusline.py`, names nothing outside the
checkout, so it carries none of the machine state #1747 exists to keep out.

So the tracked key is now the pinned state, and the assertions below moved with
it rather than being deleted: the file must be wired AND the doc section must
say so. The gap #1964 was filed about is unchanged -- `.oss/statusline.py`
shipped with nothing tracked saying whether that was on purpose -- and losing
either half reopens it.

The `$CLAUDE_PROJECT_DIR` spelling is asserted rather than assumed. An absolute
`/Users/someone/...` command passes `tests/test_settings_no_machine_state_1747.py`'s
top-level key allowlist (it reads keys, not their interiors, and says so), so
this file is the only thing standing between one machine's disk and every clone.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _statusline_command() -> str | None:
    """The command the tracked settings would run, or ``None`` if nothing is wired.

    ``None`` covers a missing file and a missing key alike; both mean the same
    thing to every caller here, and neither is a shape the assertions below want
    to distinguish. A `statusLine` that is not an object, or carries no string
    `command`, returns the empty string -- present and unusable, which must not
    read as either wired or absent.
    """
    settings_path = REPO_ROOT / ".claude" / "settings.json"
    if not settings_path.is_file():
        return None
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    if "statusLine" not in settings:
        return None
    block = settings["statusLine"]
    if not isinstance(block, dict):
        return ""
    command = block.get("command")
    return command if isinstance(command, str) else ""


def _settings_wires_statusline() -> bool:
    return bool(_statusline_command())


def _contributing_documents_statusline() -> bool:
    text = (REPO_ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")
    return (
        "statusline.py" in text
        and "settings.json" in text
        and "CLAUDE_PROJECT_DIR" in text
    )


def test_statusline_is_tracked():
    """Sanity check for the fixture the other two assertions depend on: if this
    ever goes false, `.oss/statusline.py` was deleted or renamed and the test
    below is vacuous rather than failing for the right reason."""
    assert (REPO_ROOT / ".oss" / "statusline.py").is_file()


def test_statusline_is_wired_and_documented():
    """The third state -- shipped, and nothing tracked says whether that is on
    purpose -- must not be reachable. The tracked settings wire it, and the doc
    section says so; losing either half is the state #1964 found."""
    assert _settings_wires_statusline(), (
        ".oss/statusline.py is tracked but .claude/settings.json wires no usable "
        "statusLine command (#1964). Every developer working on this repository "
        "should get the same wiring from the checkout; the untracked "
        ".claude/settings.local.json is per-machine state and makes each of them "
        "re-derive it by hand"
    )
    assert _contributing_documents_statusline(), (
        "docs/contributing.md no longer documents the statusline wiring (#1964) -- "
        "a tracked key that runs in every clone must be described where a "
        "contributor meets it"
    )


def test_settings_json_wires_statusline_through_the_project_dir_variable():
    """The wiring must resolve on every clone, not on the machine that wrote it.

    `tests/test_settings_no_machine_state_1747.py` guards which top-level keys
    travel, and states plainly that it reads the top-level surface only -- an
    absolute `/Users/someone/...` inside an allowed key passes it. This is the
    interior guard that key owes, per that file's own closing paragraph.
    """
    command = _statusline_command()
    assert command, "no statusLine command to check -- see the test above"
    assert "$CLAUDE_PROJECT_DIR" in command, (
        "the tracked statusLine command does not resolve through "
        "$CLAUDE_PROJECT_DIR, so it names some particular checkout rather than "
        "whichever one is open: " + command
    )
    assert "$HOME" not in command and not command.startswith("/"), (
        "the tracked statusLine command reaches outside the checkout, which "
        "ships one machine's disk to every clone (#1747): " + command
    )
    assert ".oss/statusline.py" in command, (
        "the tracked statusLine command does not run .oss/statusline.py, so "
        "this file is pinning something other than what it documents: " + command
    )
