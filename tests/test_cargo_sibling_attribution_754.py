"""cargo-check must not publish a sibling module's error as this file's (#754).

Split out of #753, which fixed `cargo-check`'s *misclassification* and left its
*misattribution* alone. `cargo check` analyses the whole crate, so its output
carries diagnostics about every file in it; `_parse_errors` matched all of them
and compared none of them to the file under validation. Editing a healthy
`src/main.rs` in a crate whose `src/sibling.rs` does not type-check produced:

    {"file": ".../src/main.rs", "ok": false, "count": 1,
     "errors": [{"line": 1, "col": 29, "code": "E0308", "source_context": []}]}

Three false statements in one object. `file` names main.rs, `line` is a line in
sibling.rs, and `source_context` is empty because `src/sibling.rs` was resolved
against the adapter's working directory rather than the crate.

## The rule, and why it is not a filter

**A crate error caused by another file is real; the claim about which file
caused it is what was wrong.** Dropping the diagnostic would trade a misreport
for a silent loss — the crate genuinely does not build, and a caller told
nothing about that cannot act on it. So the diagnostic stays, `ok` stays false,
and only the attribution changes: `code: "adapter"`, the reserved code for "no
verdict was obtained about this file" (`validators/SCHEMA.md`), `line: null`
because a finding that cannot be placed does not borrow a number, and the real
`path:line:col` in the message where it can be read but not mistaken for this
file's.

That code is not a label of convenience. The core already guarantees an
`adapter` result is never cached — a whole-crate verdict is not a function of
this file's content, and the cache key is a content hash — and (#969) never
triggers rollback, which is the harm the issue names: `rollback_on_fail`
reverting a good edit to main.rs for a defect in sibling.rs, where the revert
cannot possibly fix the error and the next edit is reverted again.

## Why the path comparison is not `crate_root / src_file`

The issue proposes resolving each `src_file` against `crate_root`. Verified
against cargo 1.97.1, that is wrong for any crate in a workspace: run from
`ws/member`, cargo prints `member/src/sib.rs` — relative to the **workspace
root**, not to the crate root it was invoked in. Joining that onto `crate_root`
yields `ws/member/member/src/sib.rs`, which exists nowhere, so a diagnostic
genuinely about the file under validation would fail the comparison and be
demoted to a non-verdict. That is the loud bug traded for the quiet one.

So the comparison is a **path-suffix match on segment boundaries** against the
target's absolute path, which is base-independent: it holds for a crate-root
relative path, a workspace-root relative path and an absolute one, and needs
nothing on disk to exist. And `source_context` is read from the target the
adapter was handed, never from a path reconstructed out of cargo's output —
there is no reconstruction left to get wrong.
"""
from __future__ import annotations

import importlib.util
import json
import ntpath
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _adapter_budget import adapter_budget, inner_budget  # noqa: E402
from _adapter_verdict import describe, skip_if_stalled, verdict  # noqa: E402

VALIDATORS = Path(__file__).parent.parent / "validators"
ADAPTER = VALIDATORS / "cargo-check" / "cargo-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("cargo_check_754", ADAPTER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cargo_check = _load()

# Captured from cargo 1.97.1 on a two-file crate: src/main.rs is valid, and
# src/sibling.rs contains `let _x: i32 = "nope";`.
SIBLING_ERROR = ("src/sibling.rs:1:29: error[E0308]: mismatched types: "
                 "expected `i32`, found `&str`\n")
SUMMARY = 'error: could not compile `demo` (bin "demo") due to 1 previous error\n'
OWN_ERROR = ("src/main.rs:4:5: error[E0425]: cannot find function `nope` in "
             "this scope\n")

# The same crate as a workspace member, checked from `ws/member`. cargo prints
# the path relative to the workspace root, not to the crate root.
WS_OWN_ERROR = ("member/src/main.rs:4:5: error[E0425]: cannot find function "
                "`nope` in this scope\n")
WS_SIBLING_ERROR = ("member/src/sib.rs:1:29: error[E0308]: mismatched types: "
                    "expected `i32`, found `&str`\n")

# A diagnostic raised inside a dependency's source, which is in the crate's
# build but is not any file the caller could edit.
DEP_ERROR = ("/home/u/.cargo/registry/src/index.crates.io-1/foo-0.1.0/src/lib.rs"
             ":9:1: error[E0432]: unresolved import `bar`\n")


