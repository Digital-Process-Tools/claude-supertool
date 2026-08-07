"""What a refname looks like when nobody is trying (#694, #924).

A branch name is not the tool's text. On both trackers the head branch of a
pull/merge request is named by whoever opened it, and opening one from a fork
needs no permission on the target repo — so every ref that arrives over an API
is attacker-chosen, and #150/#818/#852 established the same whitelist at four
sinks that *execute* one.

This module is the same whitelist for the sinks that **print** one. Those are
the more dangerous half and read as the safer half: a printed command is not
run by supertool, it is run by the reader, who runs it because their own tool
told them to.

`presets/gitlab/mr.py` has carried this since #694 for its `To resolve:` block.
It was private to that file, so `_branch_locale.check()` — the one line five ops
print under `Branch:` — rendered the head branch straight into
`./supertool 'git-checkout:{name}'` with nothing between the name and the quote
it closes. The abstraction existed; the call site had not adopted it. Lifting it
here is that adoption, and it is why `mr.py` now delegates rather than keeping a
second copy of a rule whose whole value is that it is the same rule everywhere.

**Quoting and refusing are both here, because the callers genuinely differ.**
`shell_ref()` is right where the command is the deliverable and its arguments
are needed whatever they contain — `mr.py`'s conflict recipe is that: refusing
to print it leaves a reader mid-conflict with nothing. `ordinary()` alone is
right where the command is a *convenience*, because there a hostile name makes
the suggestion wrong as well as unsafe, and a safe-but-wrong command is the
trade this repo keeps refusing to make. `_branch_locale` is that caller: its
suggestion goes through supertool's colon CLI, which splits `git-checkout:REF`
on `:`, so no amount of shell quoting delivers a ref containing one.
"""
from __future__ import annotations

import re
import shlex

#: What a refname looks like when nobody is trying. Git permits a great deal
#: more — `;`, backtick, `$`, `&`, quotes, parentheses, spaces — and the source
#: branch of a merge request is named by whoever opened it (#694).
ORDINARY_REF = re.compile(r"^[A-Za-z0-9._/-]+$")


def ordinary(ref: str) -> bool:
    """True for a name that can be printed into a shell command untouched.

    The leading-dash exclusion is not cosmetic: `-B` is inside the character
    class above and is still an option, not a ref, and quoting does not stop a
    shell word from being read as a flag.

    Deliberately narrow, and deliberately not widened for non-ASCII names git
    would accept. A second, wider charset invented at one call site is how the
    two drift, and the cost of refusing an unusual-but-honest name here is a
    reader doing one step by hand — visible, and in the safe direction.
    """
    return bool(ref) and not ref.startswith("-") and bool(ORDINARY_REF.match(ref))


def shell_ref(ref: str) -> str:
    """Quote a ref or path for a *printed* shell command (#694).

    The "To resolve:" block is not executed by supertool — it is printed for a
    human or an agent to paste. That is far enough from running it to make this
    hardening rather than a fix for a command injection, and close enough that
    the printed line has to be safe anyway.

    Ordinary names print bare. Quoting every branch to defend against the rare
    one would make the common case harder to read, and a command block that is
    tedious to read is one that gets skimmed instead of checked.

    Option-shaped names are the limit of what quoting can do: `git checkout -B`
    still reads `-B` as a flag inside quotes. They are quoted anyway so they
    look wrong, and `warning()` names them above the block — a name git
    itself refuses to create arriving from the API is worth a reader stopping
    over, and there is no in-line escape that makes it merely a ref.
    """
    if ordinary(ref):
        return ref
    quoted = shlex.quote(ref)
    if quoted == ref:
        # POSIX single-quote escaping is close-quote, escaped quote, reopen:
        # '\''. Unreachable while ORDINARY_REF is narrower than shlex's own
        # safe set (a ref holding ' is never returned unchanged by shlex.quote),
        # so this is the branch that would go wrong silently the day that set is
        # widened — which is the failure mode this file exists to prevent.
        quoted = "'" + ref.replace("'", "'\\''") + "'"
    return quoted


def warning(refs: list[str]) -> str | None:
    """The line above the block when a name in it is not ordinary.

    Quoting makes the command safe to run; this makes it visible that there was
    a reason to quote. `None` when every name is ordinary — the common case
    prints nothing extra, which is what keeps the notice worth reading.
    """
    odd = [r for r in refs if not ordinary(r)]
    if not odd:
        return None
    plural = "s" if len(odd) != 1 else ""
    return (
        f"  # ⚠ {len(odd)} name{plural} below contain characters a shell acts "
        f"on, and have been quoted — read the command before running it."
    )
