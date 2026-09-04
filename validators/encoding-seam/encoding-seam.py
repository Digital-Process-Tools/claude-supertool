#!/usr/bin/env python3
"""encoding-seam validator adapter -- the #418 tree-wide encoding guard, run
per-file at write time rather than only inside the full CI suite (#2287).

Same shape as `changelog-fragment.py` / `new-file-lint.py`: this adapter
states no rule of its own. It locates the PROJECT's own
`tests/test_encoding_seam.py`, imports its `encoding_violations` /
`subprocess_encoding_violations` functions (never re-implements the AST
walk), and runs them against the single file supertool is validating. A
project with no such test file gets `skipped`, not `ok` -- this is not a
claim that every project has no such convention, only that this one does
and knows how to say so.

Three states: `ok`, a finding, and `skipped` -- no `test_encoding_seam.py`
found above the file, the module could not be imported, or the file could
not be related to the repo root at all.

Usage: encoding-seam.py <file>
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from refusal import guard_main, skipped  # noqa: E402
from encoding_seam import (  # noqa: E402
    config_dir, config_dir_may_authorize_execution, find_test_module,
    load_scan_module, repo_root, scan_one, scope_kinds,
)

TOOL = "encoding-seam"


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:
    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()
    path = Path(file)

    search_from = path.parent if path.parent.exists() else Path.cwd()
    root = repo_root(search_from)
    if root is None:
        emit(skipped(TOOL, file, "could not determine the git repo root "
                     "above this file, so the project's own "
                     "test_encoding_seam.py could not be located",
                     int((time.time() - start) * 1000)))
        return

    cfg_dir, scope_known, scope_reason = config_dir()
    if scope_known and cfg_dir is not None and not config_dir_may_authorize_execution(root, cfg_dir):
        # #2228/#2236, reproduced here: this adapter imports and EXECUTES
        # a script it finds inside `root`, so `root` must be the project
        # that configured supertool -- or nested inside it -- before that
        # import is authorized. A `.supertool.json` sitting above a
        # directory of clones does not authorize running an arbitrary
        # clone's own (possibly attacker-controlled) test file.
        emit(skipped(TOOL, file, "a tests/test_encoding_seam.py may exist "
                     "inside {0}, but the .supertool.json that wired this "
                     "run lives at {1}, which shares no ownership "
                     "relationship with that project -- importing and "
                     "running a script THAT project supplies is not "
                     "authorized just because supertool was pointed at "
                     "one of its files (#2228, #2236)".format(root, cfg_dir),
                     int((time.time() - start) * 1000)))
        return
    if scope_known and cfg_dir is None and scope_reason:
        emit(skipped(TOOL, file, "a tests/test_encoding_seam.py may exist "
                     "inside {0}, but this run's SUPERTOOL_CONFIG_DIR "
                     "could not be used ({1}), so the convention-based "
                     "location cannot be checked against a project "
                     "boundary (#2228)".format(root, scope_reason),
                     int((time.time() - start) * 1000)))
        return

    module_path = find_test_module(root)
    if module_path is None:
        emit(skipped(TOOL, file, "no tests/test_encoding_seam.py found at "
                     "{0} -- this project has not adopted the "
                     "encoding-seam guard".format(root),
                     int((time.time() - start) * 1000)))
        return

    try:
        module = load_scan_module(module_path)
    except Exception as exc:  # the guard module is the project's own
        _adapter_error(file, "{0} could not be imported, so this file was "
                       "NOT checked: {1}: {2}".format(
                           module_path, type(exc).__name__, exc),
                       int((time.time() - start) * 1000))
        return

    try:
        relpath = path.resolve().relative_to(root).as_posix()
    except ValueError:
        emit(skipped(TOOL, file, "{0} is outside the repo root {1}, so the "
                     "tree-wide guard's own scope rules cannot be "
                     "applied".format(file, root),
                     int((time.time() - start) * 1000)))
        return

    kinds = scope_kinds(relpath, getattr(module, "SHIPPED", ()))
    if kinds is None:
        emit(skipped(TOOL, file, "{0} is outside both scopes the tree-wide "
                     "guard checks (tests/, or a SHIPPED directory) -- "
                     "neither half of the rule applies here, the same as "
                     "tests/test_encoding_seam.py's own tree-wide "
                     "enumeration".format(relpath),
                     int((time.time() - start) * 1000)))
        return

    records = scan_one(module, path, kinds)
    dur = int((time.time() - start) * 1000)
    errors = [{"line": r["line"], "col": None, "severity": r["severity"],
               "code": r["code"], "msg": r["msg"]} for r in records]
    # count_basis/errors_truncated (#1728, validators/SCHEMA.md): count is
    # always len(errors) above, and errors is never capped -- same shape as
    # cargo-check's declaration, the 'total'/not-truncated worked example.
    emit({"tool": TOOL, "file": file, "ok": not errors, "count": len(errors),
          "errors": errors, "duration_ms": dur,
          "count_basis": "total", "errors_truncated": False})


if __name__ == "__main__":
    guard_main(TOOL, main)
