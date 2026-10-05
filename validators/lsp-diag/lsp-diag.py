#!/usr/bin/env python3












from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "common"))
from linebreaks import split_lines  
from refusal import guard_main  


def emit(obj: dict) -> None:
    print(json.dumps(obj))
















_INLINE_BREAKS = "".join(chr(c) for c in (0x2028, 0x2029, 0x0085, 0x0B, 0x0C))
_INLINE_BREAK_RE = re.compile("[" + re.escape(_INLINE_BREAKS) + "]")




_REMAINDER_MAX = 200








_ORPHAN_TOTAL_MAX = 1000




MSG_MAX = 2000



_MARKERS = "•*"






_HEADER_RE = re.compile(r"^Found\s+(\d+)\s+diagnostic\(s\)\s+for\s+.*:$")


def first_segment(line: str) -> tuple[str, str]:







    parts = _INLINE_BREAK_RE.split(line, maxsplit=1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


def carried_note(text: str, why: str) -> str:







    flat = " ".join(_INLINE_BREAK_RE.sub(" ", text).split()).strip()
    if not flat:
        return ""
    if len(flat) > _REMAINDER_MAX:
        flat = flat[:_REMAINDER_MAX] + "…"
    return f"  [{why} — not a second diagnostic and not a location: {flat}]"


def remainder_note(rest: str) -> str:






    return carried_note(rest, "rest of the same line, after an inline line break")


def _cap(text: str, limit: int) -> str:

    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def _fold_orphans(orphans: list[str]) -> str:

    kept: list[str] = []
    total = 0
    for i, note in enumerate(orphans):
        if total + len(note) > _ORPHAN_TOTAL_MAX:
            hidden = len(orphans) - i
            kept.append(f"  [+{hidden} further fragment(s) from this file's "
                        "lines are not shown]")
            break
        kept.append(note)
        total += len(note)
    return "".join(kept)


def _framing(lines: list[str]) -> bool:









    for physical in lines:
        head = first_segment(physical)[0].strip()
        if not head or _HEADER_RE.match(head):
            continue
        return head[:1] in _MARKERS
    return False


def header_count(lines: list[str]) -> int | None:





























    if not lines:
        return None
    m = _HEADER_RE.match(first_segment(lines[0])[0].strip())
    return int(m.group(1)) if m else None


def _reconcile(claimed: int | None, produced: int) -> dict | None:






















    if claimed is None or claimed == produced:
        return None
    if claimed > produced:
        why = (f"parsed {produced} of the {claimed} diagnostic(s) this server "
               "said it was listing — findings may be missing from this "
               "receipt, so its count is a floor and not a measurement")
    else:
        why = (f"built {produced} record(s) from output that said it was "
               f"listing {claimed} — records here were not framed by the "
               "server and may not be its findings")
    return {"line": None, "col": None, "severity": "warning",
            "code": "adapter", "msg": "count reconciliation: " + why}


def parse_cclsp_diagnostics(text: str, file: str) -> list[dict]:








    physicals = split_lines(text)



    claimed = header_count(physicals)

    if "No diagnostics found" in text or "no errors, warnings, or hints" in text.lower():






        note = _reconcile(claimed, 0)
        return [note] if note else []

    errors: list[dict] = []





    orphans: list[str] = []







    marked = _framing(physicals)
    for physical in physicals:




        head, rest = first_segment(physical)
        line = head.strip()
        note = remainder_note(rest)
        if not line:



            if note:
                orphans.append(note)
            continue
        if _HEADER_RE.match(line):



            if note:
                orphans.append(note)
            continue
        if marked and line[:1] not in _MARKERS:




            orphans.append(carried_note(
                physical, "a continuation of the line above, after a line "
                          "break inside the server's message"))
            continue

        m = re.search(r"\[?\b(error|warning|info|hint)\b\]?\s*[:\s]*(.+?)(?:\s+at\s+line\s+(\d+)(?:[,\s]+(?:col(?:umn)?\s+)?(\d+))?|\s+(\d+):(\d+))\s*$",
                      line, flags=re.IGNORECASE)
        if m:
            severity = m.group(1).lower()
            msg = m.group(2).strip(" •-:")
            ln = int(m.group(3)) if m.group(3) else (int(m.group(5)) if m.group(5) else None)
            col = int(m.group(4)) if m.group(4) else (int(m.group(6)) if m.group(6) else None)
            errors.append({"line": ln, "col": col, "severity": severity,
                           "code": "lsp", "msg": msg + note})
            continue

        m = re.match(r"^(\d+):(\d+):\s*(\w+):\s*(.+)$", line)
        if m:
            errors.append({"line": int(m.group(1)), "col": int(m.group(2)),
                           "severity": m.group(3).lower(),
                           "code": "lsp", "msg": m.group(4).strip() + note})
            continue





        orphans.append(carried_note(
            physical, "a line the server sent that parses as no diagnostic"))
    if orphans and errors:





        errors[-1]["msg"] += _fold_orphans(orphans)
    for err in errors:



        err["msg"] = _cap(err["msg"], MSG_MAX)



    produced = len(errors)
    if not errors:

        if not text.startswith("diag:"):  
            errors.append({"line": None, "col": None, "severity": "info",
                           "code": "lsp", "msg": text.strip()[:500]})
    note = _reconcile(claimed, produced)
    if note:





        errors.append(note)
    return errors


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "lsp-diag", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()


    supertool_bin = os.environ.get("SUPERTOOL_BIN")
    if not supertool_bin:


        guess = os.path.join(os.path.dirname(__file__), "..", "..", "supertool.py")
        if os.path.isfile(guess):
            supertool_bin = guess
        else:
            supertool_bin = "supertool"










    diag_timeout = float(os.environ.get("SUPERTOOL_LSP_DIAG_TIMEOUT", "30"))
    try:
        r = subprocess.run(
            [sys.executable, supertool_bin, f"diag:{file}"] if supertool_bin.endswith(".py")
            else [supertool_bin, f"diag:{file}"],
            capture_output=True, text=True, timeout=diag_timeout, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        emit({"tool": "lsp-diag", "file": file, "duration_ms": int((time.time() - start) * 1000),
              "skipped": "supertool binary not found"})
        return
    except subprocess.TimeoutExpired:






        emit({"tool": "lsp-diag", "file": file, "duration_ms": int((time.time() - start) * 1000),
              "skipped": f"timed out waiting {diag_timeout:g}s for a diagnostics "
                         "answer -- not a finding about this file, the request "
                         "never came back"})
        return


    text = r.stdout
    text = re.sub(r"^---\s*diag:[^\n]*---\s*\n", "", text, count=1)
    ms = int((time.time() - start) * 1000)

















    infra = next((seg for ln in split_lines(text)
                  for seg in [first_segment(ln)[0].strip()]
                  if seg.startswith("diag:")), None)
    if infra:
        emit({"tool": "lsp-diag", "file": file, "duration_ms": ms,
              "skipped": infra[len("diag:"):].strip() or "no answer from LSP"})
        return












    if (os.environ.get("SUPERTOOL_LSP_DOC_MAYBE_STALE") == "1"
            and os.environ.get("SUPERTOOL_LSP_RESYNC_ON_QUERY") != "1"):
        emit({"tool": "lsp-diag", "file": file, "duration_ms": ms,
              "skipped": "stale document — warm LSP daemon answers from its "
                         "pre-edit copy, not from disk"})
        return

    errors = parse_cclsp_diagnostics(text, file)


    emit({"tool": "lsp-diag", "file": file,
          "ok": all(e.get("severity") != "error" for e in errors),
          "count": len(errors), "errors": errors,
          "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    guard_main("lsp-diag", main)
