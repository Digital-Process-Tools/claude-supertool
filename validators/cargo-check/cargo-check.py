#!/usr/bin/env python3
"""cargo-check validator adapter — Rust compilation check via `cargo check`.

NOTE: This validator compiles the entire crate — it can be slow (5-30s on first
run). Consider disabling it for hot-loop editing via `opt_in: true` in your
.supertool.json validator config.

Requires cargo (ships with Rust). Absent, this reports the third state —
`skipped` with the reason — rather than the `ok: true` it emitted until #1202,
which was a clean verdict about a file nothing compiled. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

Finds Cargo.toml by walking up from the .rs file. A file in no crate is also a
`skipped`, but never escalates: cargo is installed and working, and the reason
the gate did not run is the file, not the CI image.

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
from source_context import context_fields
from refusal import absent, guard_main, skipped, tool_fault
from linebreaks import split_lines

TOOL = "cargo-check"
INSTALL_HINT = ("cargo not found on PATH — this file was NOT compiled "
                "(install the Rust toolchain via rustup)")


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


def _workspace_root(crate_root: Path,
                    run: Callable[..., object] | None = None) -> tuple[str | None, str]:
    """The base cargo's relative diagnostic paths are relative to (#1045).

    Asked of cargo rather than guessed, because cargo is the only thing that
    knows: the root of a workspace is not the nearest `Cargo.toml` above the
    file, and `[workspace] members` may name a directory anywhere under it.
    `cargo metadata --no-deps` answers without resolving the dependency graph,
    so it neither touches the network nor pays for a build.

    Returns `(root, "")` or `(None, reason)` - exactly one of the two, so a
    caller that cannot get an answer is handed the words to say why. Every way
    the call can fail to produce a root is a reason, including the ones that are
    not this platform's: `FileNotFoundError [WinError 2]` on a Windows runner
    where the PATH lookup failed is the shape that escaped `git remote -v` in
    #997 and took out the whole validator instead of reaching its own "the tool
    could not answer" arm.
    """
    runner = run or subprocess.run
    try:
        r = runner(["cargo", "metadata", "--no-deps", "--format-version", "1"],
                   capture_output=True, text=True, timeout=30,
                   cwd=str(crate_root), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None, "cargo metadata timed out after 30s"
    except OSError as exc:
        return None, f"cargo metadata could not be run ({type(exc).__name__})"

    if getattr(r, "returncode", 1) != 0:
        return None, f"cargo metadata exited {r.returncode}"
    try:
        root = json.loads(r.stdout or "")["workspace_root"]
    except (ValueError, TypeError, KeyError):
        return None, "cargo metadata output was unreadable"
    if not isinstance(root, str) or not root.strip():
        return None, "cargo metadata output was unreadable"
    return root, ""


def _attribute(src_file: str, target: Path, target_raw: str = "",
               ws_root: str | None = None,
               normcase: Callable[[str], str] | None = None) -> str:
    """Which file did cargo name - this one, another one, or unanswerable?

    `"this"` / `"other"` / `"unknown"`, and the third one is the point (#1045).

    **The comparison is equality between two absolute paths.** It used to be a
    suffix match, because the adapter had one absolute path and one relative one
    and no base to close the gap with. A suffix match cannot tell a short path
    that is a tail of the target from a short path that is a different file
    higher up the tree, so #1037 put a floor of two segments under it - and a
    package at the *workspace root* prints exactly two: `src/lib.rs`, which
    every member's absolute path also ends with. The root package's pre-existing
    error was charged to the member under validation, with a real rustc code, on
    a `rollback_on_fail` validator. No floor fixes that; the two strings are
    identical and what separates them is the workspace layout.

    So the base is fetched instead of guessed. Measured against cargo 1.97: a
    relative diagnostic path is relative to the workspace root, and to that root
    whichever directory cargo was invoked from; a file outside the workspace
    root is printed absolute rather than reached with `../`. Anchoring the
    relative case to `ws_root` therefore resolves every diagnostic path to one
    absolute file, and equality decides it with nothing left over.

    This is not the join #754 refused. That one put the *crate* root in front of
    cargo's path, double-counting the member directory and demoting every real
    finding to a non-verdict. The base here is the one cargo actually used.

    **Without `ws_root`, a relative path names no file** - `src/lib.rs` is a
    real path in every package in the tree - and the answer is `"unknown"`
    rather than a pick between them. An absolute path needs no base and is still
    decided.

    Both target spellings are compared for the reason #754 gave: `abspath` joins
    onto the working directory while `resolve()` goes through
    `_getfinalpathname` on Windows and returns the canonical on-disk name,
    following `subst` and symlinked drives. They disagree, and the answer must
    not depend on which one a caller produced.
    """
    src = (src_file or "").strip()
    if not src:
        return "unknown"

    canon = posixpath.normpath(_canon(src, normcase))
    if _is_abs(src):
        src_forms = {canon}
        if os.path.isabs(src):
            try:
                src_forms.add(_canon(Path(src).resolve(), normcase))
            except OSError:
                pass
    else:
        if not (ws_root or "").strip():
            return "unknown"
        base = posixpath.normpath(_canon(ws_root, normcase))
        src_forms = {posixpath.normpath(posixpath.join(base, canon))}

    if src_forms & _target_forms(target, target_raw, normcase):
        return "this"
    return "other"


def _same_file(src_file: str, target: Path, target_raw: str = "",
               normcase: Callable[[str], str] | None = None,
               ws_root: str | None = None) -> bool:
    """Is the path cargo printed the file the adapter was asked about? (#754)

    The predicate, for callers that need only the finding / not-a-finding half.
    `_attribute` is the whole answer and `_parse_errors` uses that one: "not
    this file" and "no way to tell which file" are different sentences, and only
    one of them is entitled to name another file.
    """
    return _attribute(src_file, target, target_raw, ws_root, normcase) == "this"


def _target_forms(target: Path | str, target_raw: str = "",
                  normcase: Callable[[str], str] | None = None) -> set:
    """Every spelling of the file under validation - all of them absolute (#1037).

    The comparison has one side that arrives relative, and it is cargo's, whose
    base is the workspace root and is anchored to it by `_attribute` (#1045).
    The *target* side has no such excuse: the adapter knows the working
    directory it was invoked in, so a relative argument is anchored to that
    rather than compared as a floating tail.

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


def _unplaceable(src_file: str, ln: int, col: int, code: str, msg: str,
                 reason: str) -> str:
    """The message for a diagnostic whose file could not be identified (#1045).

    Distinct from `_elsewhere_in_crate`, which asserts the diagnostic belongs to
    *another* file - a claim this arm cannot make. `src/lib.rs` is a real path
    relative to the workspace root and a real path relative to every package in
    it; without the root, saying "not this file" would be the same guess as
    saying "this file", just quieter. What is known is printed: the path cargo
    gave, its location in that path, and why the root could not be read.
    """
    label = f"error[{code}]" if code and code != "compile" else "error"
    return (f"cargo check reported {label} at {src_file}:{ln}:{col}, a path "
            f"relative to the workspace root, and the workspace root could not "
            f"be read ({reason}) - so which file it names is unknown and no "
            f"verdict was produced about this file: {msg}")


def _parse_errors(output: str, target_file: str, ws_root: str | None = None,
                  ws_reason: str = "") -> list[dict]:
    """Extract error lines from cargo --message-format=short output.

    `cargo check` analyses the whole crate, so its output carries diagnostics
    about every file in it. Those that name `target_file` are findings and keep
    their line, column and rustc code. Those that name another file keep their
    text and lose their location: `code: "adapter"`, the code reserved across
    every adapter for "no verdict was obtained about this file", with `line` and
    `col` null because a finding that cannot be placed here does not borrow a
    number, and no `source_context` because there is no line of this file to
    render (#754).

    Those whose file cannot be identified at all - a relative path with no
    workspace root to anchor it to - are non-verdicts too, but say a different
    thing: `_unplaceable` reports that the path could not be placed, where
    `_elsewhere_in_crate` reports that it was placed elsewhere. Collapsing the
    two would publish a guess about another file in the words of a fact (#1045).
    """
    errors = []
    target = Path(target_file).resolve()
    # Short format: "path/to/file.rs:LINE:COL: error[EXXXX]: message"
    pattern = re.compile(r"^(.+?):(\d+):(\d+):\s+(error|warning)\[?([^\]]*)\]?:\s+(.+)$")
    for line in split_lines(output):
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
            verdict = _attribute(src_file, target, target_file, ws_root)
            if verdict == "this":
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
                    **context_fields(str(target), ln),
                })
            else:
                where = (_elsewhere_in_crate(src_file, ln, col, code, msg)
                         if verdict == "other"
                         else _unplaceable(src_file, ln, col, code, msg,
                                           ws_reason or "reason not recorded"))
                errors.append({
                    "line": None,
                    "col": None,
                    "severity": "error",
                    "code": "adapter",
                    "msg": where,
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
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    crate_root = _find_crate_root(file)
    if crate_root is None:
        # A scope refusal, not an absent tool: `skipped()` directly rather than
        # `absent()`, because no CI image change makes a file in no crate
        # compilable and escalating it would fire on every loose `.rs`.
        emit(skipped(TOOL, file,
                     "no Cargo.toml above this file — it belongs to no crate, "
                     "so it was NOT compiled",
                     int((time.time() - start) * 1000)))
        return

    try:
        r = subprocess.run(
            ["cargo", "check", "--message-format=short", "--quiet"],
            capture_output=True, text=True, timeout=120,
            cwd=str(crate_root), encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "cargo on PATH but could not be executed — "
                                "this file was NOT compiled",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "cargo-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout (cargo check exceeded 120s)"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode == 0:
        emit({"tool": "cargo-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return

    output = r.stderr or r.stdout or ""
    # Only on the failing path: the base is needed to attribute diagnostics, and
    # a run with no diagnostics has nothing to attribute. A clean check pays
    # nothing for it.
    ws_root, ws_reason = _workspace_root(crate_root)
    errors = _parse_errors(output, file, ws_root=ws_root, ws_reason=ws_reason)
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
    guard_main(TOOL, main)
