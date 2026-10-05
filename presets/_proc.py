#!/usr/bin/env python3














from __future__ import annotations

import os
import sys


WIN_STILL_ACTIVE = 259


WIN_QUERY_LIMITED_INFORMATION = 0x1000


def kernel32():

    import ctypes
    return ctypes.windll.kernel32  


def pid_alive_windows(pid: int) -> bool:

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
        return True  
    except OSError:
        return False
    return True
