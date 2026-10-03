#!/usr/bin/env python3























































from __future__ import annotations

import json
import os
import subprocess
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from refusal import absent, guard_main
from spawnable import argv0, spawnable

TOOL = "git-status"
INSTALL_HINT = ("git not found on PATH — the working-tree delta for this file "
                "was NOT measured (set $GIT_BIN if git lives elsewhere)")











GIT_TIMEOUT_DEFAULT = 15





TERM_GRACE_S = 2

TIMEOUT_ENV = "SUPERTOOL_GIT_TIMEOUT"


class _NoAnswer(Exception):










    def __init__(self, argv: "tuple[str, ...]", detail: str) -> None:
        super().__init__(" ".join(argv))
        self.argv = argv
        self.detail = detail


def _budget() -> int:











    raw = os.environ.get("SUPERTOOL_GIT_TIMEOUT")
    if raw is None:
        return GIT_TIMEOUT_DEFAULT
    try:
        value = int(raw.strip())
    except (AttributeError, TypeError, ValueError):
        return GIT_TIMEOUT_DEFAULT
    return value if value >= 1 else GIT_TIMEOUT_DEFAULT


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
    if _settled(proc, TERM_GRACE_S):
        return
    try:
        proc.kill()
    except OSError:



        pass
    if not _settled(proc, TERM_GRACE_S):





        pass


def _adapter_error(file: str, msg: str, dur_ms: int) -> dict:

    return {"tool": TOOL, "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": msg}],
            "duration_ms": dur_ms}


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def _parse_numstat(output: str) -> tuple[int, int]:

    line = output.strip()
    if not line:
        return 0, 0
    parts = line.split("\t")
    if len(parts) < 2:
        return 0, 0
    try:
        added = int(parts[0]) if parts[0] != "-" else 0
        removed = int(parts[1]) if parts[1] != "-" else 0
        return added, removed
    except ValueError:
        return 0, 0


def _parse_state(porcelain: str) -> str:

    line = porcelain.rstrip("\n")
    if not line:
        return "clean"
    if len(line) < 2:
        return "unknown"
    xy = line[:2]
    x = xy[0]  
    y = xy[1]  
    if xy == "??":
        return "untracked"
    if x != " " and x != "?" and y == " ":
        return "staged"
    if y != " " and y != "?":
        return "modified"
    if x != " " and x != "?":
        return "staged"
    return "clean"


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:






        emit(_adapter_error("", "no file arg", 0))
        return

    file = sys.argv[1]
    git_bin = os.environ.get("GIT_BIN", "git")

    if not spawnable(git_bin):


        emit(absent(TOOL, file, INSTALL_HINT, 0))
        return

    start = time.monotonic()
    budget = _budget()
    deadline = start + budget
    file_dir = str(pathlib.Path(file).resolve().parent)

    def ms() -> int:
        return int((time.monotonic() - start) * 1000)

    def run(*args: str) -> str:































        full_args = ("--no-optional-locks", *args)
        timed_out = ("timed out after " + str(budget) + "s (the whole-adapter "
                     "budget; raise $" + TIMEOUT_ENV + " if this repository is "
                     "genuinely this slow)")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _NoAnswer(full_args, timed_out)
        try:
            proc = subprocess.Popen(
                [argv0(git_bin), *full_args],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=file_dir, text=True, encoding="utf-8", errors="replace",
            )
        except OSError as exc:




            raise _NoAnswer(full_args, "could not be run: "
                            + exc.__class__.__name__ + " - "
                            + (str(exc) or "no reason given")) from exc
        try:
            out, err = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            _stop(proc)
            raise _NoAnswer(full_args, timed_out) from None
        except (OSError, ValueError) as exc:











            if not _settled(proc, TERM_GRACE_S):
                _stop(proc)
            raise _NoAnswer(full_args, "communicate() failed: "
                            + exc.__class__.__name__ + " - "
                            + (str(exc) or "no reason given")) from exc
        if proc.returncode == 129 and "--no-optional-locks" in err:













            raise _NoAnswer(full_args, (
                "rejected --no-optional-locks (exit 129): this git is "
                "older than the ~2.15 release that added the flag -- "
                "upgrade git, or point $GIT_BIN at a newer one"))
        return out

    try:


















        rev = run("rev-parse", "--is-inside-work-tree")
        if rev.strip() != "true":
            emit({
                "tool": "git-status", "file": file, "ok": True, "count": 0,
                "errors": [], "duration_ms": ms(),
                "metrics": {"lines_added": 0, "lines_removed": 0,
                            "lines_staged_added": 0, "lines_staged_removed": 0,
                            "state": "clean"},
            })
            return

        worktree_out = run("diff", "--numstat", "--", file)
        staged_out = run("diff", "--cached", "--numstat", "--", file)
        porcelain_out = run("status", "--porcelain", "--", file)
    except _NoAnswer as stall:
        emit(_adapter_error(file, (
            "`git " + " ".join(stall.argv) + "` " + stall.detail + ", so the "
            "working-tree delta for this file was NOT measured - this is a "
            "git-status failure, not a finding about the file."), ms()))
        return

    dur = ms()

    added, removed = _parse_numstat(worktree_out)
    staged_added, staged_removed = _parse_numstat(staged_out)
    state = _parse_state(porcelain_out)

    emit({
        "tool": "git-status",
        "file": file,
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": dur,
        "metrics": {
            "lines_added": added,
            "lines_removed": removed,
            "lines_staged_added": staged_added,
            "lines_staged_removed": staged_removed,
            "state": state,
        },
    })


if __name__ == "__main__":
    guard_main(TOOL, main)
