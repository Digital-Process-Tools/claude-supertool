#!/usr/bin/env python3
"""Local #2155 gate -- re-enable F401/F841/F541 for a file with no HEAD blob.

`pyproject.toml` ignores F401/F841/F541 tree-wide, deliberately (#1481, #797:
263 pre-existing findings in files with a history nobody is being asked to
pay down). `.github/scripts/lint_new_files.py` re-enables exactly those three
for the files a PR adds, copies or renames -- CI's `lint (files this PR adds,
renames or modifies)` leg. No *local* route re-enabled them: `validate:PATH`
(the shared `ruff` validator) inherits the tree-wide ignore same as a bare
`ruff check`, because it deliberately never hardcodes a ruleset the project
has not adopted (see validators/ruff/ruff.py's own docstring). #2155 measured
the cost of that gap directly -- three separate lanes shipped a new test file
carrying an unused import in one session, each green on every local check,
each red on the CI leg after a full push-and-wait round trip.

This is the missing local route, scoped to the case #2155 was filed for and
no wider: a file with **no blob at `HEAD`** -- never committed under this
path before, `lint_new_files.py`'s `ACR` (added/copied/renamed) case,
re-derived against `HEAD` rather than a PR base ref because there is no PR
here, only a working tree. A file that already has history is left to the
shared `ruff` validator and the CI leg's own baseline diff (#1849) -- that
comparison needs a merge-base, which this validator does not have and must
not guess at.

`EXTRA_RULES` is imported from `lint_new_files.py` rather than copied, so the
two cannot drift the way a hand-duplicated constant does.

Usage: new_file_ruff_gate.py <file>
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "validators" / "common"))
from refusal import absent, guard_main, skipped, tool_fault  # noqa: E402

sys.path.insert(0, str(_HERE))
from lint_new_files import EXTRA_RULES  # noqa: E402

TOOL = "new-file-lint"

INSTALL_HINT = "ruff not found on PATH — pip install ruff"

# Same budget as the shared ruff validator (validators/ruff/ruff.py) plus the
# `git` probe below -- two spawns, neither of them ruff's own selling point
# (milliseconds), so this stays a hang-guard rather than a performance floor.
TIMEOUT_S = 30

RC_CLEAN = 0
RC_FINDINGS = 1

def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:
    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


def _git(args, cwd: str, timeout: float = TIMEOUT_S):
    """`(returncode, stdout, stderr)`, or `None` if git could not be spawned
    at all -- absent, no permission, or an OS that could not exec it. `None`
    is not "not a repo" or "not found": those are ordinary non-zero exits and
    stay distinguishable from a git that never ran."""
    try:
        r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8",
                           errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode, r.stdout or "", r.stderr or ""


def _is_new_at_head(abspath: str) -> "tuple[bool | None, str]":
    """`(is_new, reason_if_None)`.

    `is_new` is `True` when `HEAD:<path-relative-to-repo-root>` has no blob --
    this path was never committed, so it carries none of the 263 pre-existing
    findings the tree-wide ignore exists for. `False` when it does. `None`
    when the question could not be answered at all (no git, not a repo, the
    probe timed out, or an unborn `HEAD` with nothing to compare against) --
    never guessed at either way, because guessing `False` would silently
    exempt a genuinely new file and guessing `True` would re-surface debt on
    a file this validator has no business relitigating.
    """
    directory = os.path.dirname(abspath) or "."
    root_probe = _git(["rev-parse", "--show-toplevel"], cwd=directory)
    if root_probe is None:
        return None, "git could not be spawned to resolve the repository root"
    rc, out, err = root_probe
    if rc != 0:
        return None, "not inside a git repository: " + (err.strip() or out.strip())
    root = out.strip()
    if not root:
        return None, "git reported an empty repository root"
    try:
        rel = os.path.relpath(abspath, root).replace(os.sep, "/")
    except ValueError as exc:
        # Windows: the file and the repo root are on different drives, so
        # there is no relative path between them at all.
        return None, "could not relate {0!r} to repo root {1!r}: {2}".format(
            abspath, root, exc)
    head_probe = _git(["cat-file", "-e", "HEAD:" + rel], cwd=root)
    if head_probe is None:
        return None, "git could not be spawned to probe HEAD"
    rc, _out, err = head_probe
    if rc == 0:
        return False, ""
    # Anything else -- no such path at HEAD, or no HEAD at all (unborn
    # branch, brand-new repo) -- means this path carries no history to be
    # exempt from checking, which is exactly the ACR case this gate covers.
    return True, ""


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("ruff"):
        emit(absent(TOOL, file, INSTALL_HINT, int((time.time() - start) * 1000)))
        return

    is_new, reason = _is_new_at_head(os.path.abspath(file))
    if is_new is None:
        emit(skipped(TOOL, file,
                     "could not determine whether this path has history at "
                     "HEAD, so whether the tree-wide ignore applies to it is "
                     "unknown: " + reason,
                     int((time.time() - start) * 1000)))
        return
    if not is_new:
        emit(skipped(TOOL, file,
                     "this path already has a commit at HEAD -- the "
                     "tree-wide F401/F841/F541 ignore covers it locally the "
                     "same as the shared `ruff` validator; a new finding "
                     "introduced by THIS edit is CI's `lint (files this PR "
                     "adds, renames or modifies)` leg's job, via its own "
                     "merge-base diff, which this validator has no PR base "
                     "ref to reproduce",
                     int((time.time() - start) * 1000)))
        return

    cmd = ["ruff", "check", "--output-format", "json", "--no-cache",
           "--force-exclude", "--quiet", "--extend-select", ",".join(EXTRA_RULES),
           "--", file]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT_S, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        emit(absent(TOOL, file, "ruff on PATH but could not be executed",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        _adapter_error(file, "timeout — ruff did not return within "
                             f"{TIMEOUT_S}s; the file was NOT checked",
                       int((time.time() - start) * 1000))
        return

    dur = int((time.time() - start) * 1000)
    body = (r.stdout or "").strip()
    if r.returncode not in (RC_CLEAN, RC_FINDINGS) and not body:
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        r.stderr or r.stdout or ""), dur)
        return

    try:
        items = json.loads(body) if body else []
    except ValueError:
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        r.stdout or r.stderr or ""), dur)
        return

    if not isinstance(items, list):
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        "expected a JSON array, got "
                                        f"{type(items).__name__}"), dur)
        return

    errors = []
    for item in items:
        if not isinstance(item, dict):
            continue
        location = item.get("location") or {}
        errors.append({
            "line": location.get("row"), "col": location.get("column"),
            "severity": "warning", "code": item.get("code"),
            "msg": (item.get("message") or "").strip().replace("\n", " ")[:300],
        })

    emit({"tool": TOOL, "file": file, "ok": not errors, "count": len(errors),
          "errors": errors, "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    guard_main(TOOL, main)
