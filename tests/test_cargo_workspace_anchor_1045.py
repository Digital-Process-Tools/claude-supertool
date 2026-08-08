"""cargo-check guessed which file a diagnostic named; now it anchors (#1045).

#754 replaced a `crate_root / src_file` join with a path-suffix match, and #1037
put a two-segment floor under that match. The floor is still a guess. A package
at the **workspace root** is printed by cargo as `src/lib.rs` or `src/main.rs`
- two segments, a real package path rather than a bare basename - and every
workspace member's absolute path ends with those same two segments. The root
package's pre-existing error was charged to the member file under validation,
keeping its rustc code, so `rollback_on_fail` reverted a correct edit.

Raising the floor a third time cannot work: no number separates `src/lib.rs`
(the root package) from `src/lib.rs` (a member's own file, printed identically
when the member *is* the workspace root of its own single-crate tree). The two
strings are equal; the fact that distinguishes them is the workspace layout.

Cargo carries that fact and the adapter was not asking for it. Measured against
cargo 1.97.1:

* a relative diagnostic path is relative to the **workspace root**, and to that
  root regardless of the directory cargo was invoked from;
* a file outside the workspace root is printed **absolute**, never with `../`;
* `cargo metadata --no-deps` reports `workspace_root` as an absolute path.

So every diagnostic path resolves to exactly one absolute file, and the
comparison becomes equality. No suffix, no floor, no tie to break.

The third state is where the guess used to be: if the workspace root cannot be
read, a *relative* path names no particular file and the adapter says so rather
than picking one. An absolute path still resolves without it.
"""
from __future__ import annotations

import importlib.util
import ntpath
import os
import posixpath
import subprocess
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "validators" / "cargo-check" / "cargo-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("cargo_check_1045", ADAPTER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cargo_check = _load()


def _abs(*parts: str) -> str:
    """An absolute path built for whatever platform runs this, never a literal."""
    return os.path.abspath(os.path.join(*parts))


# ---------------------------------------------------------------------------
# The reported defect: a workspace-root package is not the member being checked
# ---------------------------------------------------------------------------

WS = _abs(os.sep + "repo")
MEMBER = os.path.join(WS, "crates", "other", "src", "lib.rs")


@pytest.mark.parametrize("root_file", ["src/lib.rs", "src/main.rs"])
def test_the_workspace_root_package_is_not_the_member_under_validation(
        root_file: str) -> None:
    """Two segments, a real package path, and a different file. This is the
    whole of #1045: the member's absolute path ends with `/src/lib.rs` too."""
    target = os.path.join(WS, "crates", "other", "src",
                          os.path.basename(root_file))
    assert cargo_check._same_file(root_file, Path(target), target,
                                  ws_root=WS) is False, root_file


def test_a_workspace_root_diagnostic_is_never_charged_to_a_member(
        tmp_path: Path) -> None:
    """The damage, not the predicate: a rustc code here is a finding, and a
    finding is what `rollback_on_fail` reverts a correct edit over."""
    member = tmp_path / "crates" / "other" / "src"
    member.mkdir(parents=True)
    target = member / "lib.rs"
    target.write_text("pub fn fine() -> i32 { 1 }\n", encoding="utf-8")

    errors = cargo_check._parse_errors(
        "src/lib.rs:1:30: error[E0308]: mismatched types\n",
        str(target), ws_root=str(tmp_path))

    assert len(errors) == 1, errors
    err = errors[0]
    assert err["code"] == "adapter", (
        f"the workspace root package's error was published as a finding "
        f"about the member: {err!r}")
    assert err["line"] is None and err["col"] is None, err
    assert "src/lib.rs" in err["msg"], err["msg"]
    assert "source_context" not in err, err


def test_the_members_own_diagnostic_is_still_its_own(tmp_path: Path) -> None:
    """#754's direction, which must not move: the real finding keeps its code,
    its location and its context."""
    member = tmp_path / "crates" / "other" / "src"
    member.mkdir(parents=True)
    target = member / "lib.rs"
    target.write_text("pub fn fine() -> i32 { 1 }\n", encoding="utf-8")

    rel = posixpath.join("crates", "other", "src", "lib.rs")
    errors = cargo_check._parse_errors(
        f"{rel}:1:24: error[E0308]: mismatched types\n",
        str(target), ws_root=str(tmp_path))

    assert errors[0]["code"] == "E0308", f"a real finding was demoted: {errors[0]!r}"
    assert errors[0]["line"] == 1 and errors[0]["col"] == 24
    assert errors[0]["source_context"]


# ---------------------------------------------------------------------------
# Anchoring is exact, so the two-segment floor's accepted cost is repaid
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["build.rs", "main.rs"])
def test_a_one_segment_path_at_the_workspace_root_is_now_decidable(
        name: str) -> None:
    """The floor demoted every 1-segment path, so `build.rs` at a crate root
    was always a non-verdict. Anchored, it is simply a file: this one when it
    is this one, another when it is not."""
    at_root = os.path.join(WS, name)
    assert cargo_check._same_file(name, Path(at_root), at_root,
                                  ws_root=WS) is True
    elsewhere = os.path.join(WS, "crates", "other", name)
    assert cargo_check._same_file(name, Path(elsewhere), elsewhere,
                                  ws_root=WS) is False


