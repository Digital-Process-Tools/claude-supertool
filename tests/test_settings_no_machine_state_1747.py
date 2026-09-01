"""The tracked settings file carries only what this repo means to ship (#1747).

`.claude/settings.json` is tracked so that one thing reaches every clone: the
`PreToolUse` registration of `hooks/pre-bash-guard.sh`, which #1698 argues is
exactly the kind of rule that must not depend on a plugin release. But the
harness also owns that file. On 2026-08-15, during a session in which
`/reload-plugins` was run, six lines nobody typed appeared in it:

    "enabledPlugins": {
      "oss@dpt-plugins": true,
      ...
    }

— one machine's plugin roster, in a file every clone inherits. The same block
had been deleted a day earlier as `ships-local-state`, and #1726 closed the
sibling case for four `$HOME`-rooted hook commands. Both of those fixed the
file's *content*. Neither stopped it being re-added, because the writer is not
a person: ordinary use undoes the deletion, and the only thing between the block
and a commit is whoever reads `git status` carefully that day.

**The list is an allowlist, and that is the load-bearing choice.** A denylist of
known-bad keys (`enabledPlugins`, ...) is green the first time the harness
invents a key nobody here has heard of — it fails open, silently, which is this
repository's own defect class. An allowlist fails closed and names the newcomer.

Its cost is real and is not a bug: somebody adding a *legitimate* setting is
interrupted by a red CI leg. That interruption is the feature. The question the
failure message asks is the one that matters — did a human type this key, or did
the harness write it? — because the cheap way out is to widen the allowlist
without asking, and that converts this guard into a formality.

**What this file does not check, stated rather than implied.** The key guard
reads the top-level surface only. Machine state nested *inside* an allowed key
is not caught here, and the neighbouring file only narrows that gap rather than
closing it: `tests/test_settings_hooks_portable_1726.py` refuses a hook command
resolving through `$HOME`, which is one spelling of one machine's disk — an
absolute `/Users/someone/...` inside a hook command passes both files. A future
allowed key arrives with no interior guard at all, and whoever adds it owns
writing one.

The `.gitignore` check below is a **literal, last-match-wins reading of one
path**, not an implementation of git's pattern language. A directory pattern or
a glob that happens to cover `.claude/settings.local.json`, and any negation
spelled other than the exact literal, is invisible to it — it reports
`not-named` in that case, which is a third state and not a pass.

These tests parse JSON and read one text file. They spawn nothing and construct
no paths beyond the repository root, so they carry no platform-dependent
behaviour.
"""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETTINGS = REPO / ".claude" / "settings.json"
GITIGNORE = REPO / ".gitignore"

# Spelled with a forward slash because that is what a .gitignore pattern is on
# every platform — git's ignore syntax does not take the host separator, so this
# is a literal out of a text file rather than a path to be joined.
LOCAL_SETTINGS_PATH = ".claude/settings.local.json"

# Every top-level key this repository intends to track in .claude/settings.json.
# Adding one is a deliberate act: it means the key is something every clone
# should inherit, not something the harness maintains for this machine. If a red
# leg brought you here, read the docstring above before widening this set.
#
# `statusLine` travels because it is repository configuration, not machine state:
# its command is `python3 "$CLAUDE_PROJECT_DIR"/.oss/statusline.py`, which names
# no path outside the checkout and resolves on every clone. #1964 first settled
# the opposite -- that a display choice should be opted into per maintainer -- and
# that reading was reversed: config every developer working on this repo should
# get is exactly what a tracked settings file is for, and routing it to the
# untracked per-machine copy means each of them re-derives it by hand. The
# `$CLAUDE_PROJECT_DIR` spelling is load-bearing, and
# `tests/test_statusline_wiring_documented_1964.py` is what pins it: an absolute
# path here would pass this allowlist and ship one machine's disk to every clone.
ALLOWED_TOP_LEVEL_KEYS = frozenset({"hooks", "statusLine"})

# Keys the harness has actually been observed writing into a tracked settings
# file. Used only to make a failure message more useful; the *guard* is the
# allowlist above, and nothing here narrows it.
KNOWN_HARNESS_KEYS = frozenset({"enabledPlugins", "enabledMcpjsonServers"})


def _settings():
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def _unexpected_top_level_keys(data):
    """The checker under test. Kept separate so it can be fired deliberately."""
    return sorted(set(data) - ALLOWED_TOP_LEVEL_KEYS)


def _explain(offenders):
    lines = []
    for key in offenders:
        if key in KNOWN_HARNESS_KEYS:
            lines.append(
                f"  {key}: the harness maintains this key. It belongs in "
                f".claude/settings.local.json, which is per-machine and ignored."
            )
        else:
            lines.append(f"  {key}: not in ALLOWED_TOP_LEVEL_KEYS.")
    return chr(10).join(lines)


def test_settings_file_parses_to_a_nonempty_object():
    """Positive control for the negative assertion below.

    `no unexpected key` passes when there are no keys at all — a moved file, a
    renamed directory, a parse that returned `{}`. This one fails loudly on each
    of those instead, so the silence below means something.
    """
    assert SETTINGS.is_file(), f"tracked settings file is missing: {SETTINGS}"
    data = _settings()
    assert isinstance(data, dict) and data, (
        f"{SETTINGS} did not parse to a non-empty object; the assertion below "
        f"would have passed without examining anything"
    )
    assert "hooks" in data, (
        "the tracked settings file no longer registers any hook. That is the "
        "only reason this file is tracked at all (#1698), so either the "
        "registration was lost or this test walks a shape the file no longer has"
    )


