#!/usr/bin/env python3














































from __future__ import annotations

import base64
import binascii
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time







sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _status_probe  

WORKFLOW_DIR = ".github/workflows"







MAX_DECLARED_WORKFLOWS = 12




_WORKFLOW_SUFFIXES = (".yml", ".yaml")




_ON_KEY = re.compile(r"""^(?:on|"on"|'on')\s*:\s*(?P<rest>.*?)\s*$""")  




_NAME_KEY = re.compile(r"""^name\s*:\s*(?P<rest>.*?)\s*$""")  

_MAP_KEY = re.compile(r"^(?P<indent>\s+)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:")
_SEQ_ITEM = re.compile(r"^(?P<indent>\s+)-\s*(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*$")  





PUSH_TRIGGERS = frozenset({"push", "workflow_run", "workflow_call"})






_COMMENT = re.compile(r"(?:^|\s)#")


def _strip_comment(text: str) -> str:
    m = _COMMENT.search(text)
    return text[:m.start()] if m else text


def _strip_quotes(text: str) -> str:
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    return t


def _scalar(raw: str) -> str:









    t = raw.strip()
    if t[:1] in ("'", '"'):
        quote = t[0]
        end = t.find(quote, 1)
        return t[1:end] if end > 0 else t[1:]
    return _strip_comment(t).strip()


def _lines(text: str) -> list[str]:





    return str(text or "").split("\n")


def parse_name(text: str, path: str) -> str:






    for line in _lines(text):
        m = _NAME_KEY.match(line)
        if m:
            value = _scalar(m.group("rest"))
            if value:
                return value
            break
    return path


def parse_triggers(text: str) -> list[str] | None:

















    lines = _lines(text)
    for i, line in enumerate(lines):
        m = _ON_KEY.match(line)
        if not m:
            continue


        rest = _strip_comment(m.group("rest")).strip()
        if rest.startswith("["):
            return _flow_sequence(rest, lines[i + 1:])
        if rest:
            return [_strip_quotes(rest)]
        return _block_keys(lines[i + 1:])
    return None





_FLOW_MAX_LINES = 20


def _flow_sequence(first: str, rest: list[str]) -> list[str] | None:


















    buf = first
    for line in rest[:_FLOW_MAX_LINES]:
        if "]" in buf:
            break
        buf += " " + _strip_comment(line).strip()
    if "]" not in buf:
        return None
    inner = buf[1:].split("]", 1)[0]
    found = [_strip_quotes(p) for p in inner.split(",")]
    found = [f for f in found if f]
    return found or None


def _block_keys(rest: list[str]) -> list[str] | None:

    found: list[str] = []
    indent: int | None = None
    for line in rest:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            break  
        m = _MAP_KEY.match(line) or _SEQ_ITEM.match(line)
        if not m:
            continue
        width = len(m.group("indent"))
        if indent is None:
            indent = width
        if width == indent and m.group("key") not in found:
            found.append(m.group("key"))
    return found or None


def _run(argv: list[str], timeout: int = 15):
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )





















API_TIMEOUT = 10
BUDGET_SECS = 45
FETCH_WORKERS = 6


def _api(path: str, timeout: int = API_TIMEOUT) -> tuple[object, str]:






    try:
        r = _run(["gh", "api", path], timeout=timeout)
    except FileNotFoundError:
        return None, "gh not found"
    except subprocess.TimeoutExpired:
        return None, "gh timed out"
    except OSError as exc:
        return None, f"gh could not be run ({type(exc).__name__})"
    if r.returncode != 0:
        stderr = (r.stderr or "").strip()
        if _status_probe.says_not_found(stderr):
            return None, "404"
        return None, f"gh failed ({stderr[:120] or 'no stderr'})"
    try:
        return json.loads(r.stdout), ""
    except json.JSONDecodeError:
        return None, "gh returned invalid JSON"


def _decode(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("encoding") or "") != "base64":
        return None
    try:
        return base64.b64decode(
            str(payload.get("content") or "")).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None


def declared_at(owner: str, repo: str, sha: str,
                workers: int = FETCH_WORKERS,
                budget_secs: int = BUDGET_SECS) -> tuple[list[dict] | None, str]:












    if not owner or not repo or not sha:
        return None, "the repository or commit could not be identified"

    started = time.monotonic()
    listing, err = _api(
        f"repos/{owner}/{repo}/contents/{WORKFLOW_DIR}?ref={sha}")
    if err == "404":
        return [], ""
    if err:
        return None, f"{WORKFLOW_DIR} could not be read at this commit: {err}"
    if not isinstance(listing, list):
        return None, f"{WORKFLOW_DIR} did not read as a directory"

    paths = [
        str(entry.get("path") or "")
        for entry in listing
        if isinstance(entry, dict)
        and str(entry.get("type") or "file") == "file"
        and str(entry.get("path") or "").endswith(_WORKFLOW_SUFFIXES)
    ]
    paths = [p for p in paths if p]
    if not paths:
        return [], ""
    if len(paths) > MAX_DECLARED_WORKFLOWS:
        return None, (f"{len(paths)} workflow files at this commit, over the "
                      f"{MAX_DECLARED_WORKFLOWS} this op will fetch in one "
                      f"render")





    elapsed = time.monotonic() - started
    if elapsed > budget_secs:
        return None, (f"reading {WORKFLOW_DIR} alone took {int(elapsed)}s of "
                      f"this op's {budget_secs}s enrichment budget, so the "
                      f"{len(paths)} workflow files were not fetched")

    def fetch(path: str) -> tuple[str, str | None]:
        payload, error = _api(f"repos/{owner}/{repo}/contents/{path}?ref={sha}")
        return path, (None if error else _decode(payload))

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(workers, len(paths)))) as pool:
        bodies = dict(pool.map(fetch, paths))

    out: list[dict] = []
    for path in paths:
        text = bodies.get(path)
        if text is None:




            out.append({"name": path, "path": path, "triggers": None})
            continue
        out.append({
            "name": parse_name(text, path),
            "path": path,
            "triggers": parse_triggers(text),
        })
    return out, ""


def is_push_triggered(triggers: object) -> bool | None:

    if triggers is None:
        return None
    return any(str(t) in PUSH_TRIGGERS for t in triggers)
