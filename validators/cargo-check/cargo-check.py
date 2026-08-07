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


def _tail_match(a: str, b: str) -> bool:
    """Do two canonical paths name the same file, one possibly a tail of the
    other? Symmetric, because either side may be the relative one: cargo prints
    a relative path for a workspace member and an absolute one elsewhere, and
    the adapter is handed whatever the caller typed.

    The boundary is a separator, never a substring: `src/xmain.rs` ends with the
    characters of `main.rs` and is a different file.
    """
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


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

    tgt_forms = {_canon(target, normcase)}
    if target_raw:
        tgt_forms.add(posixpath.normpath(_canon(target_raw, normcase)))

    return any(_tail_match(s, t) for s in src_forms for t in tgt_forms)


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
