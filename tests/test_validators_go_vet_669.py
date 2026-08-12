"""The go-vet validator adapter (#669).

Go was the one mainstream language in #669's inventory with **no semantic
coverage at all**: `gofmt-check` answers a formatting question and nothing
else, so `fmt.Printf("%d", "x")` shipped clean. `go vet` closes that and costs
no new install — it ships with the toolchain, exactly like `gofmt`.

Two things this file pins that a "does it lint" test would not:

* **`go vet` is package-scoped**, like `cargo check`. Handed one `.go` file it
  reports diagnostics belonging to that file's *siblings*, so "the output has a
  located diagnostic" and "the output says something about the file you edited"
  are two different facts — `validators/SCHEMA.md` §"A located diagnostic still
  has to be about *this* file (#754)".
* **A file in no module is a `skipped`, never an `ok`.** `go vet` outside a
  module refuses with `go.mod file not found` and exits 1; reading that as a
  finding blames the file, and reading it as a pass would be another instance
  of this repo's most-filed defect.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget
from _adapter_verdict import assert_declined, assert_ok, describe, verdict
from _winenv import empty_path_env

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "go-vet" / "go-vet.py"
COMMON = REPO / "validators" / "common"

needs_go = pytest.mark.skipif(not shutil.which("go"), reason="go not on PATH")

GO_MOD = """module example.com/probe

go 1.21
"""

#: A `fmt.Printf` verb that does not match its argument. vet's printf analyzer
#: has caught this since Go 1.10; it is the cheapest reproducer that is a
#: *semantic* fault rather than a syntactic one, which is the whole gap.
BAD_PRINTF = """package pkg

import "fmt"

func Bad() {
    fmt.Printf("%d", "not a number")
}
"""
#: 1-indexed line of the offending call in BAD_PRINTF.
BAD_LINE = 6

#: The same fault under a different symbol. Two files declaring `Bad` in one
#: package is a *redeclaration* error, which is a fact about the package rather
#: than two printf findings — and it would let the sibling test pass for the
#: wrong reason.
BAD_PRINTF_B = BAD_PRINTF.replace("func Bad(", "func AlsoBad(")

CLEAN = """package pkg

func Fine() int {
    return 1
}
"""

NO_TYPE_CHECK = """package pkg

