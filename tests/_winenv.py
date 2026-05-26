"""Shared helper for tests that strip PATH but still need to run a subprocess.

On Windows, passing ``env={"PATH": ""}`` wipes SYSTEMROOT/WINDIR/PATHEXT too,
which prevents the Python interpreter from starting (cannot locate system
DLLs). The subprocess then produces empty stdout and the test crashes with a
``JSONDecodeError`` instead of validating the adapter's graceful-degrade path.

``empty_path_env()`` returns an env dict with PATH stripped but Windows
essentials preserved, so ``shutil.which(tool)`` still returns ``None`` (tool
not findable) while ``python.exe`` itself can still launch.

POSIX note: the ``KEEP`` names are Windows-specific. On Linux/macOS none of
them exist in ``os.environ`` (case-sensitive), so the comprehension yields an
empty dict and the returned env is just ``{"PATH": ""}`` — same behaviour as
the original tests. Single code path, no platform branch needed.
"""
from __future__ import annotations

import os
from typing import Dict

# Windows env vars the Python interpreter (and core CRT) need to start at all.
# APPDATA / LOCALAPPDATA / USERPROFILE are NOT required for a bare
# ``python.exe`` launch — only SYSTEMROOT + WINDIR for DLL resolution.
# PATHEXT is kept so ``shutil.which`` behaves the same way for the validator
# adapter (still returns None for missing tools).
_KEEP = frozenset({"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC"})


def empty_path_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _KEEP}
    env["PATH"] = ""
    return env
