"""Numeric env knobs, read once and read the same way everywhere (#654).

`presets/git/trail.py` wrapped a bare `int()` straight around its
`os.environ` lookups. `SUPERTOOL_MAX_COMMITS=x` therefore ended the run in a
`ValueError` traceback pointing at `int()` — which tells the caller the tool
broke, but not which variable, not what value it saw, and not what a good one
looks like.

The obvious repair is the wrong one. `try: ... except ValueError: return
default` stops the crash by making the knob *silently ignored*: a caller who set
a cap goes on believing it is in force. That trades a loud failure for a quiet
one, and the quiet one is worse — the crash at least told you something was
wrong. So this module keeps the three states of `docs/validators.md`'s
"Declining instead of guessing": honour the value, or say plainly that it could
not be read **and what is being used instead**. Never both silently.

**The notice goes to stdout, not stderr, and that is not a style choice.**
`_run_custom_op` in `supertool.py` returns `result.stdout` on success and only
appends `result.stderr` when the preset exits non-zero. A preset that warns on
stderr and then succeeds — the exact path here, since falling back to a default
*is* success — has its warning dropped by supertool before the caller sees a
byte of it. Writing to stderr would have shipped the silence this module exists
to prevent.

**The messages are pure ASCII.** Most of the repo prints ✓/✗/⚠ freely, and
`use_utf8_stdout()` makes that safe — but that call is per-preset, and this
helper is also used from `supertool.py` at module import, before any such
setup. A notice about a misconfigured environment must not itself raise
`UnicodeEncodeError` on a cp1252 console; the one message guaranteed to be read
on a broken setup is the worst possible place for a new failure mode.
"""
from __future__ import annotations

import os
import sys
from typing import Optional


#: Messages already emitted this process. Several of these knobs are read from
#: helpers called once per file or once per git call, so a single bad value
#: would otherwise repeat its notice ten times and bury the output it is
#: attached to. Said once is said; said ten times is noise that trains the
#: reader to skip it, which costs the fix its whole point.
_ANNOUNCED: "set[str]" = set()


def _notice(text: str) -> None:
    """One line, on stdout, flushed, at most once per distinct message.

    See the module docstring for why stdout and why ASCII.
    """
    if text in _ANNOUNCED:
        return
    _ANNOUNCED.add(text)
    print(text)
    sys.stdout.flush()


def env_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
    """Read `name` as an int, or say why it could not be and what is in force.

    Unset is silent — there is nothing to report about a knob nobody touched.
    Set-but-unusable is announced and falls back to `default`.

    `minimum` is a validated floor, not a clamp. A value below it is refused and
    announced exactly like unparseable junk, because `SUPERTOOL_MAX_COMMITS=-5`
    expresses no more usable intent than `=x` does. Silently clamping it to the
    floor would invent an intent the caller never expressed — and "show me -5
    commits" quietly becoming "show me the minimum" is the same silent class in
    a different hat.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _notice(f"note: {name}={raw!r} is not a whole number "
                f"- ignoring it and using {default}.")
        return default
    if minimum is not None and value < minimum:
        _notice(f"note: {name}={raw!r} is below the minimum of {minimum} "
                f"- ignoring it and using {default}.")
        return default
    return value


def env_float(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    """`env_int` for the knobs measured in seconds. Same contract, same messages."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _notice(f"note: {name}={raw!r} is not a number "
                f"- ignoring it and using {default}.")
        return default
    if value != value:  # NaN compares false against every bound, including its own
        _notice(f"note: {name}={raw!r} is not a usable number "
                f"- ignoring it and using {default}.")
        return default
    if minimum is not None and value < minimum:
        _notice(f"note: {name}={raw!r} is below the minimum of {minimum} "
                f"- ignoring it and using {default}.")
        return default
    return value
