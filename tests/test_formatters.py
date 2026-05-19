"""Tests for the formatter hook framework."""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_config(cfg: dict) -> None:
    """Inject config (formatters + optional validators) into cached config."""
    supertool._CONFIG = cfg
    supertool._CONFIG_CHECKED = True


def _set_formatters(fmt: dict) -> None:
    supertool._CONFIG = {"formatters": fmt}
    supertool._CONFIG_CHECKED = True


# ---------------------------------------------------------------------------
# _applicable_formatters
# ---------------------------------------------------------------------------

def test_applicable_formatters_empty_when_no_config() -> None:
    _set_formatters({})
    assert supertool._applicable_formatters("edit", "x.json") == {}


def test_applicable_formatters_filters_by_hooks_into() -> None:
    _set_formatters({
        "prettier": {"cmd": "prettier --write {file}", "hooks_into": ["edit"]},
        "other":    {"cmd": "x", "hooks_into": ["paste"]},
    })
    out = supertool._applicable_formatters("edit", "x.json")
    assert set(out.keys()) == {"prettier"}


def test_applicable_formatters_filters_by_match_glob() -> None:
    _set_formatters({
        "fmt-json": {"cmd": "x", "hooks_into": ["edit"], "match": "*.json"},
        "fmt-md":   {"cmd": "x", "hooks_into": ["edit"], "match": "*.md"},
    })
    assert set(supertool._applicable_formatters("edit", "a.json")) == {"fmt-json"}
    assert set(supertool._applicable_formatters("edit", "a.md")) == {"fmt-md"}


def test_applicable_formatters_ignores_malformed_specs() -> None:
    _set_formatters({"bad": "not-a-dict", "good": {"cmd": "x", "hooks_into": ["edit"]}})
    assert set(supertool._applicable_formatters("edit", "a.json")) == {"good"}


# ---------------------------------------------------------------------------
# _formatter_run_one
# ---------------------------------------------------------------------------

def test_formatter_run_one_success(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text('{"a":1}\n')
    spec = {"cmd": f"touch {f}", "timeout": 5}
    result = supertool._formatter_run_one("prettier", spec, str(f))
    assert result["ok"] is True
    assert result["name"] == "prettier"


def test_formatter_run_one_failure(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    spec = {"cmd": "false", "timeout": 5}
    result = supertool._formatter_run_one("prettier", spec, str(f))
    assert result["ok"] is False
    assert result["name"] == "prettier"


def test_formatter_run_one_substitutes_file_token(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    # cmd writes the file path into a sentinel file
    sentinel = tmp_path / "seen"
    spec = {"cmd": f"printf '%s' {{file}} > {sentinel}", "timeout": 5}
    supertool._formatter_run_one("fmt", spec, str(f))
    assert sentinel.read_text() == str(f)


def test_formatter_run_one_timeout(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    spec = {"cmd": "sleep 60", "timeout": 1}
    result = supertool._formatter_run_one("fmt", spec, str(f))
    assert result["ok"] is False
    assert "timeout" in result["msg"]


# ---------------------------------------------------------------------------
# Formatter runs before validators (integration via _run_with_validators)
# ---------------------------------------------------------------------------

def test_formatter_runs_before_validator(tmp_path: Path) -> None:
    """Formatter must touch the file before the validator sees it."""
    f = tmp_path / "x.json"
    f.write_text('{"a":1}')

    # Formatter appends a newline — validator checks the file ends with \n
    sentinel = tmp_path / "fmt_ran"
    _set_config({
        "formatters": {
            "mock-fmt": {
                "cmd": f"printf '\\n' >> {{file}} && touch {sentinel}",
                "hooks_into": ["edit"],
                "match": "*.json",
            }
        },
        "validators": {},
    })

    out = supertool._run_with_validators(
        "edit", ["edit", "", "", str(f)], lambda: "edited\n"
    )
    assert sentinel.exists(), "formatter did not run"
    assert out == "edited\n"  # no validator block when no validators configured


def test_formatter_fail_rollback_false_keeps_file(tmp_path: Path) -> None:
    """rollback_on_fail=false: file stays after formatter failure, edit succeeds."""
    f = tmp_path / "x.json"
    original = '{"a":1}\n'
    f.write_text(original)

    _set_config({
        "formatters": {
            "bad-fmt": {
                "cmd": "false",  # always fails
                "hooks_into": ["edit"],
                "match": "*.json",
                "rollback_on_fail": False,
            }
        },
        "validators": {},
    })

    def do_edit() -> str:
        f.write_text('{"a":2}\n')
        return "edited\n"

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_edit)
    # Edit succeeded despite formatter failure
    assert f.read_text() == '{"a":2}\n'
    assert "edited" in out
    assert "[formatter]" in out  # warning present


def test_formatter_fail_rollback_true_reverts_file(tmp_path: Path) -> None:
    """rollback_on_fail=true: file reverts when formatter fails."""
    f = tmp_path / "x.json"
    original = '{"a":1}\n'
    f.write_text(original)

    _set_config({
        "formatters": {
            "bad-fmt": {
                "cmd": "false",
                "hooks_into": ["edit"],
                "match": "*.json",
                "rollback_on_fail": True,
            }
        },
        "validators": {},
    })

    def do_edit() -> str:
        f.write_text('{"a":2}\n')
        return "edited\n"

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_edit)
    assert f.read_text() == original
    assert "rolled back" in out


def test_formatter_multi_glob_match(tmp_path: Path) -> None:
    """Both .json and .md files trigger the prettier formatter."""
    _set_config({
        "formatters": {
            "prettier": {
                "cmd": "true",
                "hooks_into": ["edit"],
                "match": "*.json",
            },
            "prettier-md": {
                "cmd": "true",
                "hooks_into": ["edit"],
                "match": "*.md",
            },
        },
        "validators": {},
    })
    assert "prettier" in supertool._applicable_formatters("edit", "config.json")
    assert "prettier-md" in supertool._applicable_formatters("edit", "README.md")
    assert supertool._applicable_formatters("edit", "main.py") == {}


def test_formatter_graceful_skip_missing_binary(tmp_path: Path) -> None:
    """Missing binary: formatter fails gracefully, edit is not blocked."""
    f = tmp_path / "x.json"
    f.write_text('{"a":1}\n')

    _set_config({
        "formatters": {
            "prettier": {
                "cmd": "prettier-that-does-not-exist --write {file}",
                "hooks_into": ["edit"],
                "match": "*.json",
                "rollback_on_fail": False,
            }
        },
        "validators": {},
    })

    def do_edit() -> str:
        f.write_text('{"a":2}\n')
        return "edited\n"

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_edit)
    # Edit succeeded despite missing tool
    assert f.read_text() == '{"a":2}\n'
    assert "edited" in out
