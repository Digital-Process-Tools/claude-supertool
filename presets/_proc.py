#!/usr/bin/env python3
"""The one process-liveness probe, shared by every preset that reads a PID file.

`os.kill(pid, 0)` is the POSIX idiom for "does this PID exist" — the null
signal is delivered to nothing and only the error tells you the answer. On
Windows there is no signal delivery, and Python routes any signal other than
CTRL_C_EVENT/CTRL_BREAK_EVENT to TerminateProcess. So on Windows the idiom
does not ask whether a process is alive: it kills it. Windows therefore gets
an explicit read-only probe instead.

This module exists because the same six lines lived in three files and drifted
— two copies caught `OSError` and the third did not (WinError 87 escaping out
of `radar`, #422), and then two copies kept the destructive idiom after the
third was fixed (#429). One probe, one place, no third copy.
"""
from __future__ import annotations

import os
import sys

# GetExitCodeProcess reports this for a process that has not exited.
WIN_STILL_ACTIVE = 259
# Read-only access right. Deliberately not PROCESS_ALL_ACCESS: the probe must
# never hold a handle powerful enough to terminate what it is inspecting.
WIN_QUERY_LIMITED_INFORMATION = 0x1000


def kernel32():
    """Seam so the Windows probe can be exercised from a POSIX test runner."""
    import ctypes
    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def pid_alive_windows(pid: int) -> bool:
    """Non-destructive liveness probe: open for query only, read exit code."""
    import ctypes
    k32 = kernel32()
    handle = k32.OpenProcess(WIN_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == WIN_STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Is this PID a live process? Always answers — never raises, never kills.

    An unanswerable question resolves to "not alive", because that is the safe
    direction: the caller reacts by respawning or pruning, and a duplicate
    poller is visible and cheap while a poller everyone believes is running is
    silent blindness.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            return pid_alive_windows(pid)
        except (OSError, AttributeError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True
