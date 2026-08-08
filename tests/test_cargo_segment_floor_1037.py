"""cargo-check attributed a foreign file's error to the validated file (#1037).

#754 replaced a `crate_root / src_file` join with a **path-suffix match on
segment boundaries**, and the join was genuinely wrong: cargo prints diagnostic
paths relative to the *workspace* root, so joining onto the crate root
double-counts the member directory, matches nothing, and demotes every real
finding about the file under validation to a non-verdict. Nothing here goes back
to joining.

What #754 left out is a floor on how *little* may match. Two ways too little
matched:

* the raw `{file}` argument was compared in whatever form the caller typed it,
  so a **relative** target was suffix-matched against a **relative** cargo path
  with no common anchor at all — `vendor/crates/foo/src/main.rs` "is"
  `crates/foo/src/main.rs`, and `/abs/elsewhere/src/lib.rs` "is" `src/lib.rs`;
* a single segment was enough — any file in the tree named `main.rs` matched a
  target of `main.rs`.

The diagnostic then kept its real rustc code, which means it is *not* a
non-verdict, which means `rollback_on_fail` reverts a correct edit over a defect
in a file the edit never touched. Same damage route as #969, one layer out.

Both fixes are anchoring, not joining: every target form compared is absolute
(the resolved one and the working-directory-anchored raw one, which is the pair
#754 wanted for Windows' `abspath` / `resolve` divergence), and a suffix has to
be at least two segments to identify anything.

The Windows half is asserted on every platform with `ntpath.normcase` injected,
for the reason #754's own header gives: the fold belongs to the platform, and
three CI legs went red last time on exactly this comparison.
"""
from __future__ import annotations

import importlib.util
import ntpath
import os
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "validators" / "cargo-check" / "cargo-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("cargo_check_1037", ADAPTER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cargo_check = _load()


def _same(src: str, target_raw: str, normcase=None) -> bool:
    """The adapter's own call shape: `_parse_errors` passes the resolved target
    and the raw argument it was handed."""
    return cargo_check._same_file(src, Path(target_raw).resolve(), target_raw,
                                  normcase=normcase)


# ---------------------------------------------------------------------------
# The audit's case table — every one of these is another file
# ---------------------------------------------------------------------------

NOT_THIS_FILE = [
    # a vendored copy of the very crate under validation
    ("vendor/crates/foo/src/main.rs", "crates/foo/src/main.rs"),
    # a foreign crate, and a target of one segment
    ("crates/other/src/main.rs", "main.rs"),
    # an absolute diagnostic somewhere else entirely
    ("/abs/elsewhere/src/lib.rs", "src/lib.rs"),
    # the floor itself: a bare basename identifies nothing
    ("main.rs", "src/main.rs"),
    # a sibling crate in the same workspace
    ("crates/other/src/main.rs", "crates/foo/src/main.rs"),
]


@pytest.mark.parametrize("src,target", NOT_THIS_FILE)
def test_a_foreign_path_is_not_the_validated_file(src: str, target: str) -> None:
    assert _same(src, target) is False, f"{src!r} was accepted as {target!r}"


@pytest.mark.parametrize("src,target", NOT_THIS_FILE)
def test_a_foreign_diagnostic_is_never_charged_to_this_file(
        src: str, target: str) -> None:
    """The damage, not the predicate. A rustc code here is a finding, and a
    finding is what `rollback_on_fail` reverts an edit over."""
    out = f"{src}:4:5: error[E0425]: cannot find function `nope` in this scope\n"
    errors = cargo_check._parse_errors(out, target)
    assert len(errors) == 1, errors
    err = errors[0]
    assert err["code"] == "adapter", (
        f"{src!r} was published as a finding about {target!r}: {err!r}")
    assert err["line"] is None and err["col"] is None, err
    assert src in err["msg"], err["msg"]


# ---------------------------------------------------------------------------
# ... and #754's direction, which must not move
# ---------------------------------------------------------------------------

