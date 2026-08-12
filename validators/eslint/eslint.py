#!/usr/bin/env python3
"""eslint validator adapter — JavaScript semantics via `eslint -f json` (#667).

JS/TS coverage before this was `node-check` (syntax), `tsc-check` (types, TS
only), `prettier-check` (formatting) and `stylelint` (CSS). No linter — so for
a plain `.js` file nothing checked for unused variables, `==` vs `===`,
unreachable code, shadowed declarations or accidental globals.

**Three absences, and each one arrives looking like a clean file.**

1. *eslint not installed.* `skipped`, with the install hint. On the machine
   this actually happens on — a laptop with node — `shutil.which("eslint")` is
   false and `shutil.which("npx")` is true, so the fallback below resolves and
   the install-hint branch is never reached at all. npx then exits **1 with
   empty stdout** and `Unknown command: "eslint"` (npm 11) or `could not
   determine executable to run` (npm 8-10) on stderr, which is neither a
   config problem nor a finding, and landed on `_adapter_error`: the reader
   was told eslint *failed* and sent to debug a linter that is not installed.
   `_NPX_ABSENT` catches exactly those two, and only on the npx route.
2. *eslint installed, no resolvable config.* eslint exits **2 with empty
   stdout** and puts "couldn't find an eslint.config.(js|mjs|cjs) file" on
   stderr. An adapter that only counts findings publishes `ok: true,
   count: 0` — #263's shape exactly. `skipped`.
3. *the file matched an ignore pattern.* eslint exits **0** and returns a
   single `ruleId: null, fatal: false` message saying the file was ignored.
   This is the worst of the three because the exit code is clean, and it is
   not in the issue. `skipped`.

**No fallback config is shipped, and that is the judgment call.** Inventing one
would have this validator report rules the project never adopted — the
argument already written into `validators/ruff/ruff.py`, and the first thing
anyone does about findings they did not opt into is switch the validator off,
which costs the coverage the fallback was meant to buy. It would also be
misleading in the case that motivated the issue: DVSI's `no-var` rule mostly
governs JS embedded in XML templates and inline handlers, which eslint cannot
reach at all, so a fallback config would produce a green that says nothing
about the rule it was configured for. A repo that wants JS linted adds
`eslint.config.js`; until then the honest answer is that nobody checked, and
the row says so on every edit rather than going quiet.

Where a skip is not acceptable — CI — name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` and every absence above becomes a loud
`adapter` error naming the variable. It only ever turns quiet into loud.

`rollback_on_fail` is false in every registration: a lint finding is not a
broken file.

Usage:  eslint.py <file>
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

TOOL = "eslint"

TIMEOUT_S = 60

RC_CLEAN = 0
RC_FINDINGS = 1

INSTALL_HINT = ("eslint not found (neither on PATH nor resolvable through "
                "`npx --no-install`) — `npm install --save-dev eslint`")

#: How eslint 9/10 announce that no flat config resolved. Matched on stderr,
#: deliberately narrow: any other exit-2 stays a loud fault, because
#: swallowing an unknown failure is the same category mistake pointing the
#: other way.
_NO_CONFIG = (
    "couldn't find an eslint.config",
    "couldn't find a configuration file",
    "no eslint configuration found",
)

#: How npx says the package is not installed and `--no-install` forbids
#: fetching it. Consulted only when the npx fallback was the route taken, and
#: deliberately narrow: any other npx failure stays a loud fault, because
#: swallowing an unknown failure is the same category mistake pointing the
#: other way. Both spellings are live — npm 11 rewrote the message.
_NPX_ABSENT = (
    "could not determine executable to run",
    'unknown command: "eslint"',
)

#: How eslint says it declined to lint a file it was handed. `ruleId` is null,
#: `fatal` is false, there is no location, and the exit code is 0.
_IGNORED = "file ignored"


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:
    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


def _decline(file: str, reason: str, dur_ms: int) -> None:
    """`skipped`, unless this validator is required — then a loud error."""
    if required(TOOL):
        _adapter_error(file, required_but_absent(TOOL, reason), dur_ms)
    else:
        emit(skipped(TOOL, file, reason, dur_ms))


def _resolve_cmd() -> list:
    """argv prefix for eslint: global first, then a project-local install."""
    if shutil.which(TOOL):
        return [TOOL]
    if shutil.which("npx"):
        # `--no-install` so a missing eslint stays a missing eslint rather
        # than becoming a silent network fetch inside a post-edit validator.
        return ["npx", "--no-install", TOOL]
    return []


def _ignored_reason(messages: list) -> str | None:
    """The file eslint refused to lint, or None.

    Only when *every* message is the ignore notice: a file with real findings
    plus an unrelated null-rule row is still a verdict.
    """
    if not messages:
        return None
    for m in messages:
        if not isinstance(m, dict):
            return None
        if m.get("ruleId") is not None or m.get("fatal"):
            return None
        if _IGNORED not in (m.get("message") or "").lower():
            return None
    text = " ".join((m.get("message") or "") for m in messages)
    return ("eslint declined to lint this file — it matched an ignore "
            f"pattern: {text.strip()[:200]}")


def _severity(msg: dict) -> str:
    if msg.get("fatal") or msg.get("severity") == 2:
        return "error"
    return "warning"


def _to_error(msg: dict, file: str) -> dict:
    line = msg.get("line")
    text = (msg.get("message") or "").strip().replace("\n", " ")[:300]
    err = {
        "line": line,
        "col": msg.get("column"),
        "severity": _severity(msg),
        "code": msg.get("ruleId"),
        "msg": text,
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

    base = _resolve_cmd()
    if not base:
        _decline(file, INSTALL_HINT, int((time.time() - start) * 1000))
        return
    via_npx = base[0] != TOOL

    try:
        r = subprocess.run(base + ["-f", "json", file], capture_output=True,
                           text=True, timeout=TIMEOUT_S, encoding="utf-8",
                           errors="replace")
    except FileNotFoundError:
        _decline(file, "eslint resolved but could not be executed",
                 int((time.time() - start) * 1000))
        return
    except subprocess.TimeoutExpired:
        _adapter_error(file, f"timeout — eslint did not return within "
                             f"{TIMEOUT_S}s; the file was NOT checked",
                       int((time.time() - start) * 1000))
        return

    dur = int((time.time() - start) * 1000)
    body = (r.stdout or "").strip()
    stderr = (r.stderr or "").strip()

    if not body:
        lowered = stderr.lower()
        if via_npx and any(p in lowered for p in _NPX_ABSENT):
            # An absent eslint, reached one layer further out. The same third
            # state as `not base`, with the same hint — the reader's next
            # action is `npm install`, not reading an npx traceback.
            _decline(file, INSTALL_HINT, dur)
            return
        if any(p in lowered for p in _NO_CONFIG):
            _decline(file,
                     "eslint found no resolvable configuration "
                     "(eslint.config.js) — this file was not linted, and no "
                     "fallback ruleset is invented for it", dur)
            return
        _adapter_error(file, tool_fault("eslint", r.returncode,
                                        stderr or "(no output)"), dur)
        return

    try:
        results = json.loads(body)
    except ValueError:
        _adapter_error(file, tool_fault("eslint", r.returncode,
                                        r.stdout or stderr or ""), dur)
        return

    if not isinstance(results, list):
        _adapter_error(file, tool_fault("eslint", r.returncode,
                                        f"expected a JSON array, got "
                                        f"{type(results).__name__}"), dur)
        return

    messages = []
    for res in results:
        if isinstance(res, dict) and isinstance(res.get("messages"), list):
            messages.extend(m for m in res["messages"] if isinstance(m, dict))

    ignored = _ignored_reason(messages)
    if ignored is not None:
        _decline(file, ignored, dur)
        return

    errors = [_to_error(m, file) for m in messages]
    emit({"tool": TOOL, "file": file, "ok": not errors, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
