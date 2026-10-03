#!/usr/bin/env python3


















from __future__ import annotations

import hashlib
import os
import signal
import sys
import time
from typing import Optional


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _proc  
from _paths import (  
    list_pidfiles,
    open_runtime_dir,
    read_pid,
    socket_pid_names,
    socket_pid_paths,  
)









EXIT_OK = 0            
EXIT_USAGE = 2         
EXIT_STOP_FAILED = 3   
EXIT_REFUSED = 4       
EXIT_NO_DAEMON = 5     


def stop_pid(pid: int) -> bool:
























    if pid <= 0: return False
    try: os.kill(pid, signal.SIGTERM)
    except ProcessLookupError: return True
    except PermissionError: return False
    deadline = time.time() + 3
    while time.time() < deadline:
        if not _proc.pid_alive(pid): return True
        time.sleep(0.1)
    try: os.kill(pid, signal.SIGKILL)
    except ProcessLookupError: return True
    time.sleep(0.5)
    return not _proc.pid_alive(pid)


def stop_by_pidfile(pid_path: str, *, dir_fd: Optional[int] = None) -> tuple:































    where = pid_path if dir_fd is None else os.path.join(_runtime_hint(), pid_path)
    kw = {} if dir_fd is None else {"dir_fd": dir_fd}
    pid, reason = read_pid(pid_path, dir_fd=dir_fd)
    if reason:
        return False, f"  {where}: {reason}"
    if stop_pid(pid):

        try: os.unlink(pid_path, **kw)
        except FileNotFoundError: pass
        return True, f"  stopped pid={pid} ({where})"
    return False, f"  failed to stop pid={pid} ({where})"


_RUNTIME_HINT = [""]


def _runtime_hint() -> str:

    return _RUNTIME_HINT[0]


def _refused(exc: SystemExit) -> int:











    reason = exc.code
    if reason is None or isinstance(reason, int):
        raise exc
    sys.stderr.write(f"{reason}\n")
    return EXIT_REFUSED


def main(argv: list) -> int:







    _RUNTIME_HINT[0] = ""
    if len(argv) < 2:
        sys.stderr.write("usage: stop.py NAME | --all\n")
        return EXIT_USAGE
    if argv[1] == "--all":
        try:
            pidfiles, reason = list_pidfiles()
        except SystemExit as exc:
            return _refused(exc)
        if reason:







            sys.stderr.write(
                f"{reason}\n"
                f"Refusing to report on daemons we could not enumerate: "
                f"nothing was stopped, and this is not a statement that "
                f"nothing was running.\n"
            )
            return EXIT_REFUSED
        if not pidfiles:
            print("No daemons running.")
            return EXIT_OK
        print(f"Stopping {len(pidfiles)} daemon(s):")
        return _stop_each([os.path.basename(p) for p in pidfiles])

    name = argv[1]
    cwd = os.path.abspath(os.getcwd())
    try:
        _sock_name, pid_name = socket_pid_names(cwd, name)
        fd, base = open_runtime_dir()
    except SystemExit as exc:
        return _refused(exc)
    _RUNTIME_HINT[0] = base
    try:
        if not os.path.exists(os.path.join(base, pid_name)):
            print(f"No daemon found for '{name}' "
                  f"(expected {os.path.join(base, pid_name)})")
            return EXIT_NO_DAEMON
        print(f"Stopping daemon '{name}':")
        ok, message = stop_by_pidfile(pid_name, dir_fd=fd)
    finally:
        os.close(fd)
    if ok:
        print(message)
        return EXIT_OK
    sys.stderr.write(f"{message}\n")
    return EXIT_STOP_FAILED


def _stop_each(pid_names: list) -> int:







    try:
        fd, base = open_runtime_dir()
    except SystemExit as exc:
        return _refused(exc)
    _RUNTIME_HINT[0] = base
    failed = 0
    try:
        for pid_name in pid_names:
            ok, message = stop_by_pidfile(pid_name, dir_fd=fd)
            if ok:
                print(message)
            else:
                failed += 1
                sys.stderr.write(f"{message}\n")
    finally:
        os.close(fd)
    return EXIT_STOP_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
