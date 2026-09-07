#!/usr/bin/env python3
"""Scoped, pre-push runner for three tree-wide meta-guards (#2263).

Four instances of the same shape in one day (2026-09-04): a lane writes new
test/preset code that trips a tree-wide guard -- a scanner that walks the
whole tree for a known-bad pattern and asserts none of it is new -- and finds
out only after a push, a wait, and a red CI leg (often the ~9-10 minute
Windows one). `.github/scripts/check_encoding_seam.py` (#2288/#2287) already
does this for one guard family. This script does the same for the other two
named in #2263 -- the `_winenv.empty_path_env()` env-scrub pattern
(`tests/test_handrolled_path_env_guard_1151.py`) and the `presets/git/`
splitlines register (`tests/test_preset_git_splitlines_register_1130.py`) --
and calls into `check_encoding_seam` for the first, so one command covers
all three instead of three separately-remembered ones.

Each check imports (never re-implements) the scan logic the real guard test
already carries, over just the files a push would actually send -- same
git-diff scoping as `check_encoding_seam.py`, reused from it directly rather
than duplicated.

Deliberately narrow. #2263's own thread found three MORE guard families the
same day (preset-global-lifetimes, hint-register, state-reset) that this
script does not cover -- naming them here so the gap is a line someone can
read rather than a silent "meta-guards, solved". Widening this script is a
design decision #2263 leaves open, not something this change presumes to
settle.

Usage:
    check_meta_guards.py                 # files changed vs merge-base with origin/<default branch>
    check_meta_guards.py --base REF      # files changed vs REF
    check_meta_guards.py FILE [FILE...]  # explicit files, no git diff at all
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from pathlib import Path
from typing import List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "validators" / "common"))

import check_encoding_seam as _ces  # noqa: E402
from encoding_seam import (  # noqa: E402
    find_test_module, load_scan_module, scan_one, scope_kinds,
)

# Three states, never two (the class this repo has filed more than any
# other): "ran, clean" must never render the same as "did not run at all".
RC_OK = 0
RC_VIOLATIONS = 1
RC_COULD_NOT_CHECK = 2


def _load_module_from(root: Path, relpath: str, name: str):
    """Import a project file by path, the same trick #965's own scanner test
    uses for cross-file reuse (`importlib.util.spec_from_file_location`).

    Returns `None` -- never raises -- when the file is absent or fails to
    import: absence of the convention here is `skipped`, not a crash.
    """
    path = root / relpath
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:  # the project's own file, not this script's business
        return None
    return module


# ---------------------------------------------------------------------------
# 1. env-scrub -- tests/_pathenv_scan.py, scoped to changed test_*.py files
# ---------------------------------------------------------------------------

def check_env_scrub(root: Path, py_files: List[str]) -> Optional[List]:
    """`None` when the project has not adopted the guard; else its findings.

    Scoped to the guard's own population: `scan_tree` in
    `tests/test_handrolled_path_env_guard_1151.py` only ever looks at
    `tests/test_*.py`, so a changed file outside that set is not this
    check's business either.
    """
    module = _load_module_from(root, "tests/_pathenv_scan.py",
                                "meta_guard_pathenv_scan")
    if module is None:
        return None
    findings = []
    for relpath in py_files:
        name = Path(relpath).name
        if not (relpath.startswith("tests/") and name.startswith("test_")):
            continue
        path = root / relpath
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        findings.extend(module.scan_source(text, relpath))
    return findings


# ---------------------------------------------------------------------------
# 2. splitlines register -- tests/test_preset_git_splitlines_register_1130.py
# ---------------------------------------------------------------------------

def check_splitlines_register(root: Path, py_files: List[str]
                              ) -> Optional[List[Tuple[str, str, List[int]]]]:
    """`None` when the register file is absent; else `(path, key, lines)` for
    every changed `presets/git/` call site the register does not name.
    """
    module = _load_module_from(
        root, "tests/test_preset_git_splitlines_register_1130.py",
        "meta_guard_splitlines_register")
    if module is None:
        return None
    register = getattr(module, "REGISTER", None)
    visitor_cls = getattr(module, "_Visitor", None)
    if register is None or visitor_cls is None:
        return None
    offenders = []
    for relpath in py_files:
        if not relpath.startswith("presets/git/"):
            continue
        path = root / relpath
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            offenders.append((relpath, "<unreadable>", [getattr(exc, "lineno", 0) or 0]))
            continue
        found: dict = {}
        visitor_cls(relpath, found).visit(tree)
        for key, lines in sorted(found.items()):
            if key not in register:
                offenders.append((relpath, key, lines))
    return offenders


# ---------------------------------------------------------------------------
# 3. encoding-seam -- delegated to check_encoding_seam.py, not re-implemented
# ---------------------------------------------------------------------------

def check_encoding_seam(root: Path, py_files: List[str]) -> Optional[List]:
    module_path = find_test_module(root)
    if module_path is None:
        return None
    try:
        module = load_scan_module(module_path)
    except Exception:
        return None
    records = []
    for relpath in py_files:
        kinds = scope_kinds(relpath, module.SHIPPED)
        for record in scan_one(module, root / relpath, kinds):
            records.append((relpath, record))
    return records


def main(argv=None) -> int:
    _ces._use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*",
                         help="explicit files to check (skips git diff entirely)")
    parser.add_argument("--base", default=None,
                         help="ref to diff against (default: merge-base with "
                              "origin/<default branch>)")
    args = parser.parse_args(argv)

    root = _ces.repo_root(Path.cwd())
    if root is None:
        print("check-meta-guards: not inside a git repository, nothing to "
              "check", file=sys.stderr)
        return RC_COULD_NOT_CHECK

    if args.files:
        candidates = []
        for raw in args.files:
            resolved = Path(raw).resolve()
            try:
                candidates.append(resolved.relative_to(root).as_posix())
            except ValueError:
                print("check-meta-guards: {0} resolves to {1}, which is "
                      "outside the repo root {2} -- not checked".format(
                          raw, resolved, root), file=sys.stderr)
                return RC_COULD_NOT_CHECK
    else:
        base = args.base
        if base is None:
            branch = _ces._default_branch(root)
            base = _ces._merge_base(root, branch)
            if base is None:
                print("check-meta-guards: could not find a merge-base with "
                      "origin/{0}, nothing to diff against".format(branch),
                      file=sys.stderr)
                return RC_COULD_NOT_CHECK
        candidates = _ces._changed_files(root, base)
        if candidates is None:
            return RC_COULD_NOT_CHECK

    py_files = [f for f in candidates
                if f.endswith(".py") and (root / f).is_file()]

    checked_any = False
    violated = False

    env_findings = check_env_scrub(root, py_files)
    if env_findings is not None:
        checked_any = True
        violations = [f for f in env_findings if f.kind == "violation"]
        unresolved = [f for f in env_findings if f.kind == "unresolved"]
        if violations:
            violated = True
            print("check-meta-guards: env-scrub violations "
                  "(full rule: tests/test_handrolled_path_env_guard_1151.py):")
            for f in violations:
                print("  {0}".format(f.describe()))
        if unresolved:
            print("check-meta-guards: env= expressions the scanner cannot "
                  "read -- not a violation, but not clean either; read "
                  "DECLARED_UNRESOLVED in the guard test for the pattern:")
            for f in unresolved:
                print("  {0}".format(f.describe()))
    else:
        print("check-meta-guards: env-scrub guard not adopted here "
              "(no tests/_pathenv_scan.py) -- skipped, not clean",
              file=sys.stderr)

    split_offenders = check_splitlines_register(root, py_files)
    if split_offenders is not None:
        checked_any = True
        if split_offenders:
            violated = True
            print("check-meta-guards: new str.splitlines() in presets/git/ "
                  "not in REGISTER (full rule: "
                  "tests/test_preset_git_splitlines_register_1130.py):")
            for relpath, key, lines in split_offenders:
                print("  {0}::{1} lines {2}".format(relpath, key, lines))
    else:
        print("check-meta-guards: splitlines-register guard not adopted "
              "here -- skipped, not clean", file=sys.stderr)

    seam_records = check_encoding_seam(root, py_files)
    if seam_records is not None:
        checked_any = True
        errors = [(p, r) for p, r in seam_records if r["severity"] == "error"]
        warnings = [(p, r) for p, r in seam_records if r["severity"] != "error"]
        if errors:
            violated = True
            print("check-meta-guards: encoding-seam violations "
                  "(full rule: tests/test_encoding_seam.py):")
            for relpath, r in errors:
                print("  {0}:{1}: {2}".format(relpath, r["line"], r["msg"]))
        if warnings:
            print("check-meta-guards: encoding-seam calls the scan cannot "
                  "judge -- pin encoding=/errors= literally, or justify in "
                  "review:")
            for relpath, r in warnings:
                print("  {0}:{1}: {2}".format(relpath, r["line"], r["msg"]))
    else:
        print("check-meta-guards: encoding-seam guard not adopted here "
              "(no tests/test_encoding_seam.py) -- skipped, not clean",
              file=sys.stderr)

    if not checked_any:
        print("check-meta-guards: none of the three guards this script "
              "knows about are adopted in this repo -- nothing was checked",
              file=sys.stderr)
        return RC_COULD_NOT_CHECK

    if violated:
        return RC_VIOLATIONS

    print("check-meta-guards: {0} changed .py file(s), 3 guards checked "
          "(env-scrub, splitlines-register, encoding-seam), clean"
          .format(len(py_files)))
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
