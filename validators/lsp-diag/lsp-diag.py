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

#: Characters of ONE unparsed fragment carried into the message. Enough to see
#: what arrived; a record must not grow without bound because the file under
#: validation happens to contain one of the five.
_REMAINDER_MAX = 200

#: And a ceiling on the SUM of them (#1520). `_REMAINDER_MAX` caps each
#: fragment and its comment above states the invariant, but nothing capped how
#: many fragments one record collects: a file whose echoed source carries an
#: inline break on 500 physical lines grew a single `msg` past 100kB, so the
#: comment asserted a bound the code did not hold. What is over the ceiling is
#: reported as a number, never silently absent — a capped list read as a whole
#: list is this repo's house defect in miniature.
_ORPHAN_TOTAL_MAX = 1000

#: The hard ceiling on any one record's `msg`, applied last so the invariant is
#: a property of the receipt rather than of the path that happened to build it.
#: A cut is disclosed with the same ellipsis `_REMAINDER_MAX` uses.
MSG_MAX = 2000

#: The list markers cclsp renders `get_diagnostics` with. See `_framing` for why
#: presence alone does not decide anything.
_MARKERS = "•*"

#: cclsp's own list header. Framing — neither a diagnostic nor message text — so
#: it is dropped rather than carried, and it must not be counted (#1520). Its
#: number is captured because it is the server's own statement of how many
#: entries it is about to print, and `header_count` reconciles it against the
#: records this parser actually built (#1537).
_HEADER_RE = re.compile(r"^Found\s+(\d+)\s+diagnostic\(s\)\s+for\s+.*:$")


