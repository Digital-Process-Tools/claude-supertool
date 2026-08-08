#!/usr/bin/env python3
"""changelog-fragment validator adapter — refuse a bad fragment at write time (#1132).

A `changelog.d/<issue>.<section>.md` fragment is written by an ordinary `paste`
or `edit`, so nothing in the write path ever looked at its content. `paste` ran
`git-status`, printed `ok`, and the first thing to disagree was the full CI
matrix twenty minutes later — PR #1115 went red on 14 of 20 legs over one
missing `- `.

**This adapter states no rules of its own.** It imports the project's own
`assemble_changelog.py` and calls the three checks `collect()` calls —
`parse_fragment_name`, the empty-body test, `scan_fragment_body` — publishing
their messages verbatim. Those messages are already precise, and a second,
thinner description of one rule is how two descriptions drift; a local `ok`
followed by a red matrix would be #1132 inverted, which is worse than #1132.

**Nothing here is keyed on a hardcoded path.** Scope is the `match` glob in the
project's `.supertool.json`, and the rules are the project's own script, found
by walking up from the file (`$SUPERTOOL_CHANGELOG_ASSEMBLER` overrides the
default location). A project with no such script gets `skipped`, not `ok`:
supertool runs against repos that have never heard of `changelog.d/`.

Three states. `ok`, a finding, and `skipped` when this cannot answer — no
assembler above the file, the file is not a fragment at all, or markdown-it-py
is missing, which is the assembler's own `CannotValidate`.

Usage:  changelog-fragment.py <file>
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from refusal import required, required_but_absent, skipped  # noqa: E402
from source_context import source_context  # noqa: E402

TOOL = "changelog-fragment"

#: Where the project keeps the script that owns these rules. Overridable so the
#: adapter is not asserting one repo's layout as a fact about every repo.
ENV_ASSEMBLER = "SUPERTOOL_CHANGELOG_ASSEMBLER"
DEFAULT_ASSEMBLER = os.path.join(".github", "scripts", "assemble_changelog.py")


def emit(payload: dict) -> None:
    print(json.dumps(payload))


def _ms(start: float) -> int:
    return int((time.time() - start) * 1000)


def _find_assembler(target: Path) -> Path | None:
    """The nearest project script at or above `target`, or None.

    Walks parents rather than resolving against a repo root: a validator is
    handed one path and may be run from anywhere, including a worktree whose
    root is not the directory the call was made from.
    """
    relative = os.environ.get(ENV_ASSEMBLER, "").strip() or DEFAULT_ASSEMBLER
    for parent in [target.parent, *target.parent.parents]:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    return None


def _load(script: Path):
    spec = importlib.util.spec_from_file_location("_st_assemble_changelog", script)
    if spec is None or spec.loader is None:
        raise ImportError("no import spec for {0}".format(script))
    module = importlib.util.module_from_spec(spec)
    sys.modules["_st_assemble_changelog"] = module
    spec.loader.exec_module(module)
    return module


def _line_of(name: str, finding: str) -> int | None:
    """The line number the assembler put in front of its own message.

    `_finding` renders `<name>:<line>: <what>`. Anchored on this file's own
    name and escaped, so a fragment called `1.fixed.md` cannot have a digit
    from its body read as a location.
    """
    match = re.match(r"^{0}:([0-9]+): ".format(re.escape(name)), finding)
    return int(match.group(1)) if match else None


def _error(target: str, name: str, message: str, code: str) -> dict:
    line = _line_of(name, message)
    err = {"line": line, "col": None, "severity": "error",
           "code": code, "msg": message}
    context = source_context(target, line)
    if context:
        err["source_context"] = context
    return err


def _verdict(target: str, errors: list, start: float) -> dict:
    return {"tool": TOOL, "file": target, "ok": not errors, "count": len(errors),
            "errors": errors, "duration_ms": _ms(start)}


def main() -> None:
    start = time.time()
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": TOOL, "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": _ms(start)})
        return

    target = sys.argv[1]
    path = Path(target)
    name = path.name

    script = _find_assembler(path)
    if script is None:
        emit(skipped(TOOL, target,
                     "no {0} at or above {1} — this project does not declare "
                     "changelog fragment rules".format(
                         os.environ.get(ENV_ASSEMBLER, "").strip() or DEFAULT_ASSEMBLER,
                         path.parent),
                     _ms(start)))
        return

    try:
        asm = _load(script)
    except Exception as exc:  # the script is the project's, and may not import
        emit({"tool": TOOL, "file": target, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": "{0} could not be imported, so the fragment was "
                                 "NOT checked: {1}: {2}".format(
                                     script, type(exc).__name__, exc)}],
              "duration_ms": _ms(start)})
        return

    # `collect()` passes over these without a word, so neither does this. They
    # are not fragments and refusing them would make the directory unable to
    # document itself.
    if name in getattr(asm, "_IGNORED", ()) or name.startswith("."):
        emit(skipped(TOOL, target,
                     "{0} is not assembled — the release tool passes over it".format(name),
                     _ms(start)))
        return

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        emit({"tool": TOOL, "file": target, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": "could not read the fragment: {0}".format(exc)}],
              "duration_ms": _ms(start)})
        return

    errors: list = []

    # `collect()` stops at the first stage that refuses — a bad name `continue`s
    # before the body is ever read. Reporting both stages here would be more
    # helpful and would also be a second opinion, which is the one thing this
    # adapter must not have: the counts would differ from `--check`'s on the
    # same file, and the divergence would be invisible until someone compared
    # them. It also lets a definite finding be lost — a bad name plus an absent
    # markdown-it-py reached `CannotValidate` and published `skipped`, dropping
    # a refusal that needed no parser at all.
    try:
        asm.parse_fragment_name(name)
    except asm.BadFragment as exc:
        errors.append(_error(target, name, str(exc), "name"))
        emit(_verdict(target, errors, start))
        return

    if not text.strip():
        errors.append(_error(
            target, name,
            "{0}: fragment is empty — an entry nobody would ever read".format(name),
            "empty"))
        emit(_verdict(target, errors, start))
        return

    try:
        findings = asm.scan_fragment_body(name, text)
    except asm.CannotValidate as exc:
        if required(TOOL):
            emit({"tool": TOOL, "file": target, "ok": False, "count": 1,
                  "errors": [{"line": None, "col": None, "severity": "error",
                              "code": "adapter",
                              "msg": required_but_absent(TOOL, str(exc))}],
                  "duration_ms": _ms(start)})
            return
        emit(skipped(TOOL, target, str(exc), _ms(start)))
        return

    errors.extend(_error(target, name, finding, "shape") for finding in findings)
    emit(_verdict(target, errors, start))


if __name__ == "__main__":
    main()
