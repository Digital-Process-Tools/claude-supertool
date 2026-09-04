"""Resolve a configurable `_BIN` env var to an argv list (#2176).

The value may be either a single binary path -- which may legitimately
contain a space, e.g. the default Windows install location
`C:\\Program Files\\glab\\glab.exe` -- or a full shell-quoted command line,
e.g. `python /path/to/stub.py` (the shape this repo's own cross-platform
test stubs pass, quoted with `shlex.quote` per token so a POSIX-mode
`shlex.split` parses it back correctly).

Unconditionally `shlex.split`-ing an UNQUOTED single path breaks on any
space in it: `Program Files` splits into two tokens, and the adapter takes
the first ("C:/Program") as the binary, which does not exist -- a silent-off
on the platform's own default install path (#2176).

The fix tries the raw value as one whole path FIRST. Only when that does not
resolve to a real, executable file does it fall back to `shlex.split`, which
is what makes the quoted multi-token form (the test-stub convention above)
keep working. A backslash-to-forward-slash pass runs before both attempts,
ONLY on Windows (`os.name == "nt"`), because POSIX-mode `shlex.split` treats
a bare backslash as an escape character and would otherwise corrupt an
unquoted Windows path even when it contains no space (#2176).

That normalisation must never run on POSIX: a backslash is an ordinary,
unescaped character in a POSIX filename, so rewriting it to a forward slash
there silently resolves a DIFFERENT path than the one configured (#2249).

When the existence check fails and the `shlex.split` fallback changes the
value's shape (i.e. the resolved binary no longer matches what was
configured), `describe_unresolved()` discloses the original raw value
alongside it, so a caller's "not found" diagnostic doesn't quietly describe
a path the operator never set (#2250).
"""
from __future__ import annotations

import os
import shlex
import shutil


def _is_executable(path: str) -> bool:
    return bool(shutil.which(path)) or (
        os.path.isfile(path) and os.access(path, os.X_OK)
    )


def resolve_bin_cmd(raw: str, default: str) -> list[str]:
    """Turn a `_BIN` env var's raw string into an argv-prefix list.

    `raw` is the value already read from the environment (or `default` if
    unset by the caller). Returns a non-empty list; `[default]` only when
    `raw` itself is empty.
    """
    if not raw:
        return [default]

    # Only Windows treats a bare backslash as a path separator that a
    # shlex-split could corrupt (#2176). On POSIX a backslash is an
    # ordinary filename character with no escaping meaning to the
    # filesystem, so rewriting it there would resolve a different,
    # wrong path (#2249).
    candidate = raw.replace("\\", "/") if os.name == "nt" else raw

    if _is_executable(candidate):
        return [candidate]

    parts = shlex.split(candidate, posix=True)
    return parts or [default]


def describe_unresolved(raw: str, resolved: str) -> str:
    """Diagnostic-friendly description of a binary that failed to resolve (#2250).

    `resolved` is the first element of `resolve_bin_cmd`'s return value --
    what a caller is about to report as "not found". When the existence
    check failed and the `shlex.split` fallback changed the value's shape
    (it no longer matches what was configured, e.g. an unquoted Windows
    path split at its first space), disclose the ORIGINAL raw value
    alongside it, so an operator can tell a typo from a genuine
    multi-token command rather than being shown a path they never set.
    """
    if not raw or resolved == raw:
        return resolved
    return f"{resolved} (configured: {raw!r})"