def test_tracked_settings_carries_no_key_outside_the_allowlist():
    offenders = _unexpected_top_level_keys(_settings())
    assert not offenders, (
        "the tracked .claude/settings.json carries top-level keys this "
        "repository does not intend to ship to every clone (#1747):"
        + chr(10)
        + _explain(offenders)
        + chr(10) * 2
        + "If the harness wrote it, remove it — the per-machine copy is "
        ".claude/settings.local.json. If you typed it deliberately and every "
        "clone should have it, add it to ALLOWED_TOP_LEVEL_KEYS in this file "
        "and say in the pull request why it travels."
    )


def test_the_check_fires_on_the_block_that_was_actually_written():
    """The must-fire half. Without it, the test above passes when it is broken.

    The payload is the block observed on 2026-08-15, verbatim in shape.
    """
    harness_written = {
        "hooks": {"PreToolUse": []},
        "enabledPlugins": {
            "oss@dpt-plugins": True,
            "supertool@dpt-plugins": True,
            "remember@dpt-plugins": True,
            "claude-jit-context@dpt-plugins": True,
        },
    }
    assert _unexpected_top_level_keys(harness_written) == ["enabledPlugins"]
    assert "enabledPlugins" in _explain(["enabledPlugins"])

    invented = {"hooks": {}, "someKeyNobodyHasHeardOfYet": 1}
    assert _unexpected_top_level_keys(invented) == ["someKeyNobodyHasHeardOfYet"], (
        "a denylist would be green here. The direction of the list is the point: "
        "a key nobody anticipated is exactly the one that must be named"
    )

    assert _unexpected_top_level_keys({"hooks": {}}) == []


def test_allowlist_holds_no_key_the_tracked_file_has_stopped_carrying():
    """An allowlist widens silently if entries outlive their key.

    A key added for a real reason, then removed from the file, leaves a hole the
    next harness write can walk through — and nothing renders that hole, because
    the file still passes. So every allowed key must currently be in use.
    """
    data = _settings()
    stale = sorted(ALLOWED_TOP_LEVEL_KEYS - set(data))
    assert not stale, (
        "ALLOWED_TOP_LEVEL_KEYS names keys the tracked settings file no longer "
        "has, so the allowlist is wider than the file it guards: "
        + ", ".join(stale)
    )


def _gitignore_patterns(text):
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _literal_verdict(text, path):
    """Last-match-wins over the *exact literal* `path`. Three states.

    `ignored`, `unignored` (a later `!path` re-includes it), or `not-named` —
    the third being the honest answer when the file never spells this path out,
    whether because nothing covers it or because something covers it by a glob
    this function deliberately does not interpret. Returning `not-named` for
    both is the limit that the module docstring states; returning `ignored` for
    either would be the failure this whole file exists to refuse.
    """
    verdict = "not-named"
    for pattern in _gitignore_patterns(text):
        if pattern == path:
            verdict = "ignored"
        elif pattern == "!" + path:
            verdict = "unignored"
    return verdict


def test_the_per_machine_settings_file_is_ignored_by_this_repos_own_gitignore():
    """The tracked file is not the only way one machine's state gets committed.

    `.claude/settings.local.json` is where the harness's per-machine state
    belongs, and on the maintainer's disk it is ignored — by
    `~/.config/git/ignore`, a file no other clone has. Everywhere else it is
    untracked and unignored, one `git add -A` away from committing the same
    roster under a different name. `git check-ignore` cannot be used to assert
    this: it consults the global file too, so it answers yes on the one machine
    where the answer does not matter. Read the repository's own ignore file.
    """
    assert GITIGNORE.is_file(), f"repository .gitignore is missing: {GITIGNORE}"
    text = GITIGNORE.read_text(encoding="utf-8")
    assert _gitignore_patterns(text), (
        ".gitignore parsed to no patterns at all; the assertion below would have "
        "passed for the wrong reason"
    )
    verdict = _literal_verdict(text, LOCAL_SETTINGS_PATH)
    assert verdict == "ignored", (
        f"this repository's .gitignore does not ignore {LOCAL_SETTINGS_PATH} "
        f"({verdict}), so on every clone without a matching global ignore the "
        f"harness's per-machine settings sit untracked and stageable (#1747)"
    )


def test_the_gitignore_check_fires_on_a_negation_and_on_an_absence():
    """The must-fire half for the check above, over all three of its states.

    Git's rule is last-match-wins, so a `!` line after the entry re-includes the
    file — and a membership test over the pattern list reads that as ignored,
    which is a guard reporting protection it does not have. The absent case is
    asserted too, because `not-named` must not be able to drift into `ignored`.
    """
    ignored = f"__pycache__/{chr(10)}{LOCAL_SETTINGS_PATH}{chr(10)}"
    assert _literal_verdict(ignored, LOCAL_SETTINGS_PATH) == "ignored"

    negated = ignored + f"!{LOCAL_SETTINGS_PATH}{chr(10)}"
    assert _literal_verdict(negated, LOCAL_SETTINGS_PATH) == "unignored", (
        "a later `!` line un-ignores the file under git's last-match-wins rule; "
        "a check that reports `ignored` here claims protection it does not have"
    )

    reinstated = negated + f"{LOCAL_SETTINGS_PATH}{chr(10)}"
    assert _literal_verdict(reinstated, LOCAL_SETTINGS_PATH) == "ignored"

    commented = f"# {LOCAL_SETTINGS_PATH}{chr(10)}.pytest_cache/{chr(10)}"
    assert _literal_verdict(commented, LOCAL_SETTINGS_PATH) == "not-named", (
        "a commented-out entry is not an entry, and `not-named` is a third "
        "state rather than a quiet pass"
    )
