#!/usr/bin/env python3
"""stylelint validator adapter — CSS/SCSS lint via stylelint.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Uses project-local stylelint config (auto-discovered by stylelint).
Usage:  stylelint.py <file>

**Where the report arrives is not fixed, and empty is never a verdict** (#1601).
stylelint writes its formatted report to **stderr** — `process.stderr.write(report)`
in its own `cli.mjs`, with stdout reserved for `--fix` output — and older
releases wrote it to stdout. This adapter read stdout and treated emptiness as
`ok: true, count: 0`, so against a current stylelint every CSS file came back
clean, findings and all. Both streams are offered to the JSON reader now, and a
run with no report at all is a fault or a decline, never a pass.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import absent, skipped, tool_fault

TOOL = "stylelint"
INSTALL_HINT = ("stylelint not found, globally or via npx — this file was NOT "
                "linted (`npm install -g stylelint`)")

# How stylelint says "every input was excluded". It has no `--file-info`-style
# probe, and none has to be invented: an ignored path is the one case it names
# itself, on stderr, with a class name. `AllFilesIgnoredError` is the current
# spelling and the sentence is the older one, so both are matched.
IGNORED_MARKERS = ("allfilesignorederror", "input files were ignored")
IGNORED_REASON = ("stylelint declined to lint this file — every input it "
                  "resolved was excluded by an ignore pattern "
                  "(`.stylelintignore`, or `ignoreFiles` in the config), so "
                  "nothing here is a verdict about it")


def emit(d: dict) -> None:
    print(json.dumps(d))


def _resolve_cmd() -> list:
    """Return argv prefix for stylelint. Tries global, falls back to npx."""
    if shutil.which("stylelint"):
        return ["stylelint"]
    if shutil.which("npx"):
        return ["npx", "--no-install", "stylelint"]
    return []


def _report(streams) -> "list | None":
    """The `--formatter json` report, from whichever stream carries it.

    `None` means no report was found — which is not an empty report, and is
    never a clean file. The caller decides between a decline and a fault.
    """
    for text in streams:
        text = (text or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, list):
            return data
    return None


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "stylelint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    base = _resolve_cmd()
    if not base:
        # The third state, and escalatable: an uninstalled optional linter must
        # not fail every unrelated CSS edit, and must not be silent where an
        # operator named it in `$SUPERTOOL_REQUIRE_VALIDATORS` (#1202).
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return
    try:
        r = subprocess.run(base + ["--formatter", "json", file],
                           capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two. Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "stylelint was found on PATH but could not be "
                                "executed — this file was NOT linted",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "stylelint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 60000})
        return
    dur = int((time.time() - start) * 1000)
    data = _report((r.stdout, r.stderr))
    if data is None:
        noise = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()
        if any(m in noise.lower() for m in IGNORED_MARKERS):
            emit(skipped(TOOL, file, IGNORED_REASON, dur))
            return
        # No report on either stream: a config error, a broken install, a flag
        # this stylelint does not have. The tool ran and said nothing about the
        # file, which is a fault someone has to fix — loud, never a pass.
        emit({"tool": "stylelint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": tool_fault("stylelint", r.returncode, noise)}],
              "duration_ms": dur})
        return
    errors = []
    for item in data:
        for w in item.get("warnings", []):
            ln = w.get("line")
            err = {
                "line": ln,
                "col": w.get("column"),
                "severity": w.get("severity", "warning"),
                "code": w.get("rule"),
                "msg": (w.get("text") or "")[:300],
            }
            if ln is not None:
                err.update(context_fields(file, ln))
            errors.append(err)
    emit({"tool": "stylelint", "file": file, "ok": len(errors) == 0,
          "count": len(errors), "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
