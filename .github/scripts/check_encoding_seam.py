#!/usr/bin/env python3
"""Scoped, pre-push runner for the #418 encoding-seam guard (#2287).

`tests/test_encoding_seam.py` enforces this tree-wide, but only inside the
full pytest suite -- which only runs in CI, so a violation is caught only
*after* a lane has already pushed and burned a full CI leg (often the ~9-10
minute Windows leg) finding out. Measured 2026-09-04: this guard fired 6
times across 6 different lanes dispatched in one tick, every single time on
the lane's own brand-new test file.

This script runs the SAME two scan functions -- `encoding_violations` and
`subprocess_encoding_violations` -- imported from `tests/test_encoding_seam.py`
via `validators/common/encoding_seam.py` rather than re-implemented, over
just the files a lane actually changed: git-diff'd against a base ref by
default, or named explicitly on argv. Cheap and narrow on purpose -- this
repo's own house style warns that a slow or false-positive local check
teaches lanes to route around it, which is the exact failure this exists to
avoid one level down.

Usage:
    check_encoding_seam.py                 # files changed vs merge-base with origin/<default branch>
    check_encoding_seam.py --base REF      # files changed vs REF (no merge-base lookup)
    check_encoding_seam.py FILE [FILE...]  # explicit files, no git diff at all
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                       / "validators" / "common"))
from encoding_seam import (  # noqa: E402
    find_test_module, load_scan_module, repo_root, scan_one, scope_kinds,
)

DEFAULT_BRANCH_FALLBACK = "master"
TIMEOUT_S = 30

#: `backslashreplace`, the same choice `junit_summary.py` makes and for the
#: same reason: the only input that can reach the handler once this stream
#: is pinned to UTF-8 is a lone surrogate from a `surrogateescape` decode
#: upstream (`_changed_files`, below) -- a filename git could not itself
#: decode as UTF-8. `replace` would destroy that byte a second time, one
#: layer closer to a human than the guard this script exists to enforce.
_STDOUT_ERRORS = "backslashreplace"


def _use_utf8_stdout() -> None:
    """Pin this process's own stdout/stderr to UTF-8, whatever the runner's
    console codepage is (#2288 review, second CI red).

    Measured, not assumed: on `windows-latest` this script's un-reconfigured
    stdout encoded a real, valid, on-disk filename (`test_\xe9_2287.py`) in
    the console's own codepage instead of UTF-8 -- a single byte the test's
    own capture (`encoding="utf-8"`) then could not decode, rendering as
    U+FFFD by the time it reached an assertion. The filename was never
    corrupted on disk or by `_changed_files`' own explicit
    `errors="surrogateescape"` decode of git's `-z` output; the corruption
    was this script's own stdout encode, one step it had not yet pinned --
    exactly the class of mistake `tests/test_encoding_seam.py` exists to
    catch elsewhere in this tree, reproduced in the script built to run
    that guard early. Same fix as `junit_summary.py`'s own
    `use_utf8_stdout`: in-process, not `PYTHONIOENCODING` in the workflow
    `env:`, so it does not also silently change every subprocess this job
    spawns afterward.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors=_STDOUT_ERRORS)
            continue
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:  # pragma: no cover - 3.7+ always has reconfigure
            setattr(sys, name, io.TextIOWrapper(
                buffer, encoding="utf-8", errors=_STDOUT_ERRORS,
                line_buffering=True))

# Three states on the exit code, not two (#2287 review): "ran, clean" must
# be distinguishable from "did not run at all" -- not a git repo, no
# tests/test_encoding_seam.py, no merge-base -- so a caller gating on exit
# status alone (a pre-push wrapper, a CI step) cannot read "nothing to
# check" as "checked and clean". Only RC_OK is a claim that files were
# actually scanned.
RC_OK = 0
RC_VIOLATIONS = 1
RC_COULD_NOT_CHECK = 2


def _default_branch(root: Path) -> str:
    cfg = root / ".oss.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
        branch = data.get("default_branch") if isinstance(data, dict) else None
        if isinstance(branch, str) and branch:
            return branch
    return DEFAULT_BRANCH_FALLBACK


def _merge_base(root: Path, branch: str) -> "str | None":
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "merge-base", "HEAD", "origin/" + branch],
            capture_output=True, timeout=TIMEOUT_S,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and out else None


