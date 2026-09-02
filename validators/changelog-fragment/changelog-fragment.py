#!/usr/bin/env python3
"""changelog-fragment validator adapter — refuse a bad fragment at write time (#1132).

A `changelog.d/<issue>.<section>.md` fragment is written by an ordinary `paste`
or `edit`, so nothing in the write path ever looked at its content. `paste` ran
`git-status`, printed `ok`, and the first thing to disagree was the full CI
matrix twenty minutes later — PR #1115 went red on 14 of 20 legs over one
missing `- `.

**This adapter states no rules of its own.** It imports the project's own
`assemble_changelog.py` and calls the four checks `collect()` calls, in
`collect()`'s order and gathering where it gathers — `parse_fragment_name`,
the empty-body test, `self_reference_finding` (#1251), `scan_fragment_body`
— publishing their messages verbatim. Those messages are already precise, and a second,
thinner description of one rule is how two descriptions drift; a local `ok`
followed by a red matrix would be #1132 inverted, which is worse than #1132.

**Nothing here is keyed on a hardcoded path.** Scope is the `match` glob in the
project's `.supertool.json`, and the rules are the project's own script, found
by walking up from the file, trying every entry in `ASSEMBLER_LOCATIONS` in
order at each parent (`$SUPERTOOL_CHANGELOG_ASSEMBLER` overrides with one
exact path instead of the list — #2072). A project with no such script gets
`skipped`, not `ok`: supertool runs against repos that have never heard of
`changelog.d/`.

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
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from refusal import guard_main, required, required_but_absent, skipped  # noqa: E402
from source_context import context_fields  # noqa: E402

TOOL = "changelog-fragment"

#: Where the project keeps the script that owns these rules. Overridable so the
#: adapter is not asserting one repo's layout as a fact about every repo.
ENV_ASSEMBLER = "SUPERTOOL_CHANGELOG_ASSEMBLER"

#: Known conventions, tried in order. `.github/scripts/...` is this repo's own
#: layout and the adapter's original (and only) guess. `.oss/...` and
#: `scripts/...` are `claude-oss`'s own `ASSEMBLER_LOCATIONS`
#: (`scripts/oss_rules.py:45`) -- what `/oss:scaffold` actually writes for every
#: repository that plugin sets up (#2072). A project using a fourth location
#: still has `SUPERTOOL_CHANGELOG_ASSEMBLER`.
ASSEMBLER_LOCATIONS = (
    os.path.join(".github", "scripts", "assemble_changelog.py"),
    os.path.join(".oss", "assemble_changelog.py"),
    os.path.join("scripts", "assemble_changelog.py"),
)


def emit(payload: dict) -> None:
    print(json.dumps(payload))


def _ms(start: float) -> int:
    return int((time.time() - start) * 1000)


def _locations() -> tuple:
    """The relative path(s) to try, in order.

    `SUPERTOOL_CHANGELOG_ASSEMBLER` names one path and takes it exactly --
    an operator who set it meant that location and no other. Absent that,
    every entry in `ASSEMBLER_LOCATIONS` is tried, because #2072 is what
    happens when only one guess is made and the project uses a different
    one of the two conventions `/oss:scaffold` itself writes.
    """
    override = os.environ.get(ENV_ASSEMBLER, "").strip()
    return (override,) if override else ASSEMBLER_LOCATIONS


def _repo_root(start: Path) -> Path | None:
    """The git repo root above `start`, or None if there is none to find.

    Mirrors `validators/common/ci_lint_resolve_root.py`'s `_repo_root` --
    `git -C <dir> rev-parse --show-toplevel` -- the convention this tree
    already uses for the same question. Every failure mode (`git` absent,
    `git` timed out, `start` not inside a git repository, or `git` reporting
    no toplevel) collapses to `None` here: `_find_assembler` treats "could not
    determine a root" the same as "no root", refusing rather than falling back
    to an unbounded walk. That is stricter than #2177's own three-state
    distinction, and deliberately so -- a validator that cannot even bound
    its own search has no business importing and executing a script it found
    by an unbounded one (#2178).
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    top = r.stdout.strip()
    return Path(top).resolve() if top else None


def _find_assembler(target: Path) -> Path | None:
    """The nearest project script at or above `target`, bounded at the repo
    root, or None.

    Walks parents the same way it always did, but stops at (and including)
    the git repo root above `target` rather than climbing to filesystem root
    with nothing to stop it (#2178): an unbounded walk lets a fragment inside
    one repo pick up and *execute* -- `_load` imports the script it finds --
    a same-named script sitting in a sibling directory or anywhere above the
    repo, which is attacker territory the moment this runs against an
    untrusted checkout.

    No repo root above `target` (not inside a git repository, or `git` itself
    unavailable) refuses outright rather than falling back to the old
    unbounded walk: there is no boundary to bound the search to, and "no
    assembler outside a repo" is the same refusal `_repo_root` already reads
    every failure mode into.
    """
    root = _repo_root(target.parent)
    if root is None:
        return None
    resolved_start = target.parent.resolve()
    for parent in [resolved_start, *resolved_start.parents]:
        for relative in _locations():
            candidate = parent / relative
            if candidate.is_file():
                return candidate
        if parent == root:
            break
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
    err.update(context_fields(target, line))
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
        tried = ", ".join(_locations())
        emit(skipped(TOOL, target,
                     "no assembler found at or above {0} -- tried {1}. That is "
                     "a claim about where this adapter looked, not about the "
                     "project: set {2} to point at the real script if it lives "
                     "somewhere else".format(path.parent, tried, ENV_ASSEMBLER),
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

    # `collect()`'s third stage, and it sits ahead of the body scan there for
    # the same reason it does here: it needs no parser, so an absent
    # markdown-it-py must not turn a definite finding into a `skipped`.
    # Called unqualified, as the other three already are: an assembler missing
    # a symbol this adapter mirrors is a mismatch, and the shape that hides a
    # mismatch is checking one rule fewer without saying so.
    # It does not return here, for the same reason `collect()` does not
    # `continue`: a fragment that is both malformed and silent about its issue
    # has two findings, and reporting one of them at write time while `--check`
    # reports both is the divergence this adapter exists to not have.
    self_ref = asm.self_reference_finding(name, text)
    if self_ref:
        errors.append(_error(target, name, self_ref, "self-reference"))

    try:
        findings = asm.scan_fragment_body(name, text)
    except asm.CannotValidate as exc:
        if self_ref:
            # `collect()` makes the same call: a refusal that needed no parser
            # outranks "could not look", and publishing `skipped` here would
            # drop a definite finding to report an absent tool.
            emit(_verdict(target, errors, start))
            return
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
    guard_main(TOOL, main)
