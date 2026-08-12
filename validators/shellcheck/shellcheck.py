#!/usr/bin/env python3
"""shellcheck validator adapter — shell semantics via `shellcheck -f json` (#665).

`bash-check` runs `bash -n` and answers "does this parse", which is the
smallest question you can ask of a shell script. This answers the next one.
Both stay: `bash -n` is in-process-cheap and is what still runs on a machine
where shellcheck is not installed.

**Absent, this reports the third state — `skipped` with the reason — and never
`ok`** (validators/SCHEMA.md, "Skipped: the third state"). A checker nobody
installed has said nothing about the file. Where that quiet is not acceptable
— CI, where "not installed" means the gate is not running — name this
validator in `$SUPERTOOL_REQUIRE_VALIDATORS` and the same absence becomes a
loud `adapter` error instead. See `refusal.required`.

**What this does not catch, stated because the issue says otherwise.** #665
justifies the validator with `claude-remember#251` and calls it "SC2015
verbatim". Measured against shellcheck 0.11.0, SC2015 does *not* fire on that
line, at any severity, with `-o all`: the check deliberately stays silent when
the `|| C` branch is an `echo` or a `printf`, and #251's C was
`echo "(no previous entry)" > "$TMP"`. The redirect that made it destructive
is not part of what SC2015 looks at. `tests/test_validators_shellcheck_665.py`
pins the gap so the claim cannot be restated later without a red.

What does fire, and is why this ships anyway: SC2164 (`cd` without `|| exit`),
SC2086 (unquoted expansion), SC2181 (`$?` instead of testing directly) and
SC2155 (`local x=$(cmd)` masking an exit code) — each exercised on a real file
by the test suite rather than assumed.

**No `--severity` floor and no `-o all`.** The ruleset is the project's
business, exactly as in `validators/ruff/ruff.py`: shellcheck reads a
`.shellcheckrc` walking up from the file, so a repo tunes its own findings.
An adapter that hard-coded a selection would report rules nobody adopted.

`rollback_on_fail` is false in every registration. SC2086 is a style finding,
and reverting a good edit because it landed next to an unquoted `$1` destroys
work to fix nothing. Only `bash-check` rolls back, and only because a file
that does not parse is genuinely broken.

Usage:  shellcheck.py <file>
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import required, required_but_absent, skipped, tool_fault

TOOL = "shellcheck"

# Hang-guard, not a performance assertion. shellcheck on a single file is
# milliseconds; the number is named in the decline so a reader can tell a hung
# checker from a busy machine (#658).
TIMEOUT_S = 30

# `shellcheck -f json` exit codes: 0 clean, 1 findings, 2 the file could not be
# read or its shell dialect could not be determined, 3/4 usage. Only 0 and 1
# carry a verdict about the file.
RC_CLEAN = 0
RC_FINDINGS = 1

INSTALL_HINT = ("shellcheck not found on PATH — `brew install shellcheck` / "
                "`apt install shellcheck`")

# How shellcheck says it declined rather than judged: it could not open the
# file, or it cannot tell which shell this is. Neither is a defect in the
# script, and neither may be published as a finding. Deliberately narrow — an
# exit this cannot explain stays a loud `adapter` error.
_CANNOT_CLASSIFY = "shellcheck can't be used with"
_NO_SHEBANG = "tell what kind of shell"


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:
    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


def _severity(level: object) -> str:
    """shellcheck's four levels onto SCHEMA.md's three.

    `error` is reserved for what shellcheck itself calls an error — a script
    that will not run. `warning`, `info` and `style` are all things the author
    should look at on a script that works, and flattening them to `error`
    would overstate every row this validator ever prints next to a real parse
    failure from `bash-check`.
    """
    text = str(level or "").lower()
    if text == "error":
        return "error"
    if text in ("info", "style"):
        return "info"
    return "warning"


def _to_error(item: dict, file: str) -> dict:
    line = item.get("line")
    code = item.get("code")
    err = {
        "line": line,
        "col": item.get("column"),
        "severity": _severity(item.get("level")),
        "code": f"SC{code}" if code is not None else None,
        "msg": (item.get("message") or "").strip().replace("\n", " ")[:300],
    }
    if isinstance(line, int):
        err.update(context_fields(file, line))
    return err


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which(TOOL):
        dur = int((time.time() - start) * 1000)
        if required(TOOL):
            _adapter_error(file, required_but_absent(TOOL, INSTALL_HINT), dur)
        else:
            emit(skipped(TOOL, file, INSTALL_HINT, dur))
        return

    try:
        r = subprocess.run([TOOL, "-f", "json", file], capture_output=True,
                           text=True, timeout=TIMEOUT_S, encoding="utf-8",
                           errors="replace")
    except FileNotFoundError:
        # `which` said yes and exec said no. Still an absent tool.
        dur = int((time.time() - start) * 1000)
        reason = "shellcheck on PATH but could not be executed"
        if required(TOOL):
            _adapter_error(file, required_but_absent(TOOL, reason), dur)
        else:
            emit(skipped(TOOL, file, reason, dur))
        return
    except subprocess.TimeoutExpired:
        # The binary was found and started, so this is a validator failure and
        # stays loud (docs/validators.md, "Declining instead of guessing").
        _adapter_error(file, f"timeout — shellcheck did not return within "
                             f"{TIMEOUT_S}s; the file was NOT checked",
                       int((time.time() - start) * 1000))
        return

    dur = int((time.time() - start) * 1000)
    body = (r.stdout or "").strip()
    stderr = (r.stderr or "").strip()

    # A refusal shellcheck can explain: it could not read the file, or it
    # cannot tell which shell dialect this is. The second is the extensionless
    # case with no shebang, which is a config gap and not a defect.
    if r.returncode not in (RC_CLEAN, RC_FINDINGS):
        lowered = stderr.lower()
        if _CANNOT_CLASSIFY in lowered or _NO_SHEBANG in lowered:
            emit(skipped(TOOL, file,
                         "shellcheck could not determine the shell dialect "
                         "(no shebang, no --shell) — this file was not checked",
                         dur))
            return
        _adapter_error(file, tool_fault("shellcheck", r.returncode,
                                        stderr or r.stdout or ""), dur)
        return

    try:
        items = json.loads(body) if body else []
    except ValueError:
        _adapter_error(file, tool_fault("shellcheck", r.returncode,
                                        r.stdout or stderr or ""), dur)
        return

    if not isinstance(items, list):
        _adapter_error(file, tool_fault("shellcheck", r.returncode,
                                        f"expected a JSON array, got "
                                        f"{type(items).__name__}"), dur)
        return

    errors = [_to_error(i, file) for i in items if isinstance(i, dict)]
    emit({"tool": TOOL, "file": file, "ok": not errors, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
