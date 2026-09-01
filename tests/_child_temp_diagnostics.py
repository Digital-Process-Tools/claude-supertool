"""Snapshot of temp-path state around a nested-pytest child spawn (#2015).

Five Windows CI occurrences of a nested pytest child dying during collection
share only two facts: a `FileNotFoundError` naming a temp directory, and
nothing else -- the log shows the crash but not the state that produced it.
The two competing explanations on file (a canonicalisation gap through the
legacy `C:\\Documents and Settings` junction, or a lifetime race on the
directory's own teardown) cannot be told apart from that log alone, and a
fifth occurrence (2026-08-29, `st756_vldxmp9l` under the *canonical*
`C:\\Users\\...` spelling) already rules the junction theory out on its own:
whatever this is, it survives correct canonicalisation.

This module is the instrument, not the fix. It answers the issue's own
"what would settle it": capture the tempdir's own realpath and existence,
and the env vars a child inherits it through, immediately around the spawn,
so the next occurrence carries evidence instead of just the crash.

A sixth occurrence landed after this module was written (see
`tests/_isolated_child_tmp.py`'s own docstring for what it added to the
count and the mitigation it motivated); "five" above is a historical count
as of this module's own authorship, not the count as of the issue's close.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict


def snapshot_temp_state() -> Dict[str, Any]:
    """A snapshot that cannot itself raise -- callable safely from inside an
    assertion message being built after something has already gone wrong."""
    raw = tempfile.gettempdir()
    try:
        real = os.path.realpath(raw)
    except OSError as exc:
        real = f"<realpath failed: {exc!r}>"
    try:
        exists = os.path.isdir(raw)
    except OSError as exc:
        exists = f"<stat failed: {exc!r}>"
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        try:
            cache_home_exists: Any = os.path.isdir(cache_home)
        except OSError as exc:
            cache_home_exists = f"<stat failed: {exc!r}>"
    else:
        cache_home_exists = None
    return {
        "tempdir": raw,
        "tempdir_realpath": real,
        "tempdir_exists": exists,
        "TMP": os.environ.get("TMP"),
        "TEMP": os.environ.get("TEMP"),
        "XDG_CACHE_HOME": cache_home,
        "XDG_CACHE_HOME_exists": cache_home_exists,
    }


def describe(label: str, snapshot: Dict[str, Any]) -> str:
    """Render one snapshot as a block, for embedding in a pytest.fail message."""
    lines = [f"-- temp state ({label}) --"]
    for key, val in snapshot.items():
        lines.append(f"  {key}: {val!r}")
    return "\n".join(lines)