def test_an_absolute_diagnostic_needs_no_workspace_root() -> None:
    """Cargo prints an absolute path for anything outside the workspace root,
    and an absolute path already names one file."""
    target = os.path.join(WS, "src", "lib.rs")
    foreign = _abs(os.sep + "elsewhere", "src", "lib.rs")
    assert cargo_check._same_file(target, Path(target), target) is True
    assert cargo_check._same_file(foreign, Path(target), target) is False


# ---------------------------------------------------------------------------
# The third state: no workspace root means a relative path names nothing
# ---------------------------------------------------------------------------

def test_an_unreadable_workspace_root_declines_rather_than_guessing() -> None:
    assert cargo_check._attribute("src/lib.rs", Path(MEMBER), MEMBER,
                                  ws_root=None) == "unknown"


def test_the_declined_attribution_says_so_and_is_not_a_finding(
        tmp_path: Path) -> None:
    target = tmp_path / "lib.rs"
    target.write_text("pub fn f() {}\n", encoding="utf-8")
    errors = cargo_check._parse_errors(
        "src/lib.rs:1:1: error[E0308]: mismatched types\n", str(target),
        ws_root=None, ws_reason="cargo metadata exited 101")

    assert errors[0]["code"] == "adapter", errors[0]
    assert errors[0]["line"] is None, errors[0]
    msg = errors[0]["msg"]
    assert "cargo metadata exited 101" in msg, msg
    assert "src/lib.rs" in msg and "mismatched types" in msg, msg
    assert "not this file" not in msg, (
        "a path that could not be placed was reported as another file: " + msg)


# ---------------------------------------------------------------------------
# Reading the workspace root — every way cargo can fail to answer
# ---------------------------------------------------------------------------

def _run_ok(payload: str):
    def run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, stdout=payload, stderr="")
    return run


def test_the_workspace_root_is_read_from_cargo_metadata() -> None:
    root, reason = cargo_check._workspace_root(
        Path(WS), run=_run_ok('{"workspace_root": %s}' % _json(WS)))
    assert root == WS and reason == ""


def _json(text: str) -> str:
    import json
    return json.dumps(text)


@pytest.mark.parametrize("run,expect", [
    (lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError(2, "not found")),
     "could not be run"),
    (lambda *a, **k: (_ for _ in ()).throw(OSError(13, "denied")),
     "could not be run"),
    (lambda *a, **k: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("cargo", 30)), "timed out"),
    (lambda *a, **k: subprocess.CompletedProcess(a[0], 101, stdout="",
                                                 stderr="boom"), "exited 101"),
    (_run_ok("not json at all"), "unreadable"),
    (_run_ok('{"packages": []}'), "unreadable"),
])
def test_every_metadata_failure_is_a_reason_not_a_crash(run, expect: str) -> None:
    """A spawn failure is the shape that escaped in #997: Windows raises
    `FileNotFoundError [WinError 2]` where POSIX may not fail at all, and an
    uncaught one here would take out the whole validator rather than reach the
    'the tool could not answer' arm."""
    root, reason = cargo_check._workspace_root(Path(WS), run=run)
    assert root is None
    assert expect in reason, reason


# ---------------------------------------------------------------------------
# Windows semantics, asserted on every platform (#754's header, verbatim reason)
# ---------------------------------------------------------------------------

WIN_WS = "D:\\a\\ws"
WIN_MEMBER = ntpath.join(WIN_WS, "crates", "foo", "src", "lib.rs")


@pytest.mark.parametrize("src", [
    "src\\lib.rs",                    # the workspace-root package, #1045
    "src/lib.rs",                     # ... as cargo may also spell it
    "crates\\other\\src\\lib.rs",     # a sibling member
    "lib.rs",                         # a file at the workspace root
    "D:\\other\\ws\\crates\\foo\\src\\lib.rs",
])
def test_windows_paths_that_are_another_file_do_not_match(src: str) -> None:
    assert cargo_check._same_file(src, Path(WIN_MEMBER), WIN_MEMBER,
                                  ws_root=WIN_WS,
                                  normcase=ntpath.normcase) is False, src


@pytest.mark.parametrize("src", [
    "crates\\foo\\src\\lib.rs",
    "crates/foo/src/lib.rs",
    "Crates\\Foo\\Src\\Lib.rs",
    WIN_MEMBER,
])
def test_windows_paths_that_are_this_file_still_match(src: str) -> None:
    assert cargo_check._same_file(src, Path(WIN_MEMBER), WIN_MEMBER,
                                  ws_root=WIN_WS,
                                  normcase=ntpath.normcase) is True, src


def test_a_windows_workspace_root_is_joined_with_its_own_separator() -> None:
    """The anchor is built from two folded strings, so neither side may carry a
    hardcoded separator into the join."""
    src = "src\\lib.rs"
    at_root = ntpath.join(WIN_WS, "src", "lib.rs")
    assert cargo_check._same_file(src, Path(at_root), at_root, ws_root=WIN_WS,
                                  normcase=ntpath.normcase) is True


def test_no_case_here_is_decided_by_a_hardcoded_posix_separator() -> None:
    """A guard on this file: every POSIX-shaped path above is built with
    `os.path.join`/`os.sep`, so a `\\`-separated platform reaches the same
    assertions."""
    built = os.path.join("crates", "foo", "src", "lib.rs")
    target = os.path.join(WS, built)
    assert cargo_check._same_file(built, Path(target), target,
                                  ws_root=WS) is True
    assert cargo_check._same_file(os.path.join("vendor", built), Path(target),
                                  target, ws_root=WS) is False
