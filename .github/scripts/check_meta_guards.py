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
from typing import List, NamedTuple, Optional, Tuple

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

#: `_load_module_from`'s own three states (#2439). The old contract folded
#: "file absent" and "file present but raised during exec_module" into the
#: same `None`, which then propagated into every caller's own `None` --
#: `check_encoding_seam not adopted here` printed for a guard module that
#: DOES exist and DID fail to import. LOAD_OK / LOAD_ABSENT / LOAD_FAILED
#: are the three states a caller can now tell apart.
LOAD_OK = "ok"
LOAD_ABSENT = "absent"
LOAD_FAILED = "failed"


class LoadResult(NamedTuple):
    status: str  # LOAD_OK / LOAD_ABSENT / LOAD_FAILED
    module: object = None
    error: Optional[BaseException] = None


def _load_module_from(root: Path, relpath: str, name: str) -> LoadResult:
    """Import a project file by path, the same trick #965's own scanner test
    uses for cross-file reuse (`importlib.util.spec_from_file_location`).

    Never raises. Returns a `LoadResult` whose `status` distinguishes "the
    file is not there at all" (`LOAD_ABSENT`, the convention was never
    adopted -- `skipped`) from "the file is there and raised while
    importing" (`LOAD_FAILED`, a real problem in an adopted guard module --
    NOT the same thing as absence, however both used to collapse to a bare
    `None` here, see #2439).
    """
    path = root / relpath
    if not path.is_file():
        return LoadResult(LOAD_ABSENT)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return LoadResult(LOAD_ABSENT)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # the project's own file, not this script's business
        return LoadResult(LOAD_FAILED, error=exc)
    return LoadResult(LOAD_OK, module=module)


#: A guard check's own three states, mirrored from `LoadResult` above so a
#: caller never has to re-derive "adopted" from a bare truthiness check on
#: `findings` (an adopted-and-clean guard has an EMPTY findings list, which
#: is falsy and must not be mistaken for "not adopted").
GUARD_ADOPTED = "adopted"
GUARD_ABSENT = "absent"
GUARD_LOAD_ERROR = "load_error"


class GuardResult(NamedTuple):
    status: str  # GUARD_ADOPTED / GUARD_ABSENT / GUARD_LOAD_ERROR
    # `NamedTuple` evaluates a default ONCE at class-definition time and
    # every instance that omits the field shares that same object by
    # reference -- unlike a dataclass `field(default_factory=list)`. A bare
    # `findings: list = []` here would mean every GUARD_ABSENT/GUARD_LOAD_ERROR
    # return in this file shares one list, so an in-place `.append()` on any
    # one of them would silently leak into every other (#2439 review). None
    # is the safe default; only meaningful when status == GUARD_ADOPTED, in
    # which case every call site below passes its own fresh list explicitly.
    findings: Optional[list] = None
    error: Optional[BaseException] = None  # only meaningful when GUARD_LOAD_ERROR


# ---------------------------------------------------------------------------
# 1. env-scrub -- tests/_pathenv_scan.py, scoped to changed test_*.py files
# ---------------------------------------------------------------------------

def check_env_scrub(root: Path, py_files: List[str]) -> GuardResult:
    """`GUARD_ABSENT` when the project has not adopted the guard; else its
    findings under `GUARD_ADOPTED` -- or `GUARD_LOAD_ERROR` when the guard
    module exists but raised while importing (#2439).

    Scoped to the guard's own population: `scan_tree` in
    `tests/test_handrolled_path_env_guard_1151.py` only ever looks at
    `tests/test_*.py`, so a changed file outside that set is not this
    check's business either.
    """
    load = _load_module_from(root, "tests/_pathenv_scan.py",
                              "meta_guard_pathenv_scan")
    if load.status == LOAD_ABSENT:
        return GuardResult(GUARD_ABSENT)
    if load.status == LOAD_FAILED:
        return GuardResult(GUARD_LOAD_ERROR, error=load.error)
    module = load.module
    findings = []
    for relpath in py_files:
        name = Path(relpath).name
        if not (relpath.startswith("tests/") and name.startswith("test_")):
            continue
        path = root / relpath
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        findings.extend(module.scan_source(text, relpath))
    return GuardResult(GUARD_ADOPTED, findings=findings)


# ---------------------------------------------------------------------------
# 2. splitlines register -- tests/test_preset_git_splitlines_register_1130.py
# ---------------------------------------------------------------------------

def check_splitlines_register(root: Path, py_files: List[str]) -> GuardResult:
    """`GUARD_ABSENT` when the register file is absent (or present but not
    shaped like the real register); `GUARD_ADOPTED` with `(path, key,
    lines)` findings for every changed `presets/git/` call site the
    register does not name; `GUARD_LOAD_ERROR` when the register module
    exists but raised while importing (#2439).
    """
    load = _load_module_from(
        root, "tests/test_preset_git_splitlines_register_1130.py",
        "meta_guard_splitlines_register")
    if load.status == LOAD_ABSENT:
        return GuardResult(GUARD_ABSENT)
    if load.status == LOAD_FAILED:
        return GuardResult(GUARD_LOAD_ERROR, error=load.error)
    module = load.module
    register = getattr(module, "REGISTER", None)
    visitor_cls = getattr(module, "_Visitor", None)
    if register is None or visitor_cls is None:
        # Imported fine but is not actually the register module -- that is
        # "not the convention this check needs", the same as absence, and
        # distinct from an import that raised.
        return GuardResult(GUARD_ABSENT)
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
    return GuardResult(GUARD_ADOPTED, findings=offenders)


