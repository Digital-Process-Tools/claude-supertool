#!/usr/bin/env python3
"""gitleaks validator adapter — post-edit secret scan (#668).

Thirty-odd validators and none of them looked for a credential. This is the
only one on the list whose absence is a disclosure problem rather than a
quality one: every other check catches something that costs an hour, and a
pushed secret is a rotated secret.

**The third state matters more here than anywhere else in this directory.** A
scanner that reports `ok` because its binary is missing tells the reader "no
secrets found" about a scan that never happened, and the reader acts on that
by committing. So an absent `gitleaks` is `skipped`, and the reason says *the
file was not scanned* rather than merely naming a missing binary — "gitleaks
not found" reads as a tooling note, which is not the sentence a reader needs.
Name this validator in `$SUPERTOOL_REQUIRE_VALIDATORS` (CI) and the same
absence becomes a loud `adapter` error instead.

## The finding never carries the value

gitleaks' report has `Secret` and `Match` in cleartext, and the only way to get
JSON out of it is `--report-path <file>` — so the naive adapter writes the
credential to disk before it decides not to print it. Three guards, in order
of how much they buy:

1. **`--redact` is passed**, so the value is never written to the report at
   all. Everything downstream is then working with `REDACTED`.
2. **The report lives in a private `mkdtemp` directory** (mode 0700) and is
   removed in a `finally`, so nothing survives a crash either.
3. **No `source_context` is attached.** Every sibling adapter attaches the
   surrounding source lines; for this one the source line *is* the secret, and
   a validator receipt is simultaneously a terminal, a scrollback, a log and
   an agent transcript.

The rule id, the line and gitleaks' own description are enough to act on.
`presets/_secrets.py:disclosure` already sets the house wording for this —
"matched known secret patterns", never "the output is safe" — and the message
here follows it: detection is pattern-based, so a clean result is the absence
of a match and not a guarantee.

## `rollback_on_fail` is false, against the issue's suggestion

#668 argues rollback is defensible here and nowhere else, on the grounds that
the alternative is the value sitting on disk. Three reasons it is not taken:

- **Reverting does not unpublish anything.** The value reached the edit from
  somewhere — a payload file, a paste, an agent's context, shell history — and
  all of those still hold it after the revert. Rollback buys the appearance of
  containment, not containment.
- **It destroys the rest of the edit.** A `batch:` that fixes three things and
  happens to touch a token-shaped literal loses all three.
- **The false-positive rate makes it dangerous.** A full `--no-git` scan of
  this repo returns 12 findings and all 12 are fixtures — 8 in
  `tests/test_secret_redaction_760.py` and friends, 4 in `__pycache__`.
  `claude-remember` returns 0. Rollback would have reverted real edits over
  fake keys, which is the fastest possible route to the validator being
  switched off.

The finding stays fully loud — `ok: false`, severity `error` — which is what
makes it actionable. Only the destructive auto-revert is off.

## False positives are suppressed per line, never per directory

A fixture holding a fake key is the normal case, not the edge case. The
suppression is gitleaks' own `gitleaks:allow` trailing comment: it is one
line, it lands in the diff, and a reviewer sees it. What this adapter
deliberately does **not** ship is an `exclude` glob over `tests/` or `docs/` —
that is a switch that turns the check off invisibly, forever, for exactly the
paths most likely to grow a real credential next. A repo that needs bulk
suppression uses a `.gitleaksignore` of explicit fingerprints, which is also
reviewable.

Usage:  gitleaks.py <file>
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from refusal import required, required_but_absent, skipped, tool_fault

TOOL = "gitleaks"

TIMEOUT_S = 60

# `gitleaks detect` exit codes: 0 no leaks, 1 leaks found (its --exit-code
# default), anything else is gitleaks itself failing.
RC_CLEAN = 0
RC_FINDINGS = 1

INSTALL_HINT = (
    "gitleaks not found on PATH — this file was NOT scanned for secrets "
    "(`brew install gitleaks`). A clean row here would have meant nothing")


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:
    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


def _decline(file: str, reason: str, dur_ms: int) -> None:
    if required(TOOL):
        _adapter_error(file, required_but_absent(TOOL, reason), dur_ms)
    else:
        emit(skipped(TOOL, file, reason, dur_ms))


def _to_error(item: dict) -> dict:
    """A finding with everything but the credential.

    `Secret`, `Match` and `Line` are read from the report and dropped on the
    floor; `--redact` should already have replaced them, and this does not
    rely on that. No `source_context` — see the module docstring.
    """
    rule = item.get("RuleID") or None
    desc = (item.get("Description") or "").strip().replace("\n", " ")[:200]
    line = item.get("StartLine")
    return {
        "line": line if isinstance(line, int) else None,
        "col": item.get("StartColumn") if isinstance(
            item.get("StartColumn"), int) else None,
        "severity": "error",
        "code": rule,
        "msg": (f"possible secret ({desc or rule or 'unnamed rule'}) — the "
                f"matched value is deliberately not printed. Detection is "
                f"pattern-based: rotate it if it is real, or annotate the "
                f"line with `gitleaks:allow` if it is a fixture"),
    }


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which(TOOL):
        _decline(file, INSTALL_HINT, int((time.time() - start) * 1000))
        return

    # 0700, and removed in the `finally` below: for as long as it exists this
    # directory is a credential store, even with --redact on.
    workdir = tempfile.mkdtemp(prefix="supertool-gitleaks-")
    os.chmod(workdir, 0o700)
    report = os.path.join(workdir, "report.json")
    try:
        cmd = [TOOL, "detect", "--no-git", "--redact", "--no-banner",
               "--source", file, "--report-format", "json",
               "--report-path", report]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=TIMEOUT_S, encoding="utf-8",
                               errors="replace")
        except FileNotFoundError:
            _decline(file, "gitleaks on PATH but could not be executed — this "
                           "file was NOT scanned for secrets",
                     int((time.time() - start) * 1000))
            return
        except subprocess.TimeoutExpired:
            _adapter_error(file, f"timeout — gitleaks did not return within "
                                 f"{TIMEOUT_S}s; the file was NOT scanned",
                           int((time.time() - start) * 1000))
            return

        dur = int((time.time() - start) * 1000)

        # gitleaks writes its findings to the report, not to stdout, so an
        # exit code alone cannot say whether it looked at the file. A missing
        # report on a non-zero exit is gitleaks failing, not a clean file.
        try:
            with open(report, encoding="utf-8") as fh:
                body = fh.read().strip()
        except OSError:
            body = ""

        if not body:
            if r.returncode == RC_CLEAN:
                # gitleaks writes no report when it finds nothing, on some
                # builds. Exit 0 plus no findings is a genuine clean.
                emit({"tool": TOOL, "file": file, "ok": True, "count": 0,
                      "errors": [], "duration_ms": dur})
                return
            _adapter_error(file, tool_fault("gitleaks detect", r.returncode,
                                            r.stderr or r.stdout or ""), dur)
            return

        try:
            items = json.loads(body)
        except ValueError:
            # The report itself may hold secrets; quote the exit code and the
            # tool's stderr, never the report body.
            _adapter_error(file, tool_fault(
                "gitleaks detect", r.returncode,
                "the JSON report could not be parsed (its contents are not "
                "quoted here, since a secret report is what it is)"), dur)
            return

        if not isinstance(items, list):
            _adapter_error(file, tool_fault(
                "gitleaks detect", r.returncode,
                f"expected a JSON array report, got {type(items).__name__}"),
                dur)
            return

        if r.returncode not in (RC_CLEAN, RC_FINDINGS) and not items:
            _adapter_error(file, tool_fault("gitleaks detect", r.returncode,
                                            r.stderr or r.stdout or ""), dur)
            return

        errors = [_to_error(i) for i in items if isinstance(i, dict)]
        emit({"tool": TOOL, "file": file, "ok": not errors,
              "count": len(errors), "errors": errors, "duration_ms": dur})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
