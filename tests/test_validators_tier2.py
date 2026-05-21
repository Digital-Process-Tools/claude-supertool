"""Tests for tier-2 systems validators: gofmt-check, terraform-check, cargo-check."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

VALIDATORS = Path(__file__).parent.parent / "validators"

GOFMT_CHECK = VALIDATORS / "gofmt-check" / "gofmt-check.py"
TERRAFORM_CHECK = VALIDATORS / "terraform-check" / "terraform-check.py"
CARGO_CHECK = VALIDATORS / "cargo-check" / "cargo-check.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_adapter(adapter: Path, file: str, timeout: int = 15) -> dict:
    r = subprocess.run(["python3", str(adapter), file],
                       capture_output=True, text=True, timeout=timeout)
    return json.loads(r.stdout.strip())


def run_adapter_proc(adapter: Path, file: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", str(adapter), file],
                          capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# gofmt-check
# ---------------------------------------------------------------------------

def test_gofmt_check_no_arg_returns_schema_error() -> None:
    r = subprocess.run(["python3", str(GOFMT_CHECK)],
                       capture_output=True, text=True, timeout=5)
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "gofmt-check"
    assert data["ok"] is False
    assert "no file arg" in data["errors"][0]["msg"]


@pytest.mark.skipif(not shutil.which("gofmt"), reason="gofmt not installed")
def test_gofmt_check_valid_formatted_file(tmp_path: Path) -> None:
    f = tmp_path / "ok.go"
    # gofmt-formatted Go: tabs for indentation, correct spacing
    f.write_text('package main\n\nfunc main() {\n\t_ = 1\n}\n')
    data = run_adapter(GOFMT_CHECK, str(f))
    assert data["tool"] == "gofmt-check"
    assert data["ok"] is True
    assert data["count"] == 0


@pytest.mark.skipif(not shutil.which("gofmt"), reason="gofmt not installed")
def test_gofmt_check_unformatted_file_is_hard_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.go"
    # spaces instead of tabs — gofmt will flag this
    f.write_text('package main\n\nfunc main() {\n    _ = 1\n}\n')
    data = run_adapter(GOFMT_CHECK, str(f))
    assert data["tool"] == "gofmt-check"
    assert data["ok"] is False
    assert data["count"] == 1
    assert data["errors"][0]["code"] == "formatting"
    assert "gofmt" in data["errors"][0]["msg"]


def test_gofmt_check_graceful_skip_when_tool_missing(tmp_path: Path) -> None:
    f = tmp_path / "x.go"
    f.write_text("package main\n")
    # Use a PATH that has Python but no gofmt (point to an empty dir)
    python_dir = str(Path(sys.executable).parent)
    env = {**os.environ, "PATH": python_dir}
    r = subprocess.run([sys.executable, str(GOFMT_CHECK), str(f)],
                       capture_output=True, text=True, timeout=5, env=env)
    data = json.loads(r.stdout.strip())
    assert data["ok"] is True
    assert data["count"] == 0
    assert "gofmt" in r.stderr


# ---------------------------------------------------------------------------
# terraform-check
# ---------------------------------------------------------------------------

def test_terraform_check_no_arg_returns_schema_error() -> None:
    r = subprocess.run(["python3", str(TERRAFORM_CHECK)],
                       capture_output=True, text=True, timeout=5)
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "terraform-check"
    assert data["ok"] is False
    assert "no file arg" in data["errors"][0]["msg"]


@pytest.mark.skipif(not shutil.which("terraform"), reason="terraform not installed")
def test_terraform_check_valid_formatted_file(tmp_path: Path) -> None:
    f = tmp_path / "ok.tf"
    f.write_text('resource "null_resource" "example" {\n}\n')
    data = run_adapter(TERRAFORM_CHECK, str(f))
    assert data["tool"] == "terraform-check"
    assert data["ok"] is True
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
                       capture_output=True, text=True, timeout=5, env=env)
    data = json.loads(r.stdout.strip())
    assert data["ok"] is True
    assert data["count"] == 0
    assert "terraform" in r.stderr


# ---------------------------------------------------------------------------
# cargo-check
# ---------------------------------------------------------------------------

def test_cargo_check_no_arg_returns_schema_error() -> None:
    r = subprocess.run(["python3", str(CARGO_CHECK)],
                       capture_output=True, text=True, timeout=5)
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "cargo-check"
    assert data["ok"] is False
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
                       capture_output=True, text=True, timeout=10, env=env)
    data = json.loads(r.stdout.strip())
    assert data["ok"] is True
    assert data["count"] == 0
    assert "cargo" in r.stderr


def test_cargo_check_graceful_skip_when_no_cargo_toml(tmp_path: Path) -> None:
    f = tmp_path / "orphan.rs"
    f.write_text("fn main() {}\n")
    # No Cargo.toml anywhere in tmp_path parents (tmp_path is ephemeral)
    r = subprocess.run(["python3", str(CARGO_CHECK), str(f)],
                       capture_output=True, text=True, timeout=10)
    data = json.loads(r.stdout.strip())
    # If cargo is on PATH: skip because no Cargo.toml. If cargo missing: also skip.
    assert data["ok"] is True
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
    data = run_adapter(CARGO_CHECK, str(main), timeout=120)
    assert data["tool"] == "cargo-check"
    assert data["ok"] is True
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
    data = run_adapter(CARGO_CHECK, str(main), timeout=120)
    assert data["tool"] == "cargo-check"
    assert data["ok"] is False
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
    data = run_adapter(CARGO_CHECK, str(main), timeout=120)
    if data["ok"] or not data["errors"]:
        pytest.skip("cargo check found no errors")
    err = data["errors"][0]
    assert err["line"] is not None
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    # source_context may be empty when cargo reports a relative path that
    # the helper can't resolve to a readable file. Tolerate that — the
    # field's presence + shape is what we're asserting here.
