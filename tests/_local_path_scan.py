"""Shared regex + hit-extraction for "does this text carry a real local
filesystem username-shaped path" -- factored out of
`test_trap_d_no_local_paths_2426.py` (#2426) so #2440's whole-artifact sweep
does not reimplement the same regex a second time, drifting the way #555's
six near-identical `_load` helpers did (see `_repo_walk.py`'s own docstring
for that precedent).

Three shapes: `/Users/<name>/...`, `/home/<name>/...`, `C:\\Users\\<name>\\...`,
plus the dash-flattened `-Users-<name>-...` form this repo's own scratchpad
directory naming produces. See `test_trap_d_no_local_paths_2426.py`'s module
docstring for the full history of each addition.
"""
from __future__ import annotations

import re

# `runneradmin` is the one Windows CI account name this repo's own fixtures
# already use as a legitimate non-maintainer example (trap.d/1165's Windows
# shlex note); everything else matching the shape below is a real local
# username and is in scope. `...` is an ellipsis placeholder for "some
# intermediate path segment", not a username.
_ALLOWED_WINDOWS_USERS = {"runneradmin", "Public", "Default", "..."}

# Terminates a captured username without requiring a further path segment --
# "/Users/<name>." or "/Users/<name> (parenthetical)" must still be flagged.
_BOUNDARY = r"[/\\ \t\r\n.,;:!?)`]|$"

_LOCAL_PATH = re.compile(
    r"/Users/(?P<posix_user>[A-Za-z0-9_.-]+)(?:" + _BOUNDARY + r")"
    r"|/home/(?P<home_user>[A-Za-z0-9_.-]+)(?:" + _BOUNDARY + r")"
    r"|C:\\Users\\(?P<win_user>[A-Za-z0-9_.-]+)(?:" + _BOUNDARY + r")"
    r"|-Users-(?P<dash_user>[A-Za-z0-9_.]+)-"
)


def _local_path_hits(text: str, restrict_users: frozenset[str] | None = None) -> list[str]:
    """Every username-shaped local-path hit in `text`.

    `restrict_users`, when given, narrows the report to only the named
    usernames (case-sensitive) instead of every non-placeholder hit -- used
    by #2440's whole-artifact sweep, where a generic scan would also flag
    every legitimate single-word example path (`/Users/x`, `/home/dev`, ...)
    this repo's own docs deliberately use throughout. `None` keeps the
    original #2426 behaviour: every hit not in the Windows-runner allowlist.
    """
    hits = []
    for match in _LOCAL_PATH.finditer(text):
        user = (
            match.group("posix_user")
            or match.group("home_user")
            or match.group("win_user")
            or match.group("dash_user")
        )
        if user in _ALLOWED_WINDOWS_USERS:
            continue
        if restrict_users is not None and user not in restrict_users:
            continue
        hits.append(match.group(0))
    return hits
