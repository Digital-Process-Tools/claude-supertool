"""Tests for the lsp-diag validator's text-output parser.

The validator calls `supertool diag:FILE` and parses cclsp's free-form text response
into the validators/SCHEMA.md JSON shape. The text parsing is the fragile part —
these tests pin the patterns we support.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


# Load the validator module directly (it's a script, not a package member)
_VPATH = Path(__file__).parent.parent / "validators" / "lsp-diag" / "lsp-diag.py"
_spec = importlib.util.spec_from_file_location("lsp_diag", _VPATH)
assert _spec and _spec.loader
lsp_diag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lsp_diag)


def test_no_diagnostics_returns_empty() -> None:
    out = "No diagnostics found for /path/Foo.php. The file has no errors, warnings, or hints."
    assert lsp_diag.parse_cclsp_diagnostics(out, "/path/Foo.php") == []


def test_no_diagnostics_lowercase_variant() -> None:
    assert lsp_diag.parse_cclsp_diagnostics(
        "the file has no errors, warnings, or hints.", "/x") == []


def test_error_with_line_col_format() -> None:
    out = "• [error] undefined variable $foo at line 42, col 13"
    errors = lsp_diag.parse_cclsp_diagnostics(out, "/x")
    assert len(errors) == 1
    e = errors[0]
    assert e["severity"] == "error"
    assert e["line"] == 42
    assert e["col"] == 13
    assert "undefined variable" in e["msg"]


def test_warning_with_x_y_format() -> None:
    out = "[warning] unused import 12:5"
    errors = lsp_diag.parse_cclsp_diagnostics(out, "/x")
    assert len(errors) == 1
    assert errors[0]["severity"] == "warning"
    assert errors[0]["line"] == 12
    assert errors[0]["col"] == 5


def test_x_y_severity_msg_format() -> None:
    out = "10:3: error: Argument type mismatch"
    errors = lsp_diag.parse_cclsp_diagnostics(out, "/x")
    assert len(errors) == 1
    assert errors[0]["line"] == 10
    assert errors[0]["col"] == 3
    assert errors[0]["severity"] == "error"
    assert "type mismatch" in errors[0]["msg"]


def test_multiple_diagnostics() -> None:
    out = "\n".join([
        "Found 2 diagnostic(s) for /Widget.php:",
        "• [error] missing semicolon at line 5, col 10",
        "• [warning] unused variable $x at line 8, col 1",
    ])
    errors = lsp_diag.parse_cclsp_diagnostics(out, "/Widget.php")
    assert len(errors) == 2
    assert errors[0]["severity"] == "error"
    assert errors[1]["severity"] == "warning"


def test_unrecognized_text_falls_back_to_advisory() -> None:
    out = "some unexpected lsp message"
    errors = lsp_diag.parse_cclsp_diagnostics(out, "/x")
    assert len(errors) == 1
    assert errors[0]["severity"] == "info"
    assert "some unexpected" in errors[0]["msg"]
