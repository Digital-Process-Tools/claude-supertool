










from __future__ import annotations

import json
import os
import socket
import sys
import time
import traceback














REFUSAL_PATTERNS = (
    "--paths allowlist",
    "outside the configured",
    "no files found to analyse",
    "no files found to analyze",
    "no files found to check",
)


class DaemonUnavailable(RuntimeError):
    pass



















def daemon_transport_reason(has_uds: bool | None = None) -> str | None:




































    if has_uds is None:
        has_uds = hasattr(socket, "AF_UNIX")
    if has_uds:
        return None
    return ("warm daemon needs socket.AF_UNIX, which this Python build does not "
            "expose (Windows) — warm validators cannot run here; cold "
            "validators such as phplint are unaffected")


def is_refusal(msg: str, *, extra_patterns: "str | None" = "") -> bool:



















    patterns = list(REFUSAL_PATTERNS)
    if extra_patterns:
        patterns += [p.strip().lower()
                     for p in extra_patterns.split(",") if p.strip()]
    lowered = (msg or "").lower()
    return any(p in lowered for p in patterns)


def outside_roots(file_path: str, env_var: str, raw: "str | None") -> str | None:

































    raw = raw or ""
    if not raw.strip():
        return None
    entries = [e.strip() for part in raw.split(os.pathsep) for e in part.split(",")]
    roots = [os.path.normcase(os.path.abspath(e)) for e in entries if e]
    if not roots:
        return None
    target = os.path.normcase(os.path.abspath(file_path))
    for root in roots:
        if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
            return None
    return f"path outside {env_var} allowlist"


def tool_fault(tool: str, returncode: int, output: str, limit: int = 300) -> str:




















    body = " ".join((output or "").split())
    if not body:
        body = "(no output)"
    elif len(body) > limit:
        body = body[:limit] + f"... (+{len(body) - limit} chars)"
    return (f"{tool} exited {returncode} without reporting anything about the "
            f"file - this is a {tool} failure, not a finding about the file: "
            f"{body}")




REQUIRE_VAR = "SUPERTOOL_REQUIRE_VALIDATORS"


def required(tool: str) -> bool:
























    raw = os.environ.get("SUPERTOOL_REQUIRE_VALIDATORS", "")
    if not raw.strip():
        return False
    names = [n.strip().lower()
             for part in raw.split(os.pathsep) for n in part.split(",")]
    return "*" in names or tool.lower() in names


def required_but_absent(tool: str, reason: str) -> str:

    return (f"{tool} is named in ${REQUIRE_VAR} but could not run, so this "
            f"file was NOT checked: {reason}")


def absent(tool: str, file_path: str, reason: str, dur_ms: int) -> dict:

































    if required(tool):
        return {"tool": tool, "file": file_path, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "adapter",
                            "msg": required_but_absent(tool, reason)}],
                "duration_ms": dur_ms}
    return skipped(tool, file_path, reason, dur_ms)


def skipped(tool: str, file_path: str, reason: str, dur_ms: int) -> dict:




















    return {"tool": tool, "file": file_path, "duration_ms": dur_ms,
            "skipped": reason}







CRASH_MSG_LIMIT = 1000


def crashed(tool: str, file_path: str, exc: BaseException, dur_ms: int) -> dict:








































    frames = traceback.format_exception(type(exc), exc, exc.__traceback__)






    lines = [ln for ln in "".join(frames).splitlines()
             if ln.strip()
             and set(ln.strip()) - set("^~")
             and not (ln.strip().startswith("...<")
                      and ln.strip().endswith(">..."))]
    tail = " | ".join(lines[-3:])
    detail = str(exc).strip()
    msg = "{0} adapter crashed and did NOT check this file: {1}{2} | trace: {3}".format(
        tool, type(exc).__name__, ": " + detail if detail else "", tail)
    return {"tool": tool, "file": file_path, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": " ".join(msg.split())[:CRASH_MSG_LIMIT]}],
            "duration_ms": dur_ms}


def guard_main(tool: str, main, *args) -> int:




































    started = time.monotonic()
    try:
        return main(*args)
    except Exception as exc:  
        target = sys.argv[1] if len(sys.argv) > 1 else ""
        print(json.dumps(crashed(tool, target, exc,
                                 int((time.monotonic() - started) * 1000))))
        return 0