def _parse(output: str, target: str, ws_root: str | None = None) -> list[dict]:
    """The adapter's call shape after #1045: cargo's relative paths are relative
    to the workspace root, so the root is passed in rather than guessed at by a
    suffix rule. Every case below whose target is written relative to the
    process cwd has that cwd for its workspace root - which is what the old
    suffix match was silently assuming, and had no way to check."""
    return cargo_check._parse_errors(
        output, target, ws_root=os.getcwd() if ws_root is None else ws_root)


def _only(errors: list[dict]) -> dict:
    assert len(errors) == 1, f"expected one error, got {errors!r}"
    return errors[0]


# ===========================================================================
# Layer 1: the attribution rule, in process, on every platform
# ===========================================================================

def test_a_sibling_diagnostic_is_not_reported_as_this_files_error() -> None:
    """The filed bug. main.rs is one line of `mod sibling;`; the reported
    location was line 1 column 29 of it, taken from a different file."""
    err = _only(_parse(SIBLING_ERROR + SUMMARY, "src/main.rs"))
    assert err["line"] is None, f"a location in another file was attributed here: {err!r}"
    assert err["col"] is None, f"a column in another file was attributed here: {err!r}"
    assert err["code"] == "adapter", f"a crate error was published as this file's finding: {err!r}"


def test_the_sibling_diagnostic_is_still_reported_in_full() -> None:
    """Not a filter. The crate does not build and the caller has to see it,
    with enough in the message to open the file that actually broke."""
    err = _only(_parse(SIBLING_ERROR + SUMMARY, "src/main.rs"))
    for needle in ("src/sibling.rs", "1", "29", "E0308", "mismatched types"):
        assert needle in err["msg"], f"{needle!r} missing from: {err['msg']!r}"
    assert err["severity"] == "error"


def test_an_unattributed_diagnostic_carries_no_source_context() -> None:
    """`source_context: []` read as "this file has no line 1", which is false.
    The key is absent, as it is for every other non-verdict."""
    err = _only(_parse(SIBLING_ERROR + SUMMARY, "src/main.rs"))
    assert "source_context" not in err, f"context was rendered for another file: {err!r}"


def test_the_files_own_diagnostic_is_still_a_finding() -> None:
    err = _only(_parse(OWN_ERROR + SUMMARY, "src/main.rs"))
    assert err["code"] == "E0425", describe(err)
    assert err["line"] == 4 and err["col"] == 5


def test_a_workspace_relative_path_still_matches_the_file() -> None:
    """cargo prints paths relative to the *workspace* root. Resolving against
    the crate root — what the issue proposes — demotes this real finding to a
    non-verdict, which is the same defect pointing the other way."""
    err = _only(_parse(WS_OWN_ERROR, "member/src/main.rs"))
    assert err["code"] == "E0425", f"a real finding was demoted: {err!r}"
    assert err["line"] == 4 and err["col"] == 5


def test_a_workspace_sibling_is_still_not_this_file() -> None:
    err = _only(_parse(WS_SIBLING_ERROR, "member/src/main.rs"))
    assert err["code"] == "adapter", f"a workspace sibling was attributed here: {err!r}"
    assert err["line"] is None


def test_an_absolute_diagnostic_path_matches_the_same_file() -> None:
    target = os.path.abspath(os.path.join("src", "main.rs"))
    out = f"{target}:4:5: error[E0425]: cannot find function `nope` in this scope\n"
    err = _only(_parse(out, os.path.join("src", "main.rs")))
    assert err["code"] == "E0425", f"an absolute path failed to match: {err!r}"
    assert err["line"] == 4


def test_a_dependency_source_is_not_this_file() -> None:
    err = _only(_parse(DEP_ERROR, "src/main.rs"))
    assert err["code"] == "adapter", f"a registry source was attributed here: {err!r}"
    assert ".cargo/registry" in err["msg"]


def test_a_suffix_that_is_not_a_path_segment_does_not_match() -> None:
    """`src/xmain.rs` ends with the characters of `main.rs` and is another
    file. The boundary is a separator, not a substring."""
    out = "src/xmain.rs:4:5: error[E0425]: cannot find function `nope`\n"
    err = _only(_parse(out, "src/main.rs"))
    assert err["code"] == "adapter", f"a substring match attributed the wrong file: {err!r}"