func X() { definitelyNotDefined() }
"""


def _spawn(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ADAPTER), *args],
        capture_output=True, text=True, env=env,
        timeout=adapter_budget(ADAPTER), encoding="utf-8", errors="replace",
    )


def _run(path: Path, env: dict | None = None) -> dict:
    return verdict(_spawn(str(path), env=env), adapter=ADAPTER.name)


def _module(tmp_path: Path, files: dict) -> Path:
    """A minimal Go module. Keys are module-relative paths, values bodies."""
    (tmp_path / "go.mod").write_text(GO_MOD, encoding="utf-8")
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# The third state
# ---------------------------------------------------------------------------

def test_missing_go_is_the_third_state(tmp_path: Path) -> None:
    """No Go toolchain -> `skipped`, with `ok`/`count`/`errors` omitted.

    A receipt carrying `ok: true` here reads as a pass to the delta
    arithmetic, the rollback decision and the cache alike.
    """
    root = _module(tmp_path, {"pkg/a.go": BAD_PRINTF})
    out = _run(root / "pkg" / "a.go", env=empty_path_env())
    assert "skipped" in out, describe(out)
    assert "go" in out["skipped"]
    for key in ("ok", "count", "errors"):
        assert key not in out, f"a skip must not carry {key!r}: {out}"
    assert out["tool"] == "go-vet"
    assert isinstance(out["duration_ms"], int)


def test_required_turns_the_absent_toolchain_into_a_loud_error(tmp_path: Path) -> None:
    root = _module(tmp_path, {"pkg/a.go": CLEAN})
    env = empty_path_env()
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = "go-vet"
    out = _run(root / "pkg" / "a.go", env=env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="a required validator whose toolchain is absent")
    assert out["errors"][0]["code"] == "adapter", describe(out)
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in out["errors"][0]["msg"]


@needs_go
def test_a_file_in_no_module_is_skipped_not_clean(tmp_path: Path) -> None:
    """`go vet` outside a module says `go.mod file not found` and exits 1.

    Neither state that has: it is not a finding about the file, and it is
    certainly not a pass. The toolchain is installed and working — the reason
    the gate did not run is the layout, so this never escalates under
    `$SUPERTOOL_REQUIRE_VALIDATORS` either.
    """
    loose = tmp_path / "loose.go"
    loose.write_text(BAD_PRINTF, encoding="utf-8")
    out = _run(loose)
    assert "skipped" in out, describe(out)
    assert "go.mod" in out["skipped"], out["skipped"]
    for key in ("ok", "count", "errors"):
        assert key not in out, f"a skip must not carry {key!r}: {out}"


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

@needs_go
def test_a_clean_package_is_clean(tmp_path: Path) -> None:
    root = _module(tmp_path, {"pkg/a.go": CLEAN})
    out = _run(root / "pkg" / "a.go")
    assert_ok(out)
    assert out["count"] == 0
    assert out["errors"] == []


@needs_go
def test_the_semantic_fault_gofmt_cannot_see(tmp_path: Path) -> None:
    """The gap #669 recorded, stated as a test.

    `gofmt` reformats this file without complaint. `go vet` does not.
    """
    root = _module(tmp_path, {"pkg/a.go": BAD_PRINTF})
    out = _run(root / "pkg" / "a.go")
    assert_declined(out, context="a printf verb that does not match its arg")
    err = out["errors"][0]
    assert err["line"] == BAD_LINE, describe(out)
    assert isinstance(err["col"], int), describe(out)
    assert "wrong type" in err["msg"], describe(out)
    assert err["source_context"], describe(out)
    assert err["code"] != "adapter", describe(out)


@needs_go
def test_a_siblings_finding_is_published_without_this_files_line(tmp_path: Path) -> None:
    """#754, in Go: `go vet` vets the package, not the file.

    The diagnostic is real and is kept — suppressing it leaves a clean-looking
    file in a package vet rejects. Only the attribution changes: no line, no
    column, `code: "adapter"`, and no `source_context` rendered from a file the
    diagnostic is not about.
    """
    root = _module(tmp_path, {"pkg/a.go": CLEAN, "pkg/b.go": BAD_PRINTF_B})
    out = _run(root / "pkg" / "a.go")
    assert_declined(out, context="a sibling file's vet finding")
    err = out["errors"][0]
    assert err["line"] is None, describe(out)
    assert err["col"] is None, describe(out)
    assert err["code"] == "adapter", describe(out)
    assert "source_context" not in err, describe(out)
    assert "b.go" in err["msg"], describe(out)


@needs_go
def test_the_targets_own_finding_survives_a_noisy_sibling(tmp_path: Path) -> None:
    """Both files bad: the target's diagnostic keeps its line, the sibling's
    does not. One `ok: false` hiding two different claims is what #754 was
    about."""
    root = _module(tmp_path, {"pkg/a.go": BAD_PRINTF, "pkg/b.go": BAD_PRINTF_B})
    out = _run(root / "pkg" / "a.go")
    assert_declined(out, context="two bad files, one under validation")
    located = [e for e in out["errors"] if e["line"] is not None]
    unlocated = [e for e in out["errors"] if e["line"] is None]
    assert len(located) == 1, describe(out)
    assert len(unlocated) == 1, describe(out)
    assert all(e["code"] == "adapter" for e in unlocated), describe(out)


@needs_go
def test_a_package_that_does_not_type_check_is_an_error_not_a_skip(tmp_path: Path) -> None:
    """`vet: a.go:3:12: undefined: ...` — vet could not load the package.

    It is still a fact about the code and it still has a location, so it is a
    finding, not a `skipped`. Folding it into the third state would drop
    `errors` entirely and report a package that does not build as one nobody
    looked at.
    """
    root = _module(tmp_path, {"pkg/a.go": NO_TYPE_CHECK})
    out = _run(root / "pkg" / "a.go")
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="a package that does not type-check")
    assert any("undefined" in e["msg"] for e in out["errors"]), describe(out)
    assert any(e["severity"] == "error" for e in out["errors"]), describe(out)


@needs_go
def test_analyzer_findings_are_not_published_as_errors(tmp_path: Path) -> None:
    """A vet diagnostic is a suspicious construct, not a broken file. It must
    not print at the same severity as a package that will not compile — the
    validator is registered `rollback_on_fail: false` on exactly that basis."""
    root = _module(tmp_path, {"pkg/a.go": BAD_PRINTF})
    out = _run(root / "pkg" / "a.go")
    assert out["errors"][0]["severity"] == "warning", describe(out)


@needs_go
def test_the_edited_files_own_finding_survives_a_crowded_package(tmp_path: Path) -> None:
    """The receipt is capped. The cap must not eat the reason you are reading it.

    A package with more pre-existing vet findings than the cap publishes a
    receipt in which the file you just edited does not appear — `count: 56,
    ok: false` and not one word about your line. That is this repo's own defect
    wearing a cap: the finding is absent from the output and reads as absent
    from the file. Findings about the file under validation are ordered first.
    """
    # `z.go`, not `a.go`: go vet emits diagnostics in file order, so a target
    # that sorts first survives any cap by luck and the assertion below would
    # hold against an adapter that does no ordering at all.
    files = {"pkg/z.go": BAD_PRINTF}
    for i in range(60):
        files[f"pkg/s{i:02d}.go"] = BAD_PRINTF.replace("func Bad(", f"func Sib{i:02d}(")
    root = _module(tmp_path, files)
    out = _run(root / "pkg" / "z.go")
    assert_declined(out, context="a package with more findings than the cap")
    assert out["count"] > len(out["errors"]), describe(out)
    assert out["errors"][0]["line"] == BAD_LINE, describe(out)


@needs_go
def test_a_truncated_receipt_says_it_was_truncated(tmp_path: Path) -> None:
    """Cutting the list silently is an absence produced by the adapter, read as
    an absence in the package. `count` alone does not say it — a reader who
    sees fifty rows has no reason to compare them against a number."""
    files = {"pkg/a.go": BAD_PRINTF}
    for i in range(60):
        files[f"pkg/s{i:02d}.go"] = BAD_PRINTF.replace("func Bad(", f"func Sib{i:02d}(")
    root = _module(tmp_path, files)
    out = _run(root / "pkg" / "a.go")
    tail = out["errors"][-1]
    assert tail["code"] == "adapter", describe(out)
    assert "not shown" in tail["msg"], describe(out)


# ---------------------------------------------------------------------------
# Adapter faults are never findings
# ---------------------------------------------------------------------------

def test_an_unreadable_path_is_an_adapter_error(tmp_path: Path) -> None:
    root = _module(tmp_path, {"pkg/a.go": CLEAN})
    out = _run(root / "pkg" / "nope.go")
    if "skipped" in out:
        pytest.skip("go absent on this machine")
    assert_declined(out, context="a path that does not exist")
    assert out["errors"][0]["code"] == "adapter", describe(out)


def test_no_file_arg() -> None:
    out = verdict(_spawn(), adapter=ADAPTER.name)
    assert_declined(out, context="no file argument")
    assert out["errors"][0]["code"] == "adapter"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim script")
def test_a_silent_non_zero_exit_is_never_a_pass(tmp_path: Path) -> None:
    """The #263 shape in Go's clothing: exit non-zero, print nothing.

    An adapter reading empty output as "no findings" turns a broken toolchain
    into a green. It must name the exit code instead.
    """
    root = _module(tmp_path, {"pkg/a.go": CLEAN})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "go"
    shim.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    shim.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir)
    out = _run(root / "pkg" / "a.go", env=env)
    assert out.get("ok") is not True, describe(out)
    assert "3" in json.dumps(out), describe(out)


# ---------------------------------------------------------------------------
# Attribution, asserted from any platform
# ---------------------------------------------------------------------------

def _pkg_paths():
    sys.path.insert(0, str(COMMON))
    import pkg_paths
    return pkg_paths


def test_windows_separators_still_attribute_to_the_file() -> None:
    """The #1005 / #754 class, pinned without a Windows runner.

    `go vet` on Windows separates path segments with a backslash. Folding case
    *after* normalising the separator un-normalises it — `ntpath.normcase`
    rewrites every forward slash into a backslash — so the comparison looks for
    a boundary that is no longer there and every diagnostic, including the
    file's own, is demoted to a non-verdict. That is #1005, and it went red on
    four Windows legs.
    """
    import ntpath
    pp = _pkg_paths()
    assert pp.attribute(r"pkg\a.go", target=r"D:\ws\pkg\a.go", base=r"D:\ws",
                        normcase=ntpath.normcase) == "this"
    assert pp.attribute(r"pkg\b.go", target=r"D:\ws\pkg\a.go", base=r"D:\ws",
                        normcase=ntpath.normcase) == "other"


def test_a_case_differing_spelling_is_the_same_file_on_windows() -> None:
    import ntpath
    pp = _pkg_paths()
    assert pp.attribute(r"PKG\A.GO", target=r"D:\ws\pkg\a.go", base=r"D:\ws",
                        normcase=ntpath.normcase) == "this"


def test_a_relative_path_with_no_base_names_no_file() -> None:
    """Without a base, `a.go` is a real path in every package in the tree.

    The answer is "no way to tell", not a pick between them — and only
    "another file" is entitled to name another file.
    """
    pp = _pkg_paths()
    assert pp.attribute("a.go", target="/ws/pkg/a.go", base="") == "unknown"


def test_a_leading_dot_slash_does_not_make_it_a_different_file() -> None:
    """`go vet .` prints `./a.go`, `go vet ./pkg` prints `pkg/a.go`. Same file."""
    pp = _pkg_paths()
    assert pp.attribute("./a.go", target="/ws/pkg/a.go", base="/ws/pkg") == "this"


def test_an_absolute_reported_path_needs_no_base() -> None:
    pp = _pkg_paths()
    assert pp.attribute("/ws/pkg/a.go", target="/ws/pkg/a.go", base="") == "this"
    assert pp.attribute("/ws/pkg/b.go", target="/ws/pkg/a.go", base="") == "other"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registered_without_rollback() -> None:
    """A vet finding is not a broken file. Reverting an edit over a shadowed
    variable destroys work to fix nothing — contrast `terraform-check`, where
    the file genuinely does not parse."""
    cfg = json.loads((REPO / ".supertool.example.json").read_text(encoding="utf-8"))
    entry = cfg["validators"]["go-vet"]
    assert entry["rollback_on_fail"] is False, entry
    assert entry["match"] == "*.go", entry
    assert "validators/go-vet/go-vet.py" in entry["cmd"]
    assert entry["tier"] == "slow", entry


def test_the_bundled_table_lists_it() -> None:
    """A validator nobody can find out about is not shipped."""
    doc = (REPO / "docs" / "validators.md").read_text(encoding="utf-8")
    assert "`go-vet`" in doc
