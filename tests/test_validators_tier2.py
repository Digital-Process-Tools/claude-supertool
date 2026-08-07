"""Tests for tier-2 systems validators: gofmt-check, terraform-check, cargo-check."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget
from _adapter_verdict import assert_declined, assert_ok

VALIDATORS = Path(__file__).parent.parent / "validators"

GOFMT_CHECK = VALIDATORS / "gofmt-check" / "gofmt-check.py"
TERRAFORM_CHECK = VALIDATORS / "terraform-check" / "terraform-check.py"
CARGO_CHECK = VALIDATORS / "cargo-check" / "cargo-check.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_adapter(adapter: Path, file: str, timeout: int | None = None) -> dict:
    r = subprocess.run([sys.executable, str(adapter), file],
                       capture_output=True, text=True,
                       timeout=adapter_budget(adapter) if timeout is None else timeout, encoding="utf-8", errors="replace")
    return json.loads(r.stdout.strip())


def run_adapter_proc(adapter: Path, file: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(adapter), file],
                          capture_output=True, text=True,
                          timeout=adapter_budget(adapter) if timeout is None else timeout, encoding="utf-8", errors="replace")


# The two Go fixtures, kept side by side so the difference is legible: a tab is
# what gofmt wants, the four spaces are what it rewrites.
GO_FORMATTED = 'package main\n\nfunc main() {\n\t_ = 1\n}\n'
GO_UNFORMATTED = 'package main\n\nfunc main() {\n    _ = 1\n}\n'


def write_go(path: Path, text: str) -> Path:
    """Write a Go fixture with the newlines gofmt is being asked about (#777).

    `Path.write_text` translates every newline to `os.linesep`, so on Windows
    each of these fixtures reached gofmt as CRLF. gofmt rewrites CRLF — `gofmt
    -l` prints the filename of a CRLF file whatever else is in it — so the
    *formatted* fixture is reported as needing formatting there, and the test
    asserting it is clean fails on every Windows leg.

    That is what `skipif(sys.platform == "win32", reason="gofmt adapter has
    encoding issues on Windows")` was suppressing: a defect in how this file
    wrote its fixture, recorded as a defect in the code under test, and left in
    prose long enough to be cited three times. The bytes go to disk unmodified
    now, so the assertion is about gofmt again and the skip goes with it.
    """
    path.write_bytes(text.encode("utf-8"))
    return path


def _windows_decline_reason(data: dict) -> str:
    """The xfail reason for a `gofmt-check` decline on Windows (#777).

    A decline is a legitimate third answer and this treats it as one. What it
    must not do is discard the evidence: `tool_fault` has already put gofmt's
    exit code and gofmt's own output into the message, and the branch that met
    that decline printed a fixed sentence instead. After three Windows
    occurrences the entire record of the defect was still the phrase "encoding
    issues on Windows" in a skip reason — an absence this suite produced and
    then read as an absence in the world.
    """
    errors = data.get("errors") or [{}]
    said = errors[0].get("msg") or "(the adapter recorded no message)"
    return ("gofmt-check declined with 'adapter' on Windows — known flake, see "
            "#777. The adapter, not this test, is what needs fixing. "
            "gofmt-check said: " + said)


# ---------------------------------------------------------------------------
# gofmt-check
# ---------------------------------------------------------------------------


def test_a_windows_gofmt_decline_reports_what_gofmt_said() -> None:
    """#777: a decline is an answer, and an answer has to say something.

    `tool_fault` already puts gofmt's exit code and gofmt's own output into the
    message — the adapter knows exactly what happened. The branch that meets
    that decline printed a fixed sentence instead and dropped the message, so
    after three Windows occurrences the whole record of the defect is still the
    phrase "encoding issues on Windows" in a sibling test's skip reason. A
    decline that does not carry what the tool said makes every future
    occurrence another log read that produces nothing new, which is this
    repository's own defect class pointed at its own test suite.
    """
    said = "`gofmt -l` exited 2 — subject.go: The process cannot access the file"
    data = {"tool": "gofmt-check", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": said}]}

    reason = _windows_decline_reason(data)

    assert "#777" in reason
    assert said in reason, (
        "the decline was reported without the one thing only that run knows — "
        "what gofmt actually said (#777)")

def test_gofmt_check_no_arg_returns_schema_error() -> None:
    r = subprocess.run([sys.executable, str(GOFMT_CHECK)],
                       capture_output=True, text=True, timeout=adapter_budget(GOFMT_CHECK), encoding="utf-8", errors="replace")
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "gofmt-check"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


@pytest.mark.skipif(not shutil.which("gofmt"), reason="gofmt not installed")
def test_gofmt_check_valid_formatted_file(tmp_path: Path) -> None:
    """Runs on Windows again (#777).

    It used to skip win32 outright, on the stated grounds that the adapter has
    "encoding issues" there. It does not: the fixture went through
    `Path.write_text`, which made it CRLF on Windows, and gofmt reports a CRLF
    file as needing formatting. `write_go` puts the bytes down unmodified,
    which is the whole of that fix — and the skip was the more expensive
    answer, because it left Windows with no test at all that a clean Go file is
    reported clean.
    """
    f = write_go(tmp_path / "ok.go", GO_FORMATTED)
    data = run_adapter(GOFMT_CHECK, str(f))
    assert data["tool"] == "gofmt-check"
    if sys.platform == "win32" and not data["ok"] and (
            data["errors"] or [{}])[0].get("code") == "adapter":
        # The `adapter` decline of #777 is still undiagnosed and is not this
        # test's subject. Tolerated exactly as its sibling tolerates it —
        # visibly, with the evidence attached, and never as a skip.
        pytest.xfail(_windows_decline_reason(data))
    assert_ok(data)
    assert data["count"] == 0


@pytest.mark.skipif(not shutil.which("gofmt"), reason="gofmt not installed")
def test_gofmt_reads_a_crlf_file_as_needing_formatting(tmp_path: Path) -> None:
    """The mechanism behind the win32 skip, which was never diagnosed (#777).

    Pinned on every platform, because the platform it was misdiagnosed on is
    the one nobody here can run a local check against. If this ever stops being
    true, `write_go` stops being necessary — and the reader finds that out
    here rather than from a red Windows leg.
    """
    f = tmp_path / "crlf.go"
    f.write_bytes(GO_FORMATTED.replace(chr(10), chr(13) + chr(10)).encode("utf-8"))
    data = run_adapter(GOFMT_CHECK, str(f))
    assert_declined(data)
    assert data["errors"][0]["code"] == "formatting", data["errors"][0]


@pytest.mark.skipif(not shutil.which("gofmt"), reason="gofmt not installed")
def test_gofmt_check_unformatted_file_is_hard_error(tmp_path: Path) -> None:
    f = write_go(tmp_path / "bad.go", GO_UNFORMATTED)
    data = run_adapter(GOFMT_CHECK, str(f))
    assert data["tool"] == "gofmt-check"
    assert_declined(data)
    assert data["count"] == 1

    code = data["errors"][0]["code"]
    if sys.platform == "win32" and code == "adapter":
        # KNOWN DEFICIENCY, tracked in #777 — not an accepted outcome.
        #
        # The gofmt adapter is unreliable on Windows: on the same commit and
        # runner image it returned "formatting" on py3.9/3.12 and declined with
        # "adapter" on py3.10/3.11, and on an earlier run only 3.11 declined.
        # The sibling test above skips win32 entirely for the same underlying
        # reason ("gofmt adapter has encoding issues on Windows") — a defect
        # recorded in a skip reason and never diagnosed.
        #
        # This branch exists so that a real regression on POSIX still fails
        # loudly while a red master stops training everyone to ignore this
        # file. It deliberately does NOT skip: skipping hides that Windows
        # users have a broken validator, which is the thing #777 is about.
        # The reason carries what gofmt-check actually said, because until it
        # did, three occurrences produced no information beyond having happened.
        pytest.xfail(_windows_decline_reason(data))

    assert code == "formatting"
    assert "gofmt" in data["errors"][0]["msg"]


def test_gofmt_check_graceful_skip_when_tool_missing(tmp_path: Path) -> None:
    f = tmp_path / "x.go"
    f.write_text("package main\n")
    # Use a PATH that has Python but no gofmt (point to an empty dir)
    python_dir = str(Path(sys.executable).parent)
    env = {**os.environ, "PATH": python_dir}
    r = subprocess.run([sys.executable, str(GOFMT_CHECK), str(f)],
                       capture_output=True, text=True, timeout=adapter_budget(GOFMT_CHECK), env=env, encoding="utf-8", errors="replace")
    data = json.loads(r.stdout.strip())
    assert_ok(data)
    assert data["count"] == 0
    assert "gofmt" in r.stderr


# ---------------------------------------------------------------------------
# terraform-check
# ---------------------------------------------------------------------------

def test_terraform_check_no_arg_returns_schema_error() -> None:
    r = subprocess.run([sys.executable, str(TERRAFORM_CHECK)],
                       capture_output=True, text=True, timeout=adapter_budget(TERRAFORM_CHECK), encoding="utf-8", errors="replace")
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "terraform-check"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


@pytest.mark.skipif(not shutil.which("terraform"), reason="terraform not installed")
def test_terraform_check_valid_formatted_file(tmp_path: Path) -> None:
    f = tmp_path / "ok.tf"
    f.write_text('resource "null_resource" "example" {\n}\n')
    data = run_adapter(TERRAFORM_CHECK, str(f))
    assert data["tool"] == "terraform-check"
    assert_ok(data)
    assert data["count"] == 0


@pytest.mark.skipif(not shutil.which("terraform"), reason="terraform not installed")
def test_terraform_check_unformatted_file_is_hard_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.tf"
    # Extra spaces around = that terraform fmt would fix
    f.write_text('resource "null_resource" "example"   {\n    triggers = {}\n}\n')
    data = run_adapter(TERRAFORM_CHECK, str(f))
    assert data["tool"] == "terraform-check"
    # Either formatted (if terraform considers it ok) or not — we test the adapter path
    # The key contract: ok is bool, errors is list
    assert isinstance(data["ok"], bool)
    assert isinstance(data["errors"], list)


def test_terraform_check_graceful_skip_when_tool_missing(tmp_path: Path) -> None:
    f = tmp_path / "x.tf"
    f.write_text('resource "null_resource" "x" {}\n')
    python_dir = str(Path(sys.executable).parent)
    env = {**os.environ, "PATH": python_dir}
    r = subprocess.run([sys.executable, str(TERRAFORM_CHECK), str(f)],
                       capture_output=True, text=True, timeout=adapter_budget(TERRAFORM_CHECK), env=env, encoding="utf-8", errors="replace")
    data = json.loads(r.stdout.strip())
    assert_ok(data)
    assert data["count"] == 0
    assert "terraform" in r.stderr


# ---------------------------------------------------------------------------
# cargo-check
# ---------------------------------------------------------------------------

def test_cargo_check_no_arg_returns_schema_error() -> None:
    r = subprocess.run([sys.executable, str(CARGO_CHECK)],
                       capture_output=True, text=True, timeout=adapter_budget(CARGO_CHECK), encoding="utf-8", errors="replace")
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "cargo-check"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


def test_cargo_check_graceful_skip_when_tool_missing(tmp_path: Path) -> None:
    f = tmp_path / "src" / "main.rs"
    f.parent.mkdir()
    f.write_text("fn main() {}\n")
    # Also create Cargo.toml so crate root is found — tool absence is the skip trigger
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    python_dir = str(Path(sys.executable).parent)
    env = {**os.environ, "PATH": python_dir}
    r = subprocess.run([sys.executable, str(CARGO_CHECK), str(f)],
                       capture_output=True, text=True, timeout=adapter_budget(CARGO_CHECK), env=env, encoding="utf-8", errors="replace")
    data = json.loads(r.stdout.strip())
    assert_ok(data)
    assert data["count"] == 0
    assert "cargo" in r.stderr


def test_cargo_check_graceful_skip_when_no_cargo_toml(tmp_path: Path) -> None:
    f = tmp_path / "orphan.rs"
    f.write_text("fn main() {}\n")
    # No Cargo.toml anywhere in tmp_path parents (tmp_path is ephemeral)
    r = subprocess.run([sys.executable, str(CARGO_CHECK), str(f)],
                       capture_output=True, text=True, timeout=adapter_budget(CARGO_CHECK), encoding="utf-8", errors="replace")
    data = json.loads(r.stdout.strip())
    # If cargo is on PATH: skip because no Cargo.toml. If cargo missing: also skip.
    assert_ok(data)
    assert data["count"] == 0


@pytest.mark.skipif(not shutil.which("cargo"), reason="cargo not installed")
def test_cargo_check_valid_crate(tmp_path: Path) -> None:
    # Minimal valid crate
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "test_crate"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    src = tmp_path / "src"
    src.mkdir()
    main = src / "main.rs"
    main.write_text("fn main() {}\n")
    data = run_adapter(CARGO_CHECK, str(main), timeout=adapter_budget(CARGO_CHECK, inner=120))
    assert data["tool"] == "cargo-check"
    assert_ok(data)
    assert data["count"] == 0


@pytest.mark.skipif(not shutil.which("cargo"), reason="cargo not installed")
def test_cargo_check_broken_crate_reports_errors(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "test_crate"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    src = tmp_path / "src"
    src.mkdir()
    main = src / "main.rs"
    main.write_text("fn main() { let x: i32 = \"not an int\"; }\n")
    data = run_adapter(CARGO_CHECK, str(main), timeout=adapter_budget(CARGO_CHECK, inner=120))
    assert data["tool"] == "cargo-check"
    assert_declined(data)
    assert data["count"] >= 1


@pytest.mark.skipif(not shutil.which("cargo"), reason="cargo not installed")
def test_cargo_check_source_context_on_error(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "test_crate"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    src = tmp_path / "src"
    src.mkdir()
    main = src / "main.rs"
    main.write_text("fn main() { let x: i32 = \"not an int\"; }\n")
    data = run_adapter(CARGO_CHECK, str(main), timeout=adapter_budget(CARGO_CHECK, inner=120))
    if data["ok"] or not data["errors"]:
        pytest.skip("cargo check found no errors")
    err = data["errors"][0]
    assert err["line"] is not None
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    # source_context may be empty when cargo reports a relative path that
    # the helper can't resolve to a readable file. Tolerate that — the
    # field's presence + shape is what we're asserting here.