def test_both_are_reported_and_only_the_local_one_is_attributed() -> None:
    """A caller keeps their own error and still learns the crate is broken
    elsewhere. Nothing is collapsed and nothing is dropped."""
    found = _parse(OWN_ERROR + SIBLING_ERROR + SUMMARY, "src/main.rs")
    assert len(found) == 2, f"an error was lost: {found!r}"
    mine, theirs = found
    assert mine["code"] == "E0425" and mine["line"] == 4
    assert theirs["code"] == "adapter" and theirs["line"] is None


def test_source_context_is_read_from_the_target_not_from_the_cwd(tmp_path: Path) -> None:
    """`source_context(src_file, ln)` resolved a crate-relative path against
    wherever the adapter happened to be running, so context came back empty for
    a file that was right there."""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "main.rs"
    target.write_text("fn main() {\n    let a = 1;\n    let b = 2;\n    nope();\n}\n",
                      encoding="utf-8")
    err = _only(_parse(
        "src/main.rs:4:5: error[E0425]: cannot find function `nope` in this scope\n",
        str(target), ws_root=str(tmp_path)))
    assert err["source_context"], "no context was rendered for a readable target"
    assert any("nope();" in line and "→" in line for line in err["source_context"]), \
        f"context is not centred on the error line: {err['source_context']!r}"


# --- the same rule under Windows path semantics, on every platform ---------
#
# `os.path.normcase` is the only stdlib call that knows whether a platform folds
# case, and on Windows it also rewrites a forward slash into a backslash. The
# first version of this fix normalised the separator and then called `normcase`,
# which un-normalised it, while the suffix rule still tested for a `/` boundary
# - so on Windows no diagnostic ever matched its own file and every finding was
# demoted to a non-verdict. Three CI legs went red and nothing here could have
# caught it, because the fold belongs to the platform.
#
# So the fold is injectable, the way `refusal.daemon_transport_reason` takes
# `has_uds`: the contract is asserted on every platform rather than only on the
# runners that happen to have the behaviour. `ntpath.normcase` is the real
# Windows implementation, imported and called directly.

WIN_TARGET = "D:\\a\\claude-supertool\\claude-supertool\\src\\main.rs"
WIN_WS = "D:\\a\\claude-supertool\\claude-supertool"


@pytest.mark.parametrize("src", [
    "src\\main.rs",                    # crate-relative, as Windows cargo prints it
    "src/main.rs",                     # forward slashes are valid on Windows too
    WIN_TARGET,                        # absolute, as cargo prints it for some targets
    "Src\\Main.rs",                    # the fold is a fold: Windows ignores case
])
def test_a_windows_path_matches_its_own_file_on_every_platform(src: str) -> None:
    """The regression that reached CI. Each of these IS the file under
    validation and has to stay a finding."""
    assert cargo_check._same_file(src, Path(WIN_TARGET), WIN_TARGET,
                                  normcase=ntpath.normcase,
                                  ws_root=WIN_WS) is True, src


@pytest.mark.parametrize("src", [
    "src\\sibling.rs",
    "src\\xmain.rs",                   # a character-suffix that is not a segment
    "D:\\other\\src\\main.rs",
])
def test_a_windows_sibling_is_still_not_this_file(src: str) -> None:
    assert cargo_check._same_file(src, Path(WIN_TARGET), WIN_TARGET,
                                  normcase=ntpath.normcase,
                                  ws_root=WIN_WS) is False, src


def test_windows_semantics_end_to_end_keep_the_files_own_finding(monkeypatch) -> None:
    """`test_validators_tier2.py::test_cargo_check_source_context_on_error` is
    a pre-existing test and it failed as `assert None is not None` - the file's
    own error arrived carrying no line at all. This drives the whole parse."""
    monkeypatch.setattr(cargo_check.os.path, "normcase", ntpath.normcase)
    err = _only(_parse(
        "src\\main.rs:5:5: error[E0425]: cannot find function `nope` in this scope\n",
        WIN_TARGET, ws_root=WIN_WS))
    assert err["code"] == "E0425", f"the file's own error was demoted: {err!r}"
    assert err["line"] == 5 and err["col"] == 5
    assert "source_context" in err, f"a finding lost its context key: {err!r}"


def test_windows_semantics_end_to_end_still_disown_a_sibling(monkeypatch) -> None:
    monkeypatch.setattr(cargo_check.os.path, "normcase", ntpath.normcase)
    err = _only(_parse(
        "src\\sibling.rs:1:29: error[E0308]: mismatched types\n", WIN_TARGET,
        ws_root=WIN_WS))
    assert err["code"] == "adapter", f"a sibling was attributed here: {err!r}"
    assert err["line"] is None


