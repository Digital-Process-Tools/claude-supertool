"""#2732: the Anthropic directory held a release-tree probe on
HOOK_GRANTS_PERMISSION for `hooks/hooks.json`'s PreToolUse `Bash|PowerShell`
matcher, reading `hooks/pre-bash-guard.sh` as a script that "approves tool
calls". The hook only ever emits `deny` -- `_deny()` (pre-bash-guard.sh) and
`_say("deny", ...)` (pre_bash_guard.py) are the only writers of
`permissionDecision`, and the value is a literal `"deny"` in both. The
holder's own hypothesis (issue body) is that a comment quoting
`"permissionDecision":"allow"` verbatim, plus several prose uses of the word
"allow" to describe the hook's disclosed fail-open, is what a scanner reading
this file co-occurring with the matcher would flag.

The fix is wording, not behaviour: every shipped file a `PreToolUse` hook's
command reaches must describe its own fail-open without spelling an
approving decision. "The command was allowed" (a literal, shipped string in
three files, found by this issue, not by the holder) is the sharper
instance -- it survives any comment-stripping build step because it is not
a comment.

Would this test still pass if the code did nothing? No: before #2732's
fix, `_FORBIDDEN` matched in all three files below.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Exact shapes the directory's own write-up (claude-jit-context's
# docs/directory-validator.md) and this issue's holder cite: a literal,
# JSON-shaped approving decision, or prose that names the fail-open as an
# "allow" rather than as "not denied" / "proceeded".
_FORBIDDEN = (
    '"permissionDecision":"allow"',
    "The command was allowed",
    "disclosed allow",
    "disclosed-allow",
    "discloses and allows",
    "Disclose and allow",
    "forged `allow`",
    "ran and approved",
)

_HOOK_FILES = (
    "hooks/pre-bash-guard.sh",
    "hooks/pre_bash_guard.py",
    "hooks/shipped_rules.py",
)


@pytest.mark.parametrize("rel", _HOOK_FILES)
@pytest.mark.parametrize("phrase", _FORBIDDEN)
def test_no_approving_decision_language(rel: str, phrase: str) -> None:
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert phrase not in text, (
        f"{rel} still spells {phrase!r} -- the shape HOOK_GRANTS_PERMISSION "
        f"(#2732) was read from")


@pytest.mark.parametrize("rel", _HOOK_FILES)
def test_the_guard_still_only_writes_deny(rel: str) -> None:
    """Positive control: a sweep that passed because the file went empty, or
    because `deny` itself got scrubbed, would be the absence this issue is
    about, wearing the fix's clothes."""
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert '"deny"' in text, (
        f"{rel} no longer spells a literal deny decision anywhere -- "
        f"the positive control that makes the sweep above meaningful")