# ---------------------------------------------------------------------------
# 3. encoding-seam -- delegated to check_encoding_seam.py, not re-implemented
# ---------------------------------------------------------------------------

def check_encoding_seam(root: Path, py_files: List[str]) -> GuardResult:
    """`GUARD_ABSENT` when the guard test module is absent; `GUARD_ADOPTED`
    with its records when it loaded; `GUARD_LOAD_ERROR` when the module
    exists but raised while importing -- previously collapsed into the same
    `None` as absence (#2439).
    """
    module_path = find_test_module(root)
    if module_path is None:
        return GuardResult(GUARD_ABSENT)
    try:
        module = load_scan_module(module_path)
    except Exception as exc:
        return GuardResult(GUARD_LOAD_ERROR, error=exc)
    records = []
    for relpath in py_files:
        kinds = scope_kinds(relpath, module.SHIPPED)
        for record in scan_one(module, root / relpath, kinds):
            records.append((relpath, record))
    return GuardResult(GUARD_ADOPTED, findings=records)


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

    checked_names: List[str] = []
    load_errors: List[Tuple[str, str, BaseException]] = []
    violated = False

    env_result = check_env_scrub(root, py_files)
    if env_result.status == GUARD_ADOPTED:
        checked_names.append("env-scrub")
        violations = [f for f in env_result.findings if f.kind == "violation"]
        unresolved = [f for f in env_result.findings if f.kind == "unresolved"]
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
    elif env_result.status == GUARD_LOAD_ERROR:
        load_errors.append(("env-scrub", "tests/_pathenv_scan.py",
                             env_result.error))
        print("check-meta-guards: env-scrub guard module present but "
              "failed to import (tests/_pathenv_scan.py) -- {0}: {1} -- "
              "this is could-not-check, NOT the same as 'not adopted'"
              .format(type(env_result.error).__name__, env_result.error),
              file=sys.stderr)
    else:
        print("check-meta-guards: env-scrub guard not adopted here "
              "(no tests/_pathenv_scan.py) -- skipped, not clean",
              file=sys.stderr)

    split_result = check_splitlines_register(root, py_files)
    if split_result.status == GUARD_ADOPTED:
        checked_names.append("splitlines-register")
        split_offenders = split_result.findings
        if split_offenders:
            violated = True
            print("check-meta-guards: new str.splitlines() in presets/git/ "
                  "not in REGISTER (full rule: "
                  "tests/test_preset_git_splitlines_register_1130.py):")
            for relpath, key, lines in split_offenders:
                print("  {0}::{1} lines {2}".format(relpath, key, lines))
    elif split_result.status == GUARD_LOAD_ERROR:
        load_errors.append(
            ("splitlines-register",
             "tests/test_preset_git_splitlines_register_1130.py",
             split_result.error))
        print("check-meta-guards: splitlines-register guard module present "
              "but failed to import "
              "(tests/test_preset_git_splitlines_register_1130.py) -- "
              "{0}: {1} -- this is could-not-check, NOT the same as 'not "
              "adopted'".format(type(split_result.error).__name__,
                                 split_result.error),
              file=sys.stderr)
    else:
        print("check-meta-guards: splitlines-register guard not adopted "
              "here -- skipped, not clean", file=sys.stderr)

    seam_result = check_encoding_seam(root, py_files)
    if seam_result.status == GUARD_ADOPTED:
        checked_names.append("encoding-seam")
        seam_records = seam_result.findings
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
    elif seam_result.status == GUARD_LOAD_ERROR:
        load_errors.append(("encoding-seam", "tests/test_encoding_seam.py",
                             seam_result.error))
        print("check-meta-guards: encoding-seam guard module present but "
              "failed to import (tests/test_encoding_seam.py) -- {0}: {1} "
              "-- this is could-not-check, NOT the same as 'not adopted'"
              .format(type(seam_result.error).__name__, seam_result.error),
              file=sys.stderr)
    else:
        print("check-meta-guards: encoding-seam guard not adopted here "
              "(no tests/test_encoding_seam.py) -- skipped, not clean",
              file=sys.stderr)

    if not checked_names and not load_errors:
        print("check-meta-guards: none of the three guards this script "
              "knows about are adopted in this repo -- nothing was checked",
              file=sys.stderr)
        return RC_COULD_NOT_CHECK

    if load_errors:
        # An adopted guard module that cannot even import is a real problem,
        # not a transient hiccup to shrug past -- silently letting it
        # through (RC_OK) is exactly the "broken guard slips by" failure
        # #2439 warns against. Fail loudly and non-zero rather than folding
        # it into either a clean pass or an ordinary pattern violation.
        print("check-meta-guards: {0} guard module(s) failed to import -- "
              "could-not-check, never a silent clean: {1}".format(
                  len(load_errors),
                  ", ".join(name for name, _relpath, _exc in load_errors)),
              file=sys.stderr)
        return RC_COULD_NOT_CHECK

    if violated:
        return RC_VIOLATIONS

    print("check-meta-guards: {0} changed .py file(s), {1} guard(s) checked "
          "({2}), clean".format(
              len(py_files), len(checked_names), ", ".join(checked_names)))
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
