#!/usr/bin/env python3
"""tsc-check validator adapter — TypeScript syntax check via tsc --noEmit.

Requires tsc on PATH. Absent, this reports the third state — `skipped` with the
reason — rather than the `ok: true` it emitted until #1202, which was a clean
verdict about a file nothing type-checked. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

**`--pretty false` is load-bearing, not tidiness (#1499).** TypeScript 5.x
defaults to pretty output whether or not stdout is a tty: ANSI-coloured, four
lines per diagnostic, a caret rule under the offending column, and a trailing
`Found N errors` tally. Its shape is `file:line:col - error TSxxxx: msg`, which
is not the `file(line,col): error TSxxxx: msg` the parse below reads — so
stripping the colour off it would not make it parseable. Only asking for the
plain form does. The strip is still applied, because it is `--pretty false` that
happens to remove the colour today and nothing tsc documents guarantees the two
stay coupled; a `tsc` shim, or a future default, can colour the plain form too.

**Two things about paths, both #1519.** The target is contained before it
reaches argv — `tsc` reads a leading `@` as a response file and a leading `-` as
an option, so `--noEmit` did not survive an `@`-named target and files were
written. And `tsc --noEmit FILE` type-checks the whole import graph, so a
diagnostic about an imported file is the common case; the reported path is
compared against the target rather than discarded, and a foreign one keeps the
finding while losing the location. `contained_target` and `parse_diagnostics`
below carry the measurements.

Usage:  tsc-check.py <file>
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from pkg_paths import attribute
from refusal import absent
from linebreaks import split_lines

TOOL = "tsc-check"
INSTALL_HINT = ("tsc not found on PATH — this file was NOT type-checked "
                "(`npm install -g typescript`)")


# Budget for the one tool spawn below. A module constant rather than a literal
# in the call so the decline can name it: a caller reading "timeout" cannot
# tell a hung compiler from a busy machine, and the number is the first thing
# they need to decide which (#658).
TIMEOUT_S = 30

# CSI sequences, OSC strings (BEL- or ST-terminated) and the two-character
# escapes. Not just SGR colour: an OSC that retitles the reader's terminal is
# exactly the kind of thing an adapter must not republish into a `msg`.
# Each complete form is followed by its incomplete one — a CSI or OSC with no
# terminator, and last a lone ESC — which is what a stream cut mid-sequence
# leaves behind. Without them "no escape survives" is not the invariant it reads
# as. **The order carries weight in both directions.** Complete before
# incomplete, so a terminated sequence is consumed whole. And the unterminated
# OSC before the two-character escapes, because `]` is 0x5D and therefore inside
# `[@-Z\\-_]`: with that class first, `\x1b]0;title` lost its two-byte
# introducer and published `0;title` as text.
ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\[[0-?]*[ -/]*"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)|\][^\x07\x1b]*"
    r"|[@-Z\\-_]|)"
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def contained_target(file: str) -> str:
    """The target, spelled so `tsc` can only read it as a path (#1519).

    `tsc` reads a leading `@` as a **response file** — its whole command line
    comes from that file instead — and a leading `-` as an option. Neither is
    escapable, because neither is a shell question: `subprocess.run` passes the
    argument through untouched and `tsc` itself decides what it means. Measured
    on real tsc 6.0.3 with `@r.ts` on disk beside an `r.ts` holding
    `--noEmit false --outDir out`: the receipt read `{"ok": true, "count": 0}`
    for a file whose one line is a type error, and `out/a.js` and `out/b.js`
    were written — against `docs/validators.md`'s "`--noEmit` — no output files
    written". A clean verdict about a file nothing checked, plus a write.

    `--` is not the fix and was measured too: tsc answers
    `error TS5023: Unknown compiler option '--'`. A relative prefix is, and it
    goes through `os.path.join` rather than a literal `./` so Windows gets its
    own separator; tsc normalises either back to the same path, and the
    diagnostic it prints is unchanged (`@r.ts(1,7): error TS2322: ...`), which
    is what keeps `parse_diagnostics` below able to attribute it.

    An absolute path is already unambiguous and is left alone — prefixing one
    would name nothing.
    """
    if not file or os.path.isabs(file) or file[0] not in "@-":
        return file
    return os.path.join(os.curdir, file)


def _elsewhere(reported: str, ln: str, col: str, severity: str,
               code: str, msg: str) -> dict:
    """A diagnostic about another file in the same program.

    Not filtered out: `tsc --noEmit FILE` type-checks the whole import graph, so
    the program genuinely does not compile and a caller told nothing cannot act
    on it. Only the attribution changes — `validators/SCHEMA.md` §"A located
    diagnostic still has to be about *this* file (#754)".
    """
    return {"line": None, "col": None, "severity": severity, "code": "adapter",
            "msg": f"in {reported}({ln},{col}) (another file in this program): "
                   f"{code}: {msg}"}


def _unplaceable(reported: str, ln: str, col: str, severity: str,
                 code: str, msg: str) -> dict:
    """A diagnostic whose path resolves to no file this adapter can name.

    Distinct from `_elsewhere` on purpose: "not this file" and "no way to tell
    which file" are different sentences, and only the first is entitled to say
    another file is at fault.
    """
    return {"line": None, "col": None, "severity": severity, "code": "adapter",
            "msg": f"tsc reported {reported}({ln},{col}) — this adapter could "
                   f"not tell whether that is the file under validation: "
                   f"{code}: {msg}"}


# "file(line,col): error TSxxxx: message" — the shape `--pretty false` is what
# guarantees. The path is CAPTURED rather than skipped past: it used to be
# `(?:.*?)`, thrown away, so an imported file's diagnostic was published as this
# file's with `context_fields(file, ln)` printing this file's source at that
# file's line as its evidence (#1519).
DIAG_RE = re.compile(
    r"^(?P<path>.*?)\((?P<line>\d+),(?P<col>\d+)\):\s+"
    r"(?P<severity>\w+)\s+(?P<code>TS\d+):\s+(?P<msg>.+)$")


def parse_diagnostics(output: str, file: str, base: str) -> list:
    """tsc's plain dump into SCHEMA error objects, attributed.

    `base` is the directory the adapter ran `tsc` in — tsc prints relative to
    its own working directory, and the adapter chose it, so the base is known
    rather than inferred (`validators/SCHEMA.md`, the first of its two cases).
    """
    errors = []
    for line in split_lines(output):
        m = DIAG_RE.match(line)
        if not m:
            continue
        reported = m.group("path").strip()
        ln, col = m.group("line"), m.group("col")
        code, msg = m.group("code"), m.group("msg").strip()[:300]
        severity = (m.group("severity")
                    if m.group("severity") in ("error", "warning") else "error")
        where = attribute(reported, target=file, base=base)
        if where == "this":
            err = {"line": int(ln), "col": int(col), "severity": severity,
                   "code": code, "msg": msg}
            err.update(context_fields(file, int(ln)))
        elif where == "other":
            err = _elsewhere(reported, ln, col, severity, code, msg)
        else:
            err = _unplaceable(reported, ln, col, severity, code, msg)
        errors.append(err)
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "tsc-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("tsc"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            ["tsc", "--noEmit", "--skipLibCheck", "--pretty", "false",
             contained_target(file)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "tsc on PATH but could not be executed — "
                                "this file was NOT type-checked",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        # See hadolint.py for why this is a finding rather than a skip, and
        # why its absence was worse than a wrong verdict: an escaping
        # TimeoutExpired leaves stdout empty and the caller crashes on
        # json.loads with nothing naming the tool or the budget. `tsc` is the
        # likeliest of the three to reach its budget honestly — a cold
        # TypeScript compile on a cold runner is not fast — which makes
        # saying so, rather than dying, worth more here than anywhere.
        emit({"tool": "tsc-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — tsc did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "tsc-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    output = strip_ansi(result.stdout + result.stderr).strip()
    # The base is the directory this adapter spawned tsc in — it passed no
    # `cwd=`, so tsc inherited ours and prints relative to it.
    errors = parse_diagnostics(output, file, os.getcwd())

    # tsc objected and nothing in what it said could be placed in this file.
    # `code: "adapter"` rather than `"syntax"`, which asserted a syntax error had
    # been found here — a claim this arm cannot make, published with `line: null`
    # and `count: 1` however many diagnostics the dump actually held (#1499). The
    # core reads `adapter` on every error as "no verdict was obtained"
    # (`_validator_not_checked`), which is the third state, reached without the
    # `skipped` that would drop `errors` and lose tsc's objection with it —
    # `validators/SCHEMA.md` §"A located diagnostic still has to be about *this*
    # file (#754)".
    #
    # Reached with `output` empty too. `ok: false, count: 0, errors: []` was a
    # verdict of "not clean" carrying nothing to act on, and one the core could
    # not recognise as an absence either, because it looks for the reason on an
    # error that was not there.
    if not errors:
        if output:
            said = ("its output could not be parsed: "
                    + " ".join(output.split())[:200])
        else:
            said = "said nothing at all"
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": f"tsc exited {result.returncode} and {said} — this "
                          f"file was NOT type-checked"}]

    emit({"tool": "tsc-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    main()
