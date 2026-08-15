"""Tracked hook registrations must resolve for every clone, not one disk (#1726).

`.claude/settings.json` is tracked, so every hook command in it is registered in
every clone. Four of its five registrations resolved through
`$HOME/Documents/claude-jit-context/scripts/...` — a path on one maintainer's
machine — behind a `[ -f "$S" ] && bash "$S" || true` guard. The guard is right
in isolation (an absent script must not break a session) and wrong here: for
every other clone the result is that all four hooks silently do nothing. A rule
that never ran and a rule that never matched are indistinguishable from the
inside, which is this repository's own defect class living in the file that
enforces it.

Two properties are asserted, and each is paired with a positive control, because
an assertion over a list nothing was ever added to passes for the wrong reason:

1. No tracked hook command may resolve through `$HOME`.
2. A command that guards on a script's existence must *announce* the absent
   branch rather than returning `true` — the shape the `pre-bash-guard.sh`
   registration in the same file already uses.

This test reads and parses JSON only. It spawns nothing and touches no path, so
it carries no platform-dependent behaviour.
"""

import json
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / ".claude" / "settings.json"


def _hook_commands():
    """Every (event, command) pair registered in the tracked settings file."""
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    pairs = []
    for event, groups in data.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command")
                if command is not None:
                    pairs.append((event, command))
    return pairs


def test_settings_parses_and_registers_at_least_one_hook():
    """Positive control for both assertions below.

    Every other test here is a negative — "no command does X". A negative passes
    when the list is empty, which is exactly what a moved file, a renamed key or
    a parse that returned an empty dict produces. This one fails loudly instead.
    """
    assert SETTINGS.is_file(), f"tracked settings file is missing: {SETTINGS}"
    commands = _hook_commands()
    assert commands, f"parsed no hook commands out of {SETTINGS}"
    assert any(
        "pre-bash-guard.sh" in command for _, command in commands
    ), (
        "the raw-command guard registration is not in the parsed set; the file "
        "was read but the shape this test walks is no longer the shape it has"
    )


def test_no_tracked_hook_command_resolves_through_home():
    offenders = [
        (event, command)
        for event, command in _hook_commands()
        if "$HOME" in command or "${HOME}" in command
    ]
    assert not offenders, (
        "tracked hook commands resolve through $HOME, so they are dead in every "
        "clone but the one that path exists on. Register the hook through the "
        "plugin that owns it, or through $CLAUDE_PROJECT_DIR if the script is in "
        "this checkout:\n"
        + "\n".join(f"  {event}: {command}" for event, command in offenders)
    )


def test_absent_script_branches_announce_themselves():
    """A guarded command must say the guard did not run, not return `true`."""
    guarded = [
        (event, command)
        for event, command in _hook_commands()
        if "-f " in command
    ]
    assert guarded, (
        "no registration guards on a script's existence at all; this test would "
        "otherwise pass without having examined anything"
    )

    offenders = [
        (event, command)
        for event, command in guarded
        if "|| true" in command or "else" not in command
    ]
    assert not offenders, (
        "a hook whose script may be absent must announce that it did not run. "
        "`|| true` makes a guard that never ran look exactly like a guard that "
        "found nothing:\n"
        + "\n".join(f"  {event}: {command}" for event, command in offenders)
    )
