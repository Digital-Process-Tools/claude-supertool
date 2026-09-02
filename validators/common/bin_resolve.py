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
because POSIX-mode `shlex.split` treats a bare backslash as an escape
character and would otherwise corrupt an unquoted Windows path even when it
contains no space (#2176).
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

    candidate = raw.replace("\\", "/")

    if _is_executable(candidate):
        return [candidate]

    parts = shlex.split(candidate, posix=True)
    return parts or [default]
