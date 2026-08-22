"""#1872 -- `presets/watch/README.md` said `unwatch` reads the PID file, where
`cmd_unwatch` stops every live poller for the slot, tracked and untracked
alike (the #511 multi-kill). `docs/presets/watch.md` already had this right,
so the two documents disagreed -- and the wrong one is the shorter file an
operator reads first.

Nothing here drives a real poller; this asserts the prose against a string,
which is what a prose bug is.
"""
from __future__ import annotations

from pathlib import Path

README = Path(__file__).parent.parent / "presets" / "watch" / "README.md"
LONG_DOC = Path(__file__).parent.parent / "docs" / "presets" / "watch.md"


def _bullet(text: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith("- `unwatch`"):
            return line
    raise AssertionError("no `- `unwatch`` bullet found in the lifecycle list")


def test_the_readme_no_longer_claims_a_pid_file_only_read() -> None:
    bullet = _bullet(README.read_text(encoding="utf-8"))
    # The wrong claim: reading a PID file is presented as the whole mechanism,
    # silent about the untracked pollers a PID-file read cannot see.
    assert "reads the PID file, SIGTERM" not in bullet, bullet


def test_the_readme_states_it_stops_every_live_poller(README_TEXT=None) -> None:
    bullet = _bullet(README.read_text(encoding="utf-8"))
    assert "every" in bullet.lower() and "live" in bullet.lower(), bullet
    assert "untracked" in README.read_text(encoding="utf-8")[
        README.read_text(encoding="utf-8").index(bullet):
        README.read_text(encoding="utf-8").index(bullet) + 400], bullet


def test_the_two_documents_no_longer_disagree() -> None:
    """`docs/presets/watch.md` already had this right (#1869 fixed its sibling
    number in the same bullet). Both must now say the multi-kill, not just one.
    """
    long_text = LONG_DOC.read_text(encoding="utf-8")
    assert "unwatch` stops **every** live poller" in long_text, (
        "docs/presets/watch.md's own wording moved -- re-check what the "
        "README should match")
    short_bullet = _bullet(README.read_text(encoding="utf-8"))
    assert "every" in short_bullet.lower() and "live" in short_bullet.lower()
