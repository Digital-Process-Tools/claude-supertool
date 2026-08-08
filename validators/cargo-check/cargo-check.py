#!/usr/bin/env python3
"""cargo-check validator adapter — Rust compilation check via `cargo check`.

NOTE: This validator compiles the entire crate — it can be slow (5-30s on first
run). Consider disabling it for hot-loop editing via `opt_in: true` in your
.supertool.json validator config.

Requires cargo (ships with Rust). If missing, exits 0 with a stderr warning.
Finds Cargo.toml by walking up from the .rs file. Skips if none found.
Usage:  cargo-check.py <file>
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context
from refusal import tool_fault


def emit(d: dict) -> None:
    print(json.dumps(d))


def _find_crate_root(file: str) -> Path | None:
    """Walk parent dirs from file until Cargo.toml is found."""
    p = Path(file).resolve().parent
    while True:
        if (p / "Cargo.toml").exists():
            return p
        parent = p.parent
        if parent == p:
            return None
        p = parent


def _canon(path: object, normcase: Callable[[str], str] | None = None) -> str:
    """A path in one comparable form: folded for case, separated by `/` (#754).

    **The separator is normalised after the fold, never before.**
    `os.path.normcase` is the only stdlib call that knows whether this platform
    is case-insensitive, and on Windows it does a second thing its name does not
    advertise: it rewrites every `/` into `\\`. The first version of this fix
    replaced separators and then folded, so on Windows both sides came back
    backslash-separated while the suffix rule below still looked for a `/`
    boundary - no diagnostic could match its own file, every finding was demoted
    to a non-verdict, and three CI legs went red on the exact regression this
    module exists to prevent.

    `normcase` is injectable for the reason `refusal.daemon_transport_reason`
    takes `has_uds`: a platform behaviour asserted only on the platform that has
    it is asserted only where it was already going to be noticed. Passing
    `ntpath.normcase` reproduces Windows semantics on every runner.

    Backslashes are folded to `/` on POSIX too, where a backslash is a legal
    filename character. That is deliberate: cargo never emits one as a
    separator, and a rule that behaves differently per platform is a rule no
    test can pin from one platform.
    """
    fold = normcase or os.path.normcase
    return fold(str(path)).replace("\\", "/")


MIN_MATCH_SEGMENTS = 2


def _is_abs(path: str) -> bool:
    """Absolute under either platform's rules, whichever one we are running on.

    `os.path.isabs` answers for the host, and the host is not always the
    platform the path came from: a Windows-shaped `D:\\ws\\src\\main.rs` reaches
    these tests, and cargo's own output, from a runner this file also has to
    pass on. Both spellings are asked, for the same reason `_canon` takes an
    injectable fold - a rule that behaves differently per platform is a rule no
    test can pin from one platform.
    """
    return ntpath.isabs(path) or posixpath.isabs(path)


def _tail_match(a: str, b: str) -> bool:
    """Do two canonical paths name the same file, one possibly a tail of the
    other? Symmetric, because either side may be the relative one: cargo prints
    a relative path for a workspace member and an absolute one elsewhere.

    Two rules, and #1037 is the second one missing:

    * The boundary is a separator, never a substring: `src/xmain.rs` ends with
      the characters of `main.rs` and is a different file.
    * **A tail has to be long enough to identify something.** One segment is a
      basename, and a basename names a file in every directory of the tree at
      once. `main.rs` matched `crates/other/src/main.rs`, the diagnostic kept
      its rustc code - so it was a finding, not a non-verdict - and
      `rollback_on_fail` reverted a correct edit over another crate's
      pre-existing error.

    The floor costs a verdict only where the diagnostic genuinely cannot be
    placed: a one-segment cargo path means a file directly in the workspace
    root, and nothing in the output says whether that root is the directory
    holding the target. Declining there is the third state doing its job; the
    diagnostic is still printed in full by `_elsewhere_in_crate`, and a
    non-verdict never deletes an edit.
    """
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if not longer.endswith("/" + shorter):
        return False
    return shorter.count("/") + 1 >= MIN_MATCH_SEGMENTS


def _same_file(src_file: str, target: Path, target_raw: str = "",
               normcase: Callable[[str], str] | None = None) -> bool:
    """Is the path cargo printed the file the adapter was asked about? (#754)

    A **path-suffix match on segment boundaries**, deliberately not a join
    against the crate root. cargo prints diagnostic paths relative to the
    *workspace* root, which is not the directory it was invoked in: run from
    `ws/member`, cargo 1.97 reports `member/src/sib.rs`, so `crate_root /
    src_file` yields `ws/member/member/src/sib.rs` and matches nothing. Every
    real finding about the file under validation would then fail the comparison
    and be demoted to a non-verdict - the same misreport pointing the other way,
    and the quieter of the two.

    A suffix match needs no base and touches no disk, so it holds for a
    crate-relative path, a workspace-relative one and an absolute one alike.

    **Nothing here rests on two `resolve()` calls agreeing character for
    character**, which is why `target_raw` is compared alongside the resolved
    `target`. `os.path.abspath` and `Path.resolve()` are not the same function
    on Windows: `abspath` joins onto the working directory, while `resolve()`
    goes through `_getfinalpathname` and returns the canonical on-disk spelling
    of whatever prefix exists, following `subst` and symlinked drives on the
    way. Comparing an absolute diagnostic path against only the resolved target
    made the answer depend on that difference; comparing against both forms
    under one suffix rule does not.
    """
    src = (src_file or "").strip()
    if not src:
        return False

    src_forms = {posixpath.normpath(_canon(src, normcase))}
    if os.path.isabs(src):
        try:
            src_forms.add(_canon(Path(src).resolve(), normcase))
        except OSError:
            pass

    return any(_tail_match(s, t)
               for s in src_forms
               for t in _target_forms(target, target_raw, normcase))


def _target_forms(target: Path | str, target_raw: str = "",
                  normcase: Callable[[str], str] | None = None) -> set:
    """Every spelling of the file under validation - all of them absolute (#1037).

    The suffix rule has one side that may be relative, and it is cargo's, whose
    base is the workspace root. The *target* side has no such excuse: the
    adapter knows the working directory it was invoked in, so a relative
    argument can be anchored to it rather than compared as a floating tail.

    Compared as a floating tail is what #1037 was. `target_raw` went in exactly
    as the caller typed it, so two relative paths were suffix-matched against
    each other with no common base at all:
    `vendor/crates/foo/src/main.rs` "was" `crates/foo/src/main.rs`, and
    `/abs/elsewhere/src/lib.rs` "was" `src/lib.rs` - a foreign file's error
    charged to this one, with a real error code, on a `rollback_on_fail`
    validator.

    Anchoring is not the join #754 refused. That join put `crate_root` in front
    of *cargo's* path, whose base is the workspace root, and so double-counted
    the member directory and demoted every real finding. This puts the working
    directory in front of the *caller's* path, whose base is the working
    directory, and changes no comparison that was previously right.

    Both forms are kept for the reason #754 gave: `os.path.abspath` joins onto
    the working directory while `Path.resolve()` goes through
    `_getfinalpathname` on Windows and returns the canonical on-disk spelling,
    following `subst` and symlinked drives. They disagree, and the answer must
    not depend on which one a caller happened to produce.
    """
    forms = set()
    for raw in (target, target_raw):
        text = str(raw or "").strip()
        if not text:
            continue
        if not _is_abs(text):
            text = os.path.abspath(text)
        forms.add(posixpath.normpath(_canon(text, normcase)))
    return forms


def _elsewhere_in_crate(src_file: str, ln: int, col: int, code: str, msg: str) -> str:
    """The message for a crate error this file did not cause (#754).

    It is not filtered out. The crate genuinely does not build, and a caller
    told nothing about that cannot act on it; suppressing the diagnostic would
    trade a misreport for a silent loss, which is the worse of the two. What
    changes is only the claim about *which* file caused it - the real location
    goes in the text, where it can be read and acted on but never mistaken for a
    line of the file under validation.
    """
    label = f"error[{code}]" if code and code != "compile" else "error"
    return (f"cargo check reported {label} at {src_file}:{ln}:{col}, which is "
            f"not this file - the crate does not compile, so no verdict was "
            f"produced about this file: {msg}")


def _parse_errors(output: str, target_file: str) -> list[dict]:
    """Extract error lines from cargo --message-format=short output.

    `cargo check` analyses the whole crate, so its output carries diagnostics
    about every file in it. Those that name `target_file` are findings and keep
    their line, column and rustc code. Those that name another file keep their
    text and lose their location: `code: "adapter"`, the code reserved across
    every adapter for "no verdict was obtained about this file", with `line` and
    `col` null because a finding that cannot be placed here does not borrow a
    number, and no `source_context` because there is no line of this file to
    render (#754).
    """
    errors = []
    target = Path(target_file).resolve()
    # Short format: "path/to/file.rs:LINE:COL: error[EXXXX]: message"
    pattern = re.compile(r"^(.+?):(\d+):(\d+):\s+(error|warning)\[?([^\]]*)\]?:\s+(.+)$")
    for line in output.splitlines():
        m = pattern.match(line)
        if m:
            severity = m.group(4)
            if severity != "error":
                continue
            src_file = m.group(1)
            ln = int(m.group(2))
            col = int(m.group(3))
            code = m.group(5) or "compile"
            msg = m.group(6).strip()[:300]
            if _same_file(src_file, target, target_file):
                # Context is read from the target the adapter was handed, never
                # from a path rebuilt out of cargo's output. The old code passed
                # `src_file` straight through, so a crate-relative path was
                # resolved against wherever the adapter happened to be running
                # and the context came back empty for a file that was right
                # there.
                errors.append({
                    "line": ln,
                    "col": col,
                    "severity": "error",
                    "code": code,
                    "msg": msg,
                    "source_context": source_context(str(target), ln),
                })
            else:
                errors.append({
                    "line": None,
                    "col": None,
                    "severity": "error",
                    "code": "adapter",
                    "msg": _elsewhere_in_crate(src_file, ln, col, code, msg),
                })
    return errors


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "cargo-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("cargo"):
        print("cargo-check: cargo not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "cargo-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    crate_root = _find_crate_root(file)
    if crate_root is None:
        print("cargo-check: no Cargo.toml found, skipping", file=sys.stderr)
        emit({"tool": "cargo-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        r = subprocess.run(
            ["cargo", "check", "--message-format=short", "--quiet"],
            capture_output=True, text=True, timeout=120,
            cwd=str(crate_root), encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("cargo-check: cargo not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "cargo-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "cargo-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout (cargo check exceeded 120s)"}],
              "duration_ms": 120000})
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode == 0:
        emit({"tool": "cargo-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return

    output = r.stderr or r.stdout or ""
    errors = _parse_errors(output, file)
    if not errors:
        # cargo exited non-zero without emitting a single short-format
        # `file:line:col: error[...]` diagnostic, so nothing here is a compile
        # finding about any source file — let alone this one. The shapes that
        # land here are cargo failing before or after compilation (#753):
        #
        #   error: unclosed table, expected `]`      (unparseable Cargo.toml)
        #    --> Cargo.toml:1:9
        #   error: could not compile `demo` ... due to 1 previous error
        #                                            (the summary, alone)
        #
        # The first is a manifest error published as a Rust compile error in a
        # .rs file the compiler never reached.
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": tool_fault("cargo check", r.returncode, output)}]

    emit({"tool": "cargo-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
