"""Tests for the 7 language-native formatters added in .supertool.example.json.

Each formatter is tested with:
- dispatch_ok: correct glob match triggers the formatter
- graceful_skip: missing binary warns but does not block the edit

Tests mock the formatter cmd — no real black/gofmt/etc. install required.
"""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _touch(sentinel: Path) -> str:
    """Cross-platform `touch` replacement for formatter cmd fixtures.

    Tests previously used `touch X` which is Unix-only — Windows runners
    have no `touch` on PATH so the formatter cmd failed and sentinel was
    never created. Use a Python one-liner instead. Forward-slash path
    (Path.as_posix) avoids shlex.split backslash-escape mangling.
    """
    return f"{{python}} -c \"open(r'{sentinel.as_posix()}', 'w').close()\""


def _set_formatter(name: str, cmd: str, match: str) -> None:
    supertool._CONFIG = {
        "formatters": {
            name: {
                "cmd": cmd,
                "match": match,
                "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
                "rollback_on_fail": False,
                "timeout": 30,
            }
        },
        "validators": {},
    }
    supertool._CONFIG_CHECKED = True


def _edit(f: Path, content: str = "new\n") -> str:
    def do_edit() -> str:
        f.write_text(content)
        return "edited\n"

    return supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_edit)


# ---------------------------------------------------------------------------
# black — *.py
# ---------------------------------------------------------------------------

def test_black_dispatch_ok(tmp_path: Path) -> None:
    f = tmp_path / "hello.py"
    f.write_text("x=1\n")
    # Since #393 a known formatter runs only where the repo shows it opts in.
    (tmp_path / "pyproject.toml").write_text("[tool.black]\n")
    sentinel = tmp_path / "ran"
    _set_formatter("black", _touch(sentinel), "*.py")
    _edit(f)
    assert sentinel.exists()


def test_black_glob_no_match(tmp_path: Path) -> None:
    f = tmp_path / "hello.go"
    f.write_text("package main\n")
    sentinel = tmp_path / "ran"
    _set_formatter("black", _touch(sentinel), "*.py")
    _edit(f)
    assert not sentinel.exists()


def test_black_graceful_skip_missing_binary(tmp_path: Path) -> None:
    f = tmp_path / "hello.py"
    f.write_text("x=1\n")
    _set_formatter("black", "black-that-does-not-exist --quiet {file}", "*.py")
    out = _edit(f)
    assert f.read_text(encoding="utf-8") == "new\n"
    assert "edited" in out


# ---------------------------------------------------------------------------
# gofmt — *.go
# ---------------------------------------------------------------------------

def test_gofmt_dispatch_ok(tmp_path: Path) -> None:
    f = tmp_path / "main.go"
    f.write_text("package main\n")
    sentinel = tmp_path / "ran"
    _set_formatter("gofmt", _touch(sentinel), "*.go")
    _edit(f)
    assert sentinel.exists()


def test_gofmt_glob_no_match(tmp_path: Path) -> None:
    f = tmp_path / "main.py"
    f.write_text("x=1\n")
    sentinel = tmp_path / "ran"
    _set_formatter("gofmt", _touch(sentinel), "*.go")
    _edit(f)
    assert not sentinel.exists()


def test_gofmt_graceful_skip_missing_binary(tmp_path: Path) -> None:
    f = tmp_path / "main.go"
    f.write_text("package main\n")
    _set_formatter("gofmt", "gofmt-that-does-not-exist -w {file}", "*.go")
    out = _edit(f)
    assert f.read_text(encoding="utf-8") == "new\n"
    assert "edited" in out


# ---------------------------------------------------------------------------
# rustfmt — *.rs
# ---------------------------------------------------------------------------

def test_rustfmt_dispatch_ok(tmp_path: Path) -> None:
    f = tmp_path / "lib.rs"
    f.write_text("fn main(){}\n")
    (tmp_path / "rustfmt.toml").write_text("")
    sentinel = tmp_path / "ran"
    _set_formatter("rustfmt", _touch(sentinel), "*.rs")
    _edit(f)
    assert sentinel.exists()


def test_rustfmt_glob_no_match(tmp_path: Path) -> None:
    f = tmp_path / "lib.go"
    f.write_text("package main\n")
    sentinel = tmp_path / "ran"
    _set_formatter("rustfmt", _touch(sentinel), "*.rs")
    _edit(f)
    assert not sentinel.exists()


def test_rustfmt_graceful_skip_missing_binary(tmp_path: Path) -> None:
    f = tmp_path / "lib.rs"
    f.write_text("fn main(){}\n")
    _set_formatter("rustfmt", "rustfmt-that-does-not-exist {file}", "*.rs")
    out = _edit(f)
    assert f.read_text(encoding="utf-8") == "new\n"
    assert "edited" in out


