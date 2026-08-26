"""Unit tests for the shared npx-absence marker (#1948, #1949).

`validators/common/npx_absent.py` is the module eslint.py and stylelint.py
both reduce their npx-refusal detection to, so it needs its own tests
independent of either adapter -- the module docstring explains why a third
adapter reaching for npx should not have to re-derive this list.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "validators" / "common"))
from npx_absent import is_npx_absent  # noqa: E402


# ---------------------------------------------------------------------------
# Must fire -- every spelling seen live
# ---------------------------------------------------------------------------

def test_npm_10_9_4_spelling_fires() -> None:
    """#1948's own reproduction, npm 10.9.4."""
    stderr = ('npm error npx canceled due to missing packages and no YES '
              'option: ["eslint@10.9.0"]').lower()
    assert is_npx_absent(stderr, "eslint")


def test_npm_10_9_4_spelling_fires_for_stylelint_too() -> None:
    stderr = ('npm error npx canceled due to missing packages and no YES '
              'option: ["stylelint@17.14.1"]').lower()
    assert is_npx_absent(stderr, "stylelint")


def test_npm_8_10_spelling_still_fires() -> None:
    assert is_npx_absent("npm error could not determine executable to run", "eslint")


def test_npm_11_spelling_still_fires_and_is_tool_specific() -> None:
    assert is_npx_absent('unknown command: "eslint"', "eslint")
    # The npm-11 spelling names the package it tried to run -- a stderr
    # naming a DIFFERENT package is not this tool's absence.
    assert not is_npx_absent('unknown command: "stylelint"', "eslint")


# ---------------------------------------------------------------------------
# Must NOT fire -- any other npx failure stays a loud fault
# ---------------------------------------------------------------------------

def test_an_unrelated_npx_failure_does_not_match() -> None:
    stderr = "npm error code eacces" + chr(10) + "npm error syscall open"
    assert not is_npx_absent(stderr, "eslint")


def test_the_no_yes_option_clause_alone_does_not_match() -> None:
    """The narrowness the issue asks for: only the stable prefix counts."""
    assert not is_npx_absent("no yes option", "eslint")


def test_empty_stderr_does_not_match() -> None:
    assert not is_npx_absent("", "eslint")
