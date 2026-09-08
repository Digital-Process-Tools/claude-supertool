#!/usr/bin/env python3
"""markdownlint validator adapter — Markdown lint via markdownlint CLI.

Requires markdownlint on PATH. Absent, this reports the third state — `skipped`
with the reason — rather than the `ok: true` it emitted until #1202, which was a
clean verdict about a file nothing linted. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

Usage:  markdownlint.py <file>
"""

from __future__ import annotations

import fnmatch
import json
import os
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

# #2338: a project's own changelog-fragment convention (bullet-only, no
# heading, no 80-column wrap -- this repo's own changelog.d/README.md is one
# such convention, and it is a *project* choice, never this validator's)
# reads as malformed prose to markdownlint's stock ruleset. Scoped to the
# same glob `.supertool.json` already wires the `changelog-fragment`
# validator to, not reinvented, so the two never drift.
CHANGELOG_FRAGMENT_GLOB_ENV = "SUPERTOOL_MARKDOWNLINT_CHANGELOG_GLOB"
CHANGELOG_FRAGMENT_GLOB_DEFAULT = "*changelog.d/*.md"

# The same three conventions changelog-fragment.py's ASSEMBLER_LOCATIONS
# tries, in the same order -- this is read-only existence checking, never an
# import/execution of the found script, so the execution-trust-boundary
# machinery that guards changelog-fragment.py's own `_load` does not apply
# here: this file never runs a byte of what it finds.
CHANGELOG_ASSEMBLER_ENV = "SUPERTOOL_CHANGELOG_ASSEMBLER"
CHANGELOG_ASSEMBLER_LOCATIONS = (
    os.path.join(".github", "scripts", "assemble_changelog.py"),
    os.path.join(".oss", "assemble_changelog.py"),
    os.path.join("scripts", "assemble_changelog.py"),
)

CHANGELOG_FRAGMENT_SKIP_REASON = (
    "this path matches the project's changelog-fragment convention and its "
    "own assembler script was found above it -- the `changelog-fragment` "
    "validator already owns this file's shape (no heading, no wrap), so "
    "markdownlint's generic ruleset does not apply here (#2338)")


def _repo_root(start: str) -> str | None:
    """The git repo root above `start`, or `None` -- mirrors
    `validators/common/ci_lint_resolve_root.py`'s `_repo_root`. Any failure
    (git absent, timeout, not a repo) means the walk below cannot be bounded,
    so the caller treats that as "not exempt" rather than falling back to an
    unbounded walk."""
    start_dir = os.path.dirname(os.path.abspath(start)) or "."
    try:
        r = subprocess.run(
            ["git", "-C", start_dir, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    top = r.stdout.strip()
    return top or None


def _changelog_fragment_glob() -> str:
    override = os.environ.get(CHANGELOG_FRAGMENT_GLOB_ENV, "").strip()
    return override if override else CHANGELOG_FRAGMENT_GLOB_DEFAULT


def _assembler_locations() -> tuple:
    override = os.environ.get(CHANGELOG_ASSEMBLER_ENV, "").strip()
    return (override,) if override else CHANGELOG_ASSEMBLER_LOCATIONS


def _owned_by_changelog_fragment(file: str) -> bool:
    """True only when `file` matches the project's changelog-fragment glob
    AND that project has actually adopted the convention -- its own
    assembler script is discoverable somewhere at or above the file, bounded
    at the repo root. The glob alone is not enough: a project with an
    unrelated `changelog.d/` directory (no assembler anywhere above it) gets
    ordinary markdownlint coverage there, same as any other Markdown.

    Self-review finding: `root` came from `git rev-parse --show-toplevel`,
    which chdir()s and calls getcwd() -- the PHYSICAL, symlink-resolved
    path -- while `current` was built from repeated `os.path.dirname` on a
    plain `os.path.abspath`, which never resolves a symlink. On a tree where
    any component between the file and the repo root is a symlink (macOS's
    default TMPDIR sits under `/var`, itself a symlink to `/private/var`; a
    symlinked worktree or checkout is the same shape), the two never
    string-compare equal, the walk never stops at the true root, and it
    climbs into an unrelated ancestor directory whose own conventionally-
    named file gets picked up as if it belonged to this project -- exactly
    the escape #2178/#2236 bound `changelog-fragment.py`'s identical walk
    against, which uses `Path(...).resolve()` throughout for the same
    reason. `os.path.realpath` here matches that fix: both `current` and
    `root` are resolved once, so the loop's stop condition is a physical-
    path comparison on both sides.
    """
    norm = os.path.abspath(file).replace(os.sep, "/")
    if not fnmatch.fnmatch(norm, _changelog_fragment_glob()):
        return False
    root = _repo_root(file)
    if root is None:
        return False
    root = os.path.realpath(root)
    current = os.path.dirname(os.path.realpath(file))
    locations = [loc for loc in _assembler_locations() if loc]
    while True:
        for loc in locations:
            if os.path.isfile(os.path.join(current, loc)):
                return True
        if os.path.normcase(current) == os.path.normcase(root):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return False


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

    if _owned_by_changelog_fragment(file):
        emit(skipped(TOOL, file, CHANGELOG_FRAGMENT_SKIP_REASON,
                     int((time.time() - start) * 1000)))
        return

    if not shutil.which("markdownlint"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            ["markdownlint", "--", file],
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
