#!/usr/bin/env python3





































from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import time






_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _env import env_int, env_float  
import _untrusted  
from _spawnable import which_excluding_cwd  











_GIT_TIMEOUT_DEFAULT = 10






TIMEOUT_RC = 124






_TERM_GRACE_S = 2


def git_timeout(default: int | None = None) -> int:






    base = _GIT_TIMEOUT_DEFAULT if default is None else default
    return env_int("SUPERTOOL_GIT_TIMEOUT", base, minimum=1)


def _settled(proc: "subprocess.Popen", grace: int) -> bool:














    try:
        proc.communicate(timeout=grace)
        return True
    except subprocess.TimeoutExpired:
        return False
    except (OSError, ValueError):



        try:
            proc.wait(timeout=grace)
            return True
        except subprocess.TimeoutExpired:
            return False
        except OSError:
            return False


def _stop(proc: "subprocess.Popen") -> None:























    try:
        proc.terminate()
    except OSError:



        pass
    if _settled(proc, _TERM_GRACE_S):
        return
    try:
        proc.kill()
    except OSError:
        pass
    if not _settled(proc, _TERM_GRACE_S):





        pass






_LOCK_ERROR_RE = re.compile(r"Unable to create '?([^'\n]+?)'?:\s*File exists")













LOCK_WAIT_DEFAULT = 2.0




_LOCK_BACKOFF = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)








_LOCK_WAIT_CEILING = 60.0


def _lock_wait_budget() -> float:
    value = env_float("SUPERTOOL_GIT_LOCK_WAIT", LOCK_WAIT_DEFAULT, minimum=0.0)
    if math.isnan(value) or value == float("inf"):  
        return LOCK_WAIT_DEFAULT
    return min(value, _LOCK_WAIT_CEILING)


def _lock_fd_holder(lock_path: str, scan_timeout: float = 3.0):






















    lsof = which_excluding_cwd("lsof")
    if lsof:
        try:
            proc = subprocess.run(
                [lsof, "-w", "-F", "p", "--", lock_path],
                capture_output=True, text=True, timeout=scan_timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return None
        except OSError:
            return None










        if proc.returncode == 0:
            return bool(proc.stdout.strip())
        if proc.returncode == 1 and not proc.stderr.strip():
            return False
        return None
    if not os.path.isdir("/proc"):



        return None
    try:
        target = os.path.realpath(lock_path)
    except OSError:
        return None
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            fd_names = os.listdir(fd_dir)
        except OSError:
            continue  
        for fd in fd_names:
            try:
                if os.path.realpath(f"{fd_dir}/{fd}") == target:
                    return True
            except OSError:
                continue
    return False


def _diagnose_lock(lock_path: str) -> str:























    try:
        age = time.time() - os.stat(lock_path).st_mtime
    except OSError:
        return ("lock-diagnosis: the lock file is gone already -- whoever "
                "held it has released it")
    age_text = f"{age:.1f}s old"
    held = _lock_fd_holder(lock_path)
    shown = _untrusted.flat(lock_path, disclose_newline=True)
    if held is True:
        return (f"lock-diagnosis: live -- a process still has {shown} "
                f"open ({age_text})")
    if held is False:
        return (f"lock-diagnosis: stale -- {shown} is {age_text} and no "
                "process has it open, so it was most likely left behind by "
                "a crash rather than held by a slow one. Not removed "
                "automatically: deleting a lock on an inference is how a "
                "live write gets corrupted instead of a stale one cleared")
    return (f"lock-diagnosis: cannot tell -- {shown} is {age_text} and "
            "whether a process still holds it open could not be established "
            "on this platform (no lsof, no /proc, or the scan did not "
            "answer) -- treat it as live until a human looks")


def _with_lock_retry(attempt):












    budget = _lock_wait_budget()
    result = attempt()
    if budget <= 0:
        return result
    deadline = time.monotonic() + budget
    step = 0
    while result.returncode != 0:
        match = _LOCK_ERROR_RE.search(result.stderr or "")
        if not match:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            diagnosis = _diagnose_lock(match.group(1))







            result.stderr = (result.stderr or "").rstrip("\n") + "\n" + diagnosis
            return result
        time.sleep(min(_LOCK_BACKOFF[min(step, len(_LOCK_BACKOFF) - 1)], remaining))
        step += 1
        result = attempt()
    return result


def _git(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:







    return _with_lock_retry(lambda: _git_attempt(args, timeout))


def _git_attempt(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:

































    budget = git_timeout() if timeout is None else timeout











    cmd = ["git", "--no-optional-locks"] + args
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        _stop(proc)
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=f"timed out after {budget}s",
        )
    except (OSError, ValueError) as exc:
        if not _settled(proc, _TERM_GRACE_S):
            _stop(proc)
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=(f"communicate() failed: {exc.__class__.__name__} - "
                   f"{str(exc) or 'no reason given'}"),
        )
    return subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr,
    )


def _git_verbatim(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:



    return _with_lock_retry(lambda: _git_verbatim_attempt(args, timeout))


def _git_verbatim_attempt(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:























    budget = git_timeout() if timeout is None else timeout


    cmd = ["git", "--no-optional-locks"] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        raw_out, raw_err = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        _stop(proc)
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=f"timed out after {budget}s",
        )
    except (OSError, ValueError) as exc:
        if not _settled(proc, _TERM_GRACE_S):
            _stop(proc)
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=(f"communicate() failed: {exc.__class__.__name__} - "
                   f"{str(exc) or 'no reason given'}"),
        )












    done = subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode, stdout=raw_out, stderr=raw_err,
    )
    return subprocess.CompletedProcess(
        args=cmd, returncode=done.returncode,
        stdout=done.stdout.decode("utf-8", "replace"),
        stderr=done.stderr.decode("utf-8", "replace"),
    )
