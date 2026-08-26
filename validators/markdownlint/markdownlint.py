#!/usr/bin/env python3
"""markdownlint validator adapter — Markdown lint via markdownlint CLI.

Requires markdownlint on PATH. Absent, this reports the third state — `skipped`
with the reason — rather than the `ok: true` it emitted until #1202, which was a
clean verdict about a file nothing linted. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

Usage:  markdownlint.py <file>
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
from refusal import absent, guard_main, skipped
from linebreaks import split_lines
from path_anchor import (anchor as _anchor, safe_realpath as _safe_realpath,
                          anchor_miss_message as _anchor_miss_message)

TOOL = "markdownlint"
INSTALL_HINT = ("markdownlint not found on PATH — this file was NOT linted "
                "(`npm install -g markdownlint-cli`)")


# Budget for the one tool spawn below. A module constant rather than a literal
# in the call so the decline can name it: a caller reading "timeout" cannot
# tell a hung linter from a busy machine, and the number is the first thing
# they need to decide which (#658).
TIMEOUT_S = 30

# What "it linted nothing" looks like, and why it needs no second spawn.
#
# markdownlint-cli resolves its arguments to a file list and honours
# `.markdownlintignore` (and `--ignore-path`) while doing it. When the path it
# was handed survives none of that — an ignore match, or a file that is not
# there — it has nothing to lint, prints its usage banner on **stdout** and
# exits 0 (measured, markdownlint-cli 0.49.1: 1501 bytes of help). A file it
# genuinely linted clean prints nothing on stdout.
#
# So the discriminator is stdout, not a probe: there is no `--file-info`
# equivalent here, and output on stdout at a zero exit means the run was not
# about this file.
#
# **stderr is not part of that question**, and reading `stdout + stderr` was a
# real misreport rather than a conservative one (#1601 audit). markdownlint is
# a Node program, and Node writes its own chatter to stderr over runs that
# worked perfectly — one `[DEP0040] DeprecationWarning` turned every clean
# markdown file into a `skipped` that named a determinate, false cause, and in
# a repo where this validator fires on every markdown edit that is markdown
# linting silently off everywhere.
#
# The residual this leaves is a markdownlint that fails while exiting 0 and
# says so only on stderr: that would be read as clean. It is accepted rather
# than guarded, because nothing can tell that apart from a deprecation warning
# by content, and the guard we had for it disabled the linter on the case that
# actually happens. A tool that fails exits non-zero, and both non-zero arms
# below read stderr.
NOTHING_LINTED_REASON = (
    "markdownlint exited 0 and printed to stdout, which is what it does when "
    "the path resolved to no files to lint — an ignore-file match "
    "(`.markdownlintignore`, `--ignore-path`) or a path that is not there — "
    "so this run is not a verdict about the file: ")


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "markdownlint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("markdownlint"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            ["markdownlint", file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "markdownlint on PATH but could not be "
                                "executed — this file was NOT linted",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        # See hadolint.py for why this is a finding rather than a skip, and
        # why its absence was worse than a wrong verdict: an escaping
        # TimeoutExpired leaves stdout empty and the caller crashes on
        # json.loads with nothing naming the tool or the budget.
        emit({"tool": "markdownlint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — markdownlint did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        chatter = (result.stdout or "").strip()
        if chatter:
            lines = split_lines(chatter)
            emit(skipped(TOOL, file,
                         NOTHING_LINTED_REASON + lines[0].strip()[:200],
                         duration))
            return
        emit({"tool": "markdownlint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    # Parse markdownlint output: "file:line:col rule/description"
    # or "file:line rule/description" (no col) — and, since markdownlint-cli
    # started printing a severity word between the two ("b.md:1:1 error MD018
    # ...", measured on 0.49.1), optionally that. Without it every row fell
    # through to the catch-all below: four located findings arrived as one
    # unlocated `lint` error with no source context and a count of 1.
    #
    # Anchored on the invoked path itself (#1934, then #1940 for this
    # adapter) rather than a bare `(?:.*?)`: the non-greedy wildcard used to
    # discard the path instead of matching it, so it bound to the *earliest*
    # `:digit:digit:` run anywhere in the line — including one supplied by a
    # filename crafted to contain its own `N:M: ` sequence, e.g.
    # `x:1:1: fake.md`. Building the pattern from `file` means only a
    # spelling of the path markdownlint was actually invoked against can
    # start a match (see `path_anchor.py`, #1937, for what "a spelling of"
    # widened to). `.search()`, not `.match()`: a tool can print the invoked
    # path more than once before its own diagnostic.
    #
    # Observed, not reasoned: markdownlint-cli 0.49.1 (npx, node v22.22.1)
    # echoes the exact argv path back unmodified, including through a
    # symlink and with a crafted `x:1:1: fake.md` name — verified directly
    # against the real binary rather than assumed from the sibling adapters
    # this class was found across.
    errors = []
    real = _safe_realpath(file)
    extra = [real] if real and real != file else []
    pattern = _anchor(
        file,
        r":(\d+)(?::(\d+))?\s+(?:(?:error|warning)\s+)?(MD\d+[^\s]*)\s+(.+)$",
        extra_paths=extra)
    output = (result.stdout + result.stderr).strip()
    for line in split_lines(output):
        m = pattern.search(line)
        if m:
            lineno, col, code, msg = m.groups()
            ln = int(lineno)
            err = {
                "line": ln,
                "col": int(col) if col else None,
                "severity": "error",
                "code": code,
                "msg": msg.strip()[:300],
            }
            err.update(context_fields(file, ln))
            errors.append(err)

    if not errors and output:
        # #1937, third CI round, applied here for #1940: when the anchor
        # missed but markdownlint DID say something, say what it saw --
        # the invoked path and whatever path the tool's own output appears
        # to name -- instead of silently reporting a false clean verdict.
        # A non-zero exit that produced no located finding is still not
        # "ok: true, count: 0" about the file.
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "lint",
                   "msg": _anchor_miss_message(file, output, output[:300])}]

    emit({"tool": "markdownlint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    guard_main(TOOL, main)
