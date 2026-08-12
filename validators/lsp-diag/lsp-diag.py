#!/usr/bin/env python3
"""LSP-diag validator adapter.

Calls `supertool diag:FILE` and converts the text output to a validators/SCHEMA.md
JSON receipt. Reuses the long-lived MCP daemon — sub-second when warm.

Why not call MCP directly: the supertool dispatch handles config lookup, daemon
auto-spawn, and tool routing. Reusing it keeps the validator dumb.

Usage:  lsp-diag.py <file>
Output: one JSON object on stdout.
Exit:   0 (always).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "common"))
from linebreaks import split_lines  # noqa: E402


def emit(obj: dict) -> None:
    print(json.dumps(obj))


#: The five boundaries `str.splitlines()` breaks on and LF/CR/CRLF framing does
#: not (#1486). They are deliberately NOT line ends here — which is exactly why
#: a fragment after one of them lands on the *same* line as a real diagnostic,
#: and pattern 1 below anchors its `at line N, col M` tail to the end of the
#: line. Whichever tail is last wins; the fragment's is last (#1500).
#:
#: All five are also `str.isspace()`, so `str.strip()` deletes a leading one —
#: which is a second forgery, not the same one: the probe in `main()` reads
#: `ln.strip().startswith("diag:")`, so a fragment at the start of a physical
#: line still imitated supertool own message prefix and turned the whole
#: receipt into `skipped`, erasing every real finding (#482 shape).
#:
#: Spelled with `chr()` rather than inline: five invisible characters inside a
#: character class is a line no reviewer can check by eye.
_INLINE_BREAKS = "".join(chr(c) for c in (0x2028, 0x2029, 0x0085, 0x0B, 0x0C))
_INLINE_BREAK_RE = re.compile("[" + re.escape(_INLINE_BREAKS) + "]")

#: Characters of an unparsed remainder carried into the message. Enough to see
#: what arrived; a record must not grow without bound because the file under
#: validation happens to contain one of the five.
_REMAINDER_MAX = 200


def first_segment(line: str) -> tuple[str, str]:
    """`(what the server framed, the rest of the same line)`.

    Split before any `strip()`: a leading inline break is whitespace to
    `str.strip()`, so stripping first hands the whole line to the fragment.
    An empty first element means nothing preceded the break, and so nothing the
    server framed as a diagnostic is on that line.
    """
    parts = _INLINE_BREAK_RE.split(line, maxsplit=1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


def remainder_note(rest: str) -> str:
    """The rest of a line, labelled as the rest of a line.

    Not dropped — a message may legitimately contain one of the five, and text
    the server sent is evidence. Not parsed either: its trailing `at line N,
    col M` is the file under validation choosing a location, which is #1500.
    """
    flat = " ".join(_INLINE_BREAK_RE.sub(" ", rest).split()).strip()
    if not flat:
        return ""
    if len(flat) > _REMAINDER_MAX:
        flat = flat[:_REMAINDER_MAX] + "…"
    return ("  [rest of the same line, after an inline line break — "
            f"not a second diagnostic and not a location: {flat}]")


def parse_cclsp_diagnostics(text: str, file: str) -> list[dict]:
    """Parse cclsp's get_diagnostics text output into the SCHEMA error shape.

    cclsp shapes observed:
      "No diagnostics found for /path. The file has no errors, warnings, or hints."
      "Found N diagnostic(s) for /path:\\n• [severity] message at line X, col Y"
      "[error] message at line X:Y"   (some variants)
    Falls back to one generic error if format doesn't match.
    """
    if "No diagnostics found" in text or "no errors, warnings, or hints" in text.lower():
        return []

    errors: list[dict] = []
    #: Remainders from lines that produced no record of their own. Kept, because
    #: an absence produced by the tool is the defect this fix is about and the
    #: first cut of it dropped them silently; attached to a record rather than
    #: becoming one, because `count` is what `_validator_regressed` subtracts
    #: and a fragment minting a record is #1486.
    orphans: list[str] = []
    for physical in split_lines(text):
        # Only the first segment is parsed, and the remainder is disclosed
        # rather than dropped (#1500). Everything after an inline break is the
        # file under validation talking, and its own trailing `at line N` would
        # otherwise win the end anchor below.
        head, rest = first_segment(physical)
        line = head.strip()
        note = remainder_note(rest)
        if not line:
            # Nothing the server framed precedes the break, so there is no
            # diagnostic to place here and the fragment must not become one.
            # It is still text the tool sent, so it is carried, not dropped.
            if note:
                orphans.append(note)
            continue
        # Pattern 1: "• [severity] message at line X, col Y" or "at X:Y"
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
        # Pattern 2: standalone "X:Y: severity: message"
        m = re.match(r"^(\d+):(\d+):\s*(\w+):\s*(.+)$", line)
        if m:
            errors.append({"line": int(m.group(1)), "col": int(m.group(2)),
                           "severity": m.group(3).lower(),
                           "code": "lsp", "msg": m.group(4).strip() + note})
    if orphans and errors:
        # On the last record, not on the one each orphan happened to follow: the
        # label already says the text is neither a location nor a finding, and
        # threading provenance per record buys nothing a reader would act on.
        # With no record at all they fall through to the advisory below, which
        # carries the whole text verbatim.
        errors[-1]["msg"] += "".join(orphans)
    if not errors:
        # Unrecognized but non-empty → keep the text as a single advisory
        if not text.startswith("diag:"):  # ignore supertool's own error messages
            errors.append({"line": None, "col": None, "severity": "info",
                           "code": "lsp", "msg": text.strip()[:500]})
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

    # Find the supertool binary — env override > sibling of this script > PATH
    supertool_bin = os.environ.get("SUPERTOOL_BIN")
    if not supertool_bin:
        # Validators live at supertool_dir/validators/lsp-diag/lsp-diag.py
        # supertool.py is at supertool_dir/supertool.py
        guess = os.path.join(os.path.dirname(__file__), "..", "..", "supertool.py")
        if os.path.isfile(guess):
            supertool_bin = guess
        else:
            supertool_bin = "supertool"

    try:
        r = subprocess.run(
            [sys.executable, supertool_bin, f"diag:{file}"] if supertool_bin.endswith(".py")
            else [supertool_bin, f"diag:{file}"],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        emit({"tool": "lsp-diag", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "supertool binary not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "lsp-diag", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 30000})
        return

    # Strip supertool's "--- diag:FILE ---" header line
    text = r.stdout
    text = re.sub(r"^---\s*diag:[^\n]*---\s*\n", "", text, count=1)
    ms = int((time.time() - start) * 1000)

    # supertool prefixes its own messages with the op name (#346) so adapters
    # drop them instead of counting them as findings: "no LSP configured",
    # "MCP server 'X' unavailable" (routine since #488 stopped short-budget
    # validators spawning daemons), "MCP error: ...", "no result from ...",
    # "orchestrator timeout after Ns". None of them is a verdict.
    #
    # This used to emit ok/count/errors ALONGSIDE the skip, and only for two of
    # those phrasings; the rest fell through to the parser, which dropped them
    # by the same op_name guard and left `{"ok": true, "count": 0}` — an
    # infrastructure failure rendered as a pass (#482). The receipt is now a
    # pure skip: nothing for a consumer keying off `ok` to misread.
    # `first_segment` before `strip`, not `strip` alone: all five inline breaks
    # are `str.isspace()`, so a leading one is stripped away and the fragment
    # behind it reaches `startswith("diag:")`. #1486 stopped them ending a line
    # and this is the other half — a forged skip discards every finding on the
    # file, so the file under validation must not be able to reach it.
    infra = next((seg for ln in split_lines(text)
                  for seg in [first_segment(ln)[0].strip()]
                  if seg.startswith("diag:")), None)
    if infra:
        emit({"tool": "lsp-diag", "file": file, "duration_ms": ms,
              "skipped": infra[len("diag:"):].strip() or "no answer from LSP"})
        return

    # Staleness (#482). cclsp serves get_diagnostics from a per-URI cache that
    # publishDiagnostics fills and nothing ever invalidates, and its
    # ensureFileOpen returns early when the file is already open — so the
    # daemon never re-reads from disk for the rest of its life. The framework's
    # own pre-edit baseline pass is what opens the document, which makes the
    # after-check stale on exactly the file just edited: clean when the edit
    # broke the file, or the previous version's errors at the previous
    # version's line/col. Neither describes the bytes on disk, so neither is
    # reported. SUPERTOOL_LSP_RESYNC_ON_QUERY=1 opts a server back in — set it
    # via the validator spec's `env` block when the LSP behind `diag:` re-reads
    # the file on every query rather than caching it forever.
    if (os.environ.get("SUPERTOOL_LSP_DOC_MAYBE_STALE") == "1"
            and os.environ.get("SUPERTOOL_LSP_RESYNC_ON_QUERY") != "1"):
        emit({"tool": "lsp-diag", "file": file, "duration_ms": ms,
              "skipped": "stale document — warm LSP daemon answers from its "
                         "pre-edit copy, not from disk"})
        return

    errors = parse_cclsp_diagnostics(text, file)
    # rollback_on_fail is false for this validator — even with errors we report ok=True
    # so the edit isn't rolled back. The framework sees count>0 as advisory only.
    emit({"tool": "lsp-diag", "file": file,
          "ok": all(e.get("severity") != "error" for e in errors),
          "count": len(errors), "errors": errors,
          "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    main()