def _changed_files(root: Path, base: str) -> "list[str] | None":
    """Files a push from `HEAD` would actually carry, relative to `base`.

    Diffed against `HEAD`, not the working tree: an untracked or unstaged
    file never leaves this machine on a push, so it is not this script's
    business, and folding it in would flag files nobody is about to send.

    `-z` rather than plain `--name-only` (#2287 review): git's default
    `core.quotePath=true` C-quotes and octal-escapes a path holding a
    non-ASCII byte -- `"test_\303\251.py"` on stdout for an on-disk
    `test_e-acute.py` -- and `.splitlines()` over that would hand the
    *quoted* string to `(root / f).is_file()`, which is never true, so the
    file is silently dropped from the scan. `-z` NUL-terminates the raw
    bytes unquoted regardless of `core.quotePath`, including for a rename
    under `--diff-filter=ACMR` (`--name-only` never emits the two-path
    rename pair that bare `--name-status -z` does -- verified empirically,
    one path per changed file either way). The child's stdout is decoded
    here explicitly rather than via `text=True`, for the same reason rule 2
    of this repo's own encoding-seam guard exists: `errors="replace"` on a
    filename would silently corrupt the very bytes this scan is scoping
    itself by.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", "-z",
             "--diff-filter=ACMR", base, "HEAD"],
            capture_output=True, timeout=TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print("check-encoding-seam: could not run git diff against {0}: "
              "{1}".format(base, exc), file=sys.stderr)
        return None
    if r.returncode != 0:
        print("check-encoding-seam: git diff against {0} failed: {1}".format(
            base, (r.stderr or b"").decode("utf-8", "replace").strip()),
              file=sys.stderr)
        return None
    return [chunk.decode("utf-8", "surrogateescape")
            for chunk in (r.stdout or b"").split(b"\x00") if chunk]


def main(argv=None) -> int:
    _use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*",
                         help="explicit files to check (skips git diff entirely)")
    parser.add_argument("--base", default=None,
                         help="ref to diff against (default: merge-base with "
                              "origin/<default branch>)")
    args = parser.parse_args(argv)

    root = repo_root(Path.cwd())
    if root is None:
        print("check-encoding-seam: not inside a git repository, nothing to "
              "check", file=sys.stderr)
        return RC_COULD_NOT_CHECK

    module_path = find_test_module(root)
    if module_path is None:
        print("check-encoding-seam: no tests/test_encoding_seam.py found at "
              "{0} -- this project has not adopted the encoding-seam guard, "
              "nothing to run scoped".format(root), file=sys.stderr)
        return RC_COULD_NOT_CHECK

    try:
        module = load_scan_module(module_path)
    except Exception as exc:  # the guard module is the project's own
        print("check-encoding-seam: {0} could not be imported, so nothing "
              "was checked: {1}: {2}".format(
                  module_path, type(exc).__name__, exc), file=sys.stderr)
        return RC_COULD_NOT_CHECK

    if args.files:
        # Explicit paths are resolved against the CALLER's cwd, not `root`
        # (#2287 review): `main()` is invoked from wherever the operator
        # is standing, and a path typed relative to a subdirectory (`cd
        # tests && ../.github/scripts/check_encoding_seam.py test_foo.py`)
        # is valid there, not against `root`. Joining it onto `root`
        # instead silently produced a nonexistent path and dropped the
        # file from the scan with no error -- indistinguishable from
        # "checked, clean".
        candidates = []
        for raw in args.files:
            resolved = Path(raw).resolve()
            try:
                candidates.append(resolved.relative_to(root).as_posix())
            except ValueError:
                print("check-encoding-seam: {0} resolves to {1}, which is "
                      "outside the repo root {2} -- not checked".format(
                          raw, resolved, root), file=sys.stderr)
                return RC_COULD_NOT_CHECK
    else:
        base = args.base
        if base is None:
            branch = _default_branch(root)
            base = _merge_base(root, branch)
            if base is None:
                print("check-encoding-seam: could not find a merge-base with "
                      "origin/{0}, nothing to diff against".format(branch),
                      file=sys.stderr)
                return RC_COULD_NOT_CHECK
        candidates = _changed_files(root, base)
        if candidates is None:
            return RC_COULD_NOT_CHECK

    py_files = [f for f in candidates
                if f.endswith(".py") and (root / f).is_file()]

    all_records = []  # (relpath, record)
    for relpath in py_files:
        kinds = scope_kinds(relpath, module.SHIPPED)
        for record in scan_one(module, root / relpath, kinds):
            all_records.append((relpath, record))

    if not all_records:
        print("check-encoding-seam: {0} changed .py file(s) checked, "
              "clean".format(len(py_files)))
        return RC_OK

    errors = [(p, r) for p, r in all_records if r["severity"] == "error"]
    warnings = [(p, r) for p, r in all_records if r["severity"] != "error"]

    if errors:
        print("check-encoding-seam: encoding-seam violations in changed "
              "files (full rule: tests/test_encoding_seam.py):")
        for relpath, r in errors:
            print("  {0}:{1}: {2}".format(relpath, r["line"], r["msg"]))
    if warnings:
        print("check-encoding-seam: calls the scan cannot judge -- pin "
              "encoding=/errors= literally, or justify in review:")
        for relpath, r in warnings:
            print("  {0}:{1}: {2}".format(relpath, r["line"], r["msg"]))
    return RC_VIOLATIONS


if __name__ == "__main__":
    raise SystemExit(main())