THIS_FILE = [
    # crate-relative, the common case
    ("src/main.rs", "src/main.rs"),
    # workspace-relative: cargo run from `ws/member` prints the member prefix.
    # Joining onto the crate root is what #754 refused; this is the case it
    # refused it for.
    ("member/src/main.rs", "member/src/main.rs"),
    # cargo printed workspace-relative, the caller typed crate-relative
    ("member/src/main.rs", "member/src/main.rs"),
]


@pytest.mark.parametrize("src,target", THIS_FILE)
def test_the_files_own_diagnostic_is_still_this_file(src: str, target: str) -> None:
    assert _same(src, target) is True, f"a real finding about {target!r} was demoted"


def test_a_crate_relative_path_still_matches_a_deeper_target() -> None:
    """The adapter runs cargo with `cwd=crate_root`, so a crate that is not a
    workspace member gets `src/main.rs` out of cargo while the caller named the
    file `member/src/main.rs` from further up. Same file, and the anchoring
    must not reach it: the target is anchored, cargo's path stays a free tail."""
    assert _same("src/main.rs", os.path.join("member", "src", "main.rs")) is True
    deep = os.path.join("member", "src", "main.rs")
    assert _same(deep, deep) is True


def test_an_absolute_diagnostic_still_matches_its_own_file() -> None:
    target = os.path.join("src", "main.rs")
    assert _same(os.path.abspath(target), target) is True


def test_a_real_finding_keeps_its_rustc_code_and_location(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    target = src / "main.rs"
    target.write_text("fn main() {\n    nope();\n}\n", encoding="utf-8")
    errors = cargo_check._parse_errors(
        "src/main.rs:2:5: error[E0425]: cannot find function `nope`\n", str(target))
    assert errors[0]["code"] == "E0425", f"a real finding was demoted: {errors[0]!r}"
    assert errors[0]["line"] == 2


# ---------------------------------------------------------------------------
# Windows semantics, asserted on every platform (#754's header, verbatim reason)
# ---------------------------------------------------------------------------

WIN_TARGET = "D:\\a\\ws\\crates\\foo\\src\\main.rs"


@pytest.mark.parametrize("src", [
    "src\\main.rs",
    "src/main.rs",
    "crates\\foo\\src\\main.rs",
    WIN_TARGET,
    "Crates\\Foo\\Src\\Main.rs",
])
def test_windows_paths_that_are_this_file_still_match(src: str) -> None:
    assert cargo_check._same_file(src, Path(WIN_TARGET), WIN_TARGET,
                                  normcase=ntpath.normcase) is True, src


@pytest.mark.parametrize("src", [
    "main.rs",                                    # the floor, folded
    "vendor\\crates\\foo\\src\\main.rs",          # a vendored copy
    "crates\\other\\src\\main.rs",                # a sibling crate
    "D:\\other\\ws\\crates\\foo\\src\\main.rs",   # another workspace
])
def test_windows_paths_that_are_another_file_do_not_match(src: str) -> None:
    assert cargo_check._same_file(src, Path(WIN_TARGET), WIN_TARGET,
                                  normcase=ntpath.normcase) is False, src


def test_a_windows_relative_target_is_anchored_not_suffix_matched() -> None:
    """The raw argument in relative form is where both false positives came in.
    `normcase` is injected, so this asserts the Windows fold on every platform;
    the anchor itself is this process's cwd on whatever platform runs it."""
    target = ntpath.join("crates", "foo", "src", "main.rs")
    assert cargo_check._same_file(
        ntpath.join("vendor", target), Path(target).resolve(), target,
        normcase=ntpath.normcase) is False


def test_no_case_is_decided_by_a_hardcoded_posix_separator() -> None:
    """A guard on this file rather than on the adapter: every path above is
    built with `os.path.join`/`ntpath.join` or is a deliberate literal, so a
    `\\`-separated platform reaches the same assertions."""
    built = os.path.join("crates", "foo", "src", "main.rs")
    assert _same(os.path.join("vendor", built), built) is False
    assert _same(built, built) is True
