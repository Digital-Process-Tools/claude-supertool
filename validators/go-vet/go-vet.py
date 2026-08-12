#!/usr/bin/env python3
"""go-vet validator adapter — Go semantic checks via `go vet` (#669).

`gofmt-check` was Go's only bundled validator, and it answers a formatting
question. A `fmt.Printf("%d", "x")`, a lock copied by value, a struct tag that
does not parse — all of them gofmt clean. `go vet` is the cheap half of the gap
#669 recorded: it ships with the toolchain, so a repo that can build Go can run
it, and there is nothing extra to install.

Three states, and the two ways this adapter can have no verdict are different:

* **No Go toolchain** -> `absent()`, which is a `skipped` unless the repo named
  this validator in `$SUPERTOOL_REQUIRE_VALIDATORS`. "Not installed" on a
  laptop is fine; on CI it means the gate is not running.
* **No `go.mod` above the file** -> a plain `skipped` that never escalates. The
  toolchain is installed and working; the reason `go vet` will not look is the
  layout, and shouting about the CI image would point at the wrong thing.

**`go vet` is package-scoped.** Handed `pkg/a.go` it vets every file in `pkg`,
so a diagnostic in `pkg/b.go` arrives in the same output. Those are kept —
suppressing them leaves a clean-looking file in a package vet rejects — but
they are published with `line`/`col` null, `code: "adapter"` and no
`source_context`, per `validators/SCHEMA.md` §"A located diagnostic still has
to be about *this* file (#754)". `adapter` also means such a finding is never
cached and never rolls anything back, which is what stops a sibling's defect
from reverting a good edit.

The tool is run **in the package directory**, so every relative path it prints
is relative to a base this adapter chose rather than one it has to infer. That
is the whole reason this adapter needs none of the workspace-root machinery
`cargo-check` carries (#1045).

Usage:  go-vet.py <file>
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from pkg_paths import attribute
from refusal import absent, skipped, tool_fault
from linebreaks import split_lines

TOOL = "go-vet"
BINARY = "go"
INSTALL_HINT = ("go not found on PATH — this package was NOT vetted "
                "(install the Go toolchain from https://go.dev/dl/)")
TIMEOUT = 60
#: More findings than anyone reads in a post-edit receipt. `count` still
#: reports the full number, and the cut is disclosed in the last published
#: slot rather than left for a reader to infer by comparing two numbers.
MAX_ERRORS = 50

#: `./a.go:6:2: msg`, `pkg/a.go:6:2: msg`, `vet: a.go:3:12: msg`, and the
#: Windows shapes of each. The path group is non-greedy so a drive letter's
#: colon is not mistaken for the line separator.
#:
#: **The load prefix is the tool binary's own name, so on Windows it is
#: `vet.exe: `.** Matching only `vet: ` there leaves the prefix inside the path
#: group — the diagnostic then names a file called `vet.exe: .\a.go`, which is
#: not the file under validation, so a package that does not compile is
#: published as some sibling's `warning`. Four red windows-latest legs on PR
#: #1443 and green on the other eighteen; `tests/test_validators_go_vet_669.py`
#: pins both spellings from any runner.
DIAG = re.compile(
    r"^(?P<load>vet(?:\.exe)?:\s+)?(?P<path>.+?):(?P<line>\d+):(?P<col>\d+):"
    r"\s*(?P<msg>.*)$")


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> dict:
    return {"tool": TOOL, "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": msg}],
            "duration_ms": dur_ms}


def _module_root(start: pathlib.Path) -> pathlib.Path | None:
    """Nearest directory at or above `start` holding a `go.mod`."""
    for d in [start, *start.parents]:
        if (d / "go.mod").is_file():
            return d
    return None


def _elsewhere(reported: str, line: str, col: str, msg: str) -> dict:
    """A package diagnostic this file did not cause.

    Not filtered out: the package genuinely fails vet, and a caller told
    nothing about that cannot act on it. Only the attribution changes — the
    location belongs to another file, so it is not published as this one's.
    """
    return {"line": None, "col": None, "severity": "warning", "code": "adapter",
            "msg": f"in {reported}:{line}:{col} (another file in this package): {msg}"}


def _unplaceable(reported: str, line: str, col: str, msg: str) -> dict:
    """A diagnostic whose path resolves to no file this adapter can name.

    Distinct from `_elsewhere` on purpose: "not this file" and "no way to tell
    which file" are different sentences, and only the first is entitled to say
    another file is at fault.
    """
    return {"line": None, "col": None, "severity": "warning", "code": "adapter",
            "msg": f"go vet reported {reported}:{line}:{col} — this adapter could "
                   f"not tell whether that is the file under validation: {msg}"}


def _os_reason(exc: OSError) -> str:
    """The OS's own words for a failed spawn, or a sentence saying it gave none.

    `OSError.strerror` is `None` for a raise carrying no errno, and an f-string
    renders that as the word `None` — "go vet could not be run: OSError — None"
    reads as a reason that was reported and happened to be None. The absence is
    the adapter's to disclose, not the reader's to decode.
    """
    return exc.strerror or str(exc) or "the OS reported no reason"


def _truncation_notice(hidden: int) -> dict:
    """The cut, said out loud.

    A capped list read as a whole list is this repo's own defect in miniature:
    an absence the adapter produced, indistinguishable from an absence in the
    package. `count` carries the real total, but nobody compares a number at
    the top of a receipt against the number of rows underneath it.
    """
    return {"line": None, "col": None, "severity": "warning", "code": "adapter",
            "msg": f"{hidden} further go vet finding(s) in this package are not "
                   f"shown — `count` reports the full number"}


def _parse(output: str, target: str, base: str) -> list:
    errors = []
    for raw in split_lines(output):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue  # `# example.com/probe/pkg` names the package, not a fault
        m = DIAG.match(text)
        if not m:
            continue
        reported, line, col, msg = (m.group("path"), m.group("line"),
                                    m.group("col"), m.group("msg"))
        # `vet: ` — `vet.exe: ` on Windows, see DIAG — prefixes the load and
        # type-check failures: the package did not compile. That is go's own
        # distinction, not one invented here, and it is the only severity
        # signal the text output carries.
        severity = "error" if m.group("load") else "warning"
        where = attribute(reported, target=target, base=base)
        if where == "this":
            err = {"line": int(line), "col": int(col), "severity": severity,
                   "code": "load" if severity == "error" else None,
                   "msg": msg}
            err.update(context_fields(target, int(line)))
        elif where == "other":
            err = _elsewhere(reported, line, col, msg)
            err["severity"] = severity
        else:
            err = _unplaceable(reported, line, col, msg)
            err["severity"] = severity
        # Rank, not sort key: findings about the file under validation come
        # first so the cap below cannot eat the reason this ran at all. go vet
        # emits in file order, so a target late in the alphabet is exactly the
        # one a naive cut loses. Within a rank the tool's own order is kept.
        errors.append((0 if where == "this" else 1, err))
    errors.sort(key=lambda pair: pair[0])
    return [err for _, err in errors]


def main() -> None:
    start = time.time()

    def ms() -> int:
        return int((time.time() - start) * 1000)

    if len(sys.argv) < 2 or not sys.argv[1]:
        emit(_adapter_error("", "no file arg", ms()))
        return

    file = sys.argv[1]

    if not os.path.isfile(file):
        emit(_adapter_error(file, "file not found", ms()))
        return

    if not shutil.which(BINARY):
        emit(absent(TOOL, file, INSTALL_HINT, ms()))
        return

    target = os.path.abspath(file)
    pkg_dir = os.path.dirname(target)

    if _module_root(pathlib.Path(pkg_dir)) is None:
        emit(skipped(TOOL, file,
                     "no go.mod at or above this file — `go vet` will not load a "
                     "package outside a module, so this file was NOT vetted",
                     ms()))
        return

    try:
        proc = subprocess.run([BINARY, "vet", "."], capture_output=True,
                              text=True, timeout=TIMEOUT, cwd=pkg_dir,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        # Deliberately loud. The binary exists and was invoked; a tool that
        # hangs is a validator failure, and guessing towards silence there is
        # how a broken gate starts looking clean.
        emit(_adapter_error(file, f"go vet timed out after {TIMEOUT}s", ms()))
        return
    except OSError as exc:
        # Windows raises FileNotFoundError [WinError 2] where POSIX may not
        # fail at all (#997). Only the platform's own reason is quoted, so the
        # message reads the same shape everywhere.
        emit(_adapter_error(
            file,
            f"go vet could not be run: {exc.__class__.__name__} — {_os_reason(exc)}",
            ms()))
        return

    output = (proc.stderr or "") + "\n" + (proc.stdout or "")

    if proc.returncode == 0:
        emit({"tool": TOOL, "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": ms()})
        return

    # A refusal the pre-flight above did not predict — a `go.mod` that exists
    # but names a module go will not load, for instance. Quote go's own line.
    if "go.mod file not found" in output or "cannot find main module" in output:
        first = (split_lines(output.strip()) or [""])[0][:200]
        emit(skipped(TOOL, file,
                     "go declined to load a module for this file, so it was NOT "
                     "vetted: " + first, ms()))
        return

    errors = _parse(output, target=target, base=pkg_dir)

    if not errors:
        # Non-zero exit with nothing parseable. Empty output read as "no
        # findings" is #263 — a green meaning "I analysed nothing" is
        # byte-identical to one meaning "I analysed it and it is fine".
        emit(_adapter_error(file, tool_fault(TOOL, proc.returncode, output), ms()))
        return

    published = errors
    if len(errors) > MAX_ERRORS:
        published = errors[:MAX_ERRORS - 1]
        published.append(_truncation_notice(len(errors) - len(published)))

    emit({"tool": TOOL, "file": file, "ok": False, "count": len(errors),
          "errors": published, "duration_ms": ms()})


if __name__ == "__main__":
    main()