def first_segment(line: str) -> tuple[str, str]:
    """`(what the server framed, the rest of the same line)`.

    Split before any `strip()`: a leading inline break is whitespace to
    `str.strip()`, so stripping first hands the whole line to the fragment.
    An empty first element means nothing preceded the break, and so nothing the
    server framed as a diagnostic is on that line.
    """
    parts = _INLINE_BREAK_RE.split(line, maxsplit=1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


def carried_note(text: str, why: str) -> str:
    """Text the server sent that this parser refused to read as a diagnostic.

    One label shape for all three refusals — a remainder after an inline break,
    a continuation after a line break inside a message, and a line that parsed
    as nothing — because they are the same statement: kept as evidence, not read
    as a second finding, and not read as a location.
    """
    flat = " ".join(_INLINE_BREAK_RE.sub(" ", text).split()).strip()
    if not flat:
        return ""
    if len(flat) > _REMAINDER_MAX:
        flat = flat[:_REMAINDER_MAX] + "…"
    return f"  [{why} — not a second diagnostic and not a location: {flat}]"


def remainder_note(rest: str) -> str:
    """The rest of a line, labelled as the rest of a line.

    Not dropped — a message may legitimately contain one of the five, and text
    the server sent is evidence. Not parsed either: its trailing `at line N,
    col M` is the file under validation choosing a location, which is #1500.
    """
    return carried_note(rest, "rest of the same line, after an inline line break")


def _cap(text: str, limit: int) -> str:
    """`text`, never longer than `limit`, and saying so when it was cut."""
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def _fold_orphans(orphans: list[str]) -> str:
    """The kept fragments, bounded, with the cut named rather than implied."""
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
    """Does this server frame each diagnostic with a list marker? (#1520)

    Decided from the FIRST line that carries anything the server framed, never
    from any line: a document already in the bulletless variant can contain an
    echoed `*` in its source, and letting that flip the answer would turn every
    real finding into a continuation — the loud bug traded for the quiet one.
    A marker cannot appear before the first diagnostic, so the first framed line
    is the one place the file under validation cannot have reached yet.
    """
    for physical in lines:
        head = first_segment(physical)[0].strip()
        if not head or _HEADER_RE.match(head):
            continue
        return head[:1] in _MARKERS
    return False


def header_count(lines: list[str]) -> int | None:
    """`N` from the server's own list header, or None if it sent no header.

    Read from the FIRST line carrying anything framed, never from any line —
    the same discipline `_framing` uses and for the same reason. The real
    header precedes every diagnostic, so it is the one position the file under
    validation cannot get in front of the server. A `Found 9 diagnostic(s) for
    x:` echoed out of a source file would otherwise choose the number this
    parser reconciles against, and a forged N is a forged verdict.

    None is "no number", which is a different fact from "the numbers agree" —
    `_reconcile` keeps them apart rather than letting an absent header read as
    a match.
    """
    for physical in lines:
        head = first_segment(physical)[0].strip()
        if not head:
            continue
        m = _HEADER_RE.match(head)
        return int(m.group(1)) if m else None
    return None


def _reconcile(claimed: int | None, produced: int) -> dict | None:
    """The server's count against the records this parser built, or None (#1537).

    Three states, not two. No header is not agreement and not a finding: there
    is nothing to compare, so the receipt says nothing rather than implying the
    two numbers matched. Agreement is silent too — a disclosure that fires on
    every run is one nobody reads.

    A disagreement is a **disclosure, never a `skipped`**. A skip discards every
    record beside it, and when the server framed 3 entries and 2 parsed, those 2
    are measurements of the file that were correctly made; throwing them away to
    say "I do not know" is the loud bug traded for the quiet one, and it is the
    #482 shape this adapter has already been bitten by once.

    Severity is `warning` in both directions, so this record can never be the
    reason `ok` is false. That matters structurally: `_validator_not_checked`
    fires on `ok: false` with *every* error `code: "adapter"` and renders the
    whole run `NOT CHECKED`. A note added to say the receipt may be short must
    not be able to erase the receipt.

    `code: "adapter"` because it is this parser reporting on its own parse, not
    a finding about the file — which is also why it carries no line or col.
    """
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
    """Parse cclsp's get_diagnostics text output into the SCHEMA error shape.

    cclsp shapes observed:
      "No diagnostics found for /path. The file has no errors, warnings, or hints."
      "Found N diagnostic(s) for /path:\\n• [severity] message at line X, col Y"
      "[error] message at line X:Y"   (some variants)
    Falls back to one generic error if format doesn't match.
    """
    physicals = split_lines(text)
    #: Read before anything else can return, because the clean-answer arm below
    #: is reachable from the file's own bytes and this is the one number that
    #: contradicts it (#1537).
    claimed = header_count(physicals)

    if "No diagnostics found" in text or "no errors, warnings, or hints" in text.lower():
        # Matched as a substring of the WHOLE text, so a diagnostic *message*
        # quoting either phrase returns [] for the entire file — the file under
        # validation choosing a clean verdict, the #482 shape reached from the
        # inside. The server's own header cannot appear in genuinely clean
        # output, so when one is there claiming entries, the two statements
        # disagree and the receipt says so instead of rendering the file clean.
        note = _reconcile(claimed, 0)
        return [note] if note else []

    errors: list[dict] = []
    #: Remainders from lines that produced no record of their own. Kept, because
    #: an absence produced by the tool is the defect this fix is about and the
    #: first cut of it dropped them silently; attached to a record rather than
    #: becoming one, because `count` is what `_validator_regressed` subtracts
    #: and a fragment minting a record is #1486.
    orphans: list[str] = []
    #: #1500 bounded the parse to the first segment of a line, where a segment
    #: ends at one of the five INLINE breaks. LF is not one of them by
    #: construction — it is the framing character — so a server message
    #: carrying an LF still becomes several physical lines and every line after
    #: the first was parsed as a fresh diagnostic at a location the message
    #: chose (#1520). Where the server marks its list, an unmarked line is
    #: inside the entry above it and cannot be an entry of its own.
    marked = _framing(physicals)
    for physical in physicals:
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
        if _HEADER_RE.match(line):
            # The server's own list header. Not a diagnostic, and not message
            # text either, so it is the one thing here that is dropped rather
            # than carried — carrying it would paste framing into a `msg`.
            if note:
                orphans.append(note)
            continue
        if marked and line[:1] not in _MARKERS:
            # A continuation of the entry above. Kept and labelled, never
            # parsed: `count` is what `_validator_regressed` subtracts and its
            # trailing `at line N, col M` is the file under validation choosing
            # a location.
            orphans.append(carried_note(
                physical, "a continuation of the line above, after a line "
                          "break inside the server's message"))
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
            continue
        # A non-empty head matching neither pattern used to fall off the end of
        # the loop body, taking its already-computed remainder with it — and the
        # whole-text fallback below only runs when NOTHING matched, so with any
        # other line holding a record both halves left no trace in the receipt
        # (#1520). `remainder_note`'s own docstring said "Not dropped".
        orphans.append(carried_note(
            physical, "a line the server sent that parses as no diagnostic"))
    if orphans and errors:
        # On the last record, not on the one each orphan happened to follow: the
        # label already says the text is neither a location nor a finding, and
        # threading provenance per record buys nothing a reader would act on.
        # With no record at all they fall through to the advisory below, which
        # carries the whole text verbatim.
        errors[-1]["msg"] += _fold_orphans(orphans)
    for err in errors:
        # Last, so the ceiling is a property of the receipt and not of whichever
        # path built the message. A server message is the server's, and it can
        # be any length.
        err["msg"] = _cap(err["msg"], MSG_MAX)
    #: Counted here, before the fallback advisory below: that advisory is this
    #: adapter's own text, not a diagnostic the server framed, and reconciling
    #: against it would report `parsed 1 of the 3` for a parse that read none.
    produced = len(errors)
    if not errors:
        # Unrecognized but non-empty → keep the text as a single advisory
        if not text.startswith("diag:"):  # ignore supertool's own error messages
            errors.append({"line": None, "col": None, "severity": "info",
                           "code": "lsp", "msg": text.strip()[:500]})
    note = _reconcile(claimed, produced)
    if note:
        # A record rather than a suffix on someone else's `msg`, which is where
        # the orphan fragments go. Those are text the server sent about the
        # file; this is a statement about the receipt, it has to survive when
        # there is no other record to hang it on, and a consumer keying on
        # `code` has to be able to find it.
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