# ---------------------------------------------------------------------------
# phpcbf — *.php
# ---------------------------------------------------------------------------

def test_phpcbf_dispatch_ok(tmp_path: Path) -> None:
    f = tmp_path / "Foo.php"
    f.write_text("<?php\n")
    (tmp_path / "phpcs.xml").write_text("<ruleset/>\n")
    sentinel = tmp_path / "ran"
    _set_formatter("phpcbf", _touch(sentinel), "*.php")
    _edit(f)
    assert sentinel.exists()


def test_phpcbf_glob_no_match(tmp_path: Path) -> None:
    f = tmp_path / "Foo.py"
    f.write_text("x=1\n")
    sentinel = tmp_path / "ran"
    _set_formatter("phpcbf", _touch(sentinel), "*.php")
    _edit(f)
    assert not sentinel.exists()


def test_phpcbf_graceful_skip_missing_binary(tmp_path: Path) -> None:
    f = tmp_path / "Foo.php"
    f.write_text("<?php\n")
    _set_formatter("phpcbf", "phpcbf-that-does-not-exist --standard=PSR12 {file}", "*.php")
    out = _edit(f)
    assert f.read_text(encoding="utf-8") == "new\n"
    assert "edited" in out


# ---------------------------------------------------------------------------
# shfmt — *.sh
# ---------------------------------------------------------------------------

def test_shfmt_dispatch_ok(tmp_path: Path) -> None:
    f = tmp_path / "deploy.sh"
    f.write_text("#!/bin/bash\necho hi\n")
    sentinel = tmp_path / "ran"
    _set_formatter("shfmt", _touch(sentinel), "*.sh")
    _edit(f)
    assert sentinel.exists()


def test_shfmt_glob_no_match(tmp_path: Path) -> None:
    f = tmp_path / "deploy.py"
    f.write_text("x=1\n")
    sentinel = tmp_path / "ran"
    _set_formatter("shfmt", _touch(sentinel), "*.sh")
    _edit(f)
    assert not sentinel.exists()


def test_shfmt_graceful_skip_missing_binary(tmp_path: Path) -> None:
    f = tmp_path / "deploy.sh"
    f.write_text("#!/bin/bash\necho hi\n")
    _set_formatter("shfmt", "shfmt-that-does-not-exist -w {file}", "*.sh")
    out = _edit(f)
    assert f.read_text(encoding="utf-8") == "new\n"
    assert "edited" in out


# ---------------------------------------------------------------------------
# terraform-fmt — *.tf
# ---------------------------------------------------------------------------

def test_terraform_fmt_dispatch_ok(tmp_path: Path) -> None:
    f = tmp_path / "main.tf"
    f.write_text('resource "aws_s3_bucket" "b" {}\n')
    sentinel = tmp_path / "ran"
    _set_formatter("terraform-fmt", _touch(sentinel), "*.tf")
    _edit(f)
    assert sentinel.exists()


def test_terraform_fmt_glob_no_match(tmp_path: Path) -> None:
    f = tmp_path / "main.py"
    f.write_text("x=1\n")
    sentinel = tmp_path / "ran"
    _set_formatter("terraform-fmt", _touch(sentinel), "*.tf")
    _edit(f)
    assert not sentinel.exists()


def test_terraform_fmt_graceful_skip_missing_binary(tmp_path: Path) -> None:
    f = tmp_path / "main.tf"
    f.write_text('resource "aws_s3_bucket" "b" {}\n')
    _set_formatter("terraform-fmt", "terraform-that-does-not-exist fmt -write=true {file}", "*.tf")
    out = _edit(f)
    assert f.read_text(encoding="utf-8") == "new\n"
    assert "edited" in out


# ---------------------------------------------------------------------------
# rubocop — *.rb
# ---------------------------------------------------------------------------

def test_rubocop_dispatch_ok(tmp_path: Path) -> None:
    f = tmp_path / "app.rb"
    f.write_text("puts 'hello'\n")
    sentinel = tmp_path / "ran"
    _set_formatter("rubocop", _touch(sentinel), "*.rb")
    _edit(f)
    assert sentinel.exists()


def test_rubocop_glob_no_match(tmp_path: Path) -> None:
    f = tmp_path / "app.py"
    f.write_text("x=1\n")
    sentinel = tmp_path / "ran"
    _set_formatter("rubocop", _touch(sentinel), "*.rb")
    _edit(f)
    assert not sentinel.exists()


def test_rubocop_graceful_skip_missing_binary(tmp_path: Path) -> None:
    f = tmp_path / "app.rb"
    f.write_text("puts 'hello'\n")
    _set_formatter("rubocop", "rubocop-that-does-not-exist -a --no-color --format quiet {file}", "*.rb")
    out = _edit(f)
    assert f.read_text(encoding="utf-8") == "new\n"
    assert "edited" in out