def test_warnings_are_still_ignored_wherever_they_come_from() -> None:
    assert _parse(
        "src/sibling.rs:1:9: warning[unused]: unused variable `x`\n"
        "src/main.rs:2:9: warning[unused]: unused variable `y`\n",
        "src/main.rs") == []


def test_output_with_no_located_diagnostic_is_still_not_a_verdict() -> None:
    """#753's branch, unchanged: nothing here may start matching."""
    for out in ("", SUMMARY,
                "error: unclosed table, expected `]`\n --> Cargo.toml:1:9\n"):
        assert _parse(out, "src/main.rs") == [], out


# ===========================================================================
# Layer 2: a real two-file crate, because the attribution depends on cargo's
# own relative paths and a stub would have to guess at them
# ===========================================================================

needs_cargo = pytest.mark.skipif(
    shutil.which("cargo") is None,
    reason="cargo is not installed; the rule it exercises is pinned in process above")


def _crate(root: Path, *, sibling_broken: bool, main_broken: bool) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8")
    (root / "src" / "sibling.rs").write_text(
        'pub fn go() { let _x: i32 = "nope"; }\n' if sibling_broken
        else "pub fn go() {}\n", encoding="utf-8")
    (root / "src" / "main.rs").write_text(
        "mod sibling;\n\nfn main() {\n    sibling::go();\n"
        + ("    nope();\n" if main_broken else "") + "}\n", encoding="utf-8")
    return root / "src" / "main.rs"


def _run(target: Path) -> dict:
    """The spawn, with the adapter's own 120s wall as a decline (#1604).

    On a loaded `windows-latest` runner `cargo check` blows that wall, the
    adapter correctly reports a timeout, and every assertion below then reads
    that timeout string as a verdict about `sibling.rs`:

        assert 'sibling.rs' in 'timeout (cargo check exceeded 120s)'

    Nothing else moves. `skip_if_stalled` hands back any payload that IS a
    verdict about the file, so a real cargo finding still fails here; only a
    payload that spent the adapter's whole internal budget without reaching one
    declines, carrying the rendered verdict into the skip reason. A blown
    *outer* budget still raises `TimeoutExpired` and still fails, because that
    one means the adapter ignored its own timeout (see `_adapter_budget`).
    """
    r = subprocess.run([sys.executable, str(ADAPTER), str(target)],
                       capture_output=True, text=True,
                       timeout=adapter_budget(ADAPTER),
                       encoding="utf-8", errors="replace")
    return skip_if_stalled(verdict(r, adapter="cargo-check"),
                           inner_s=inner_budget(ADAPTER))


@needs_cargo
def test_real_crate_a_sibling_error_is_not_charged_to_this_file(tmp_path: Path) -> None:
    target = _crate(tmp_path, sibling_broken=True, main_broken=False)
    data = _run(target)
    assert data["ok"] is False, describe(data)
    assert data["count"] == 1, describe(data)
    err = data["errors"][0]
    assert err["line"] is None, describe(data)
    assert err["code"] == "adapter", describe(data)
    assert "sibling.rs" in err["msg"], describe(data)
    assert "E0308" in err["msg"], describe(data)
    assert "source_context" not in err, describe(data)


@needs_cargo
def test_real_crate_this_files_own_error_is_still_a_finding(tmp_path: Path) -> None:
    target = _crate(tmp_path, sibling_broken=False, main_broken=True)
    data = _run(target)
    assert data["ok"] is False, describe(data)
    err = data["errors"][0]
    assert err["code"] == "E0425", describe(data)
    assert err["line"] == 5, describe(data)
    assert err["source_context"], describe(data)


@needs_cargo
def test_real_crate_a_healthy_crate_is_still_clean(tmp_path: Path) -> None:
    target = _crate(tmp_path, sibling_broken=False, main_broken=False)
    data = _run(target)
    assert data["ok"] is True, describe(data)
    assert data["count"] == 0, describe(data)


@needs_cargo
def test_real_crate_the_verdict_json_is_the_only_thing_on_stdout(tmp_path: Path) -> None:
    target = _crate(tmp_path, sibling_broken=True, main_broken=False)
    r = subprocess.run([sys.executable, str(ADAPTER), str(target)],
                       capture_output=True, text=True,
                       timeout=adapter_budget(ADAPTER),
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().count("\n") == 0, r.stdout
    json.loads(r.stdout.strip())
