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
    # requires_config: this repo has no .prettierrc, and since #393 that alone
    # would gate prettier out — which is not what this test is about.
    _set_formatters({
        "prettier": {"cmd": "prettier --write {file}", "hooks_into": ["edit"],
                     "requires_config": False},
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


def test_applicable_formatters_exclude_mirrors_validators() -> None:
    _set_formatters({
        "fmt": {"cmd": "x", "hooks_into": ["edit"], "match": "*.php", "exclude": "*tests/*"},
    })
    assert set(supertool._applicable_formatters("edit", "tests/aTest.php")) == set()
    assert set(supertool._applicable_formatters("edit", "src/Foo.php")) == {"fmt"}


def test_applicable_formatters_exclude_list() -> None:
    _set_formatters({
        "fmt": {"cmd": "x", "hooks_into": ["edit"], "match": "*.php",
                "exclude": ["*tests/*", "*/Generated/*"]},
    })
    assert set(supertool._applicable_formatters("edit", "tests/aTest.php")) == set()
    assert set(supertool._applicable_formatters("edit", "src/Generated/Foo.php")) == set()
    assert set(supertool._applicable_formatters("edit", "src/Foo.php")) == {"fmt"}


# ---------------------------------------------------------------------------
# _formatter_run_one
# ---------------------------------------------------------------------------

def test_formatter_run_one_success(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text('{"a":1}\n')
    # Cross-platform `touch` — Windows runners have no `touch` binary.
    cmd = f"{{python}} -c \"open(r'{f.as_posix()}', 'w').close()\""
    spec = {"cmd": cmd, "timeout": 5}
    result = supertool._formatter_run_one("prettier", spec, str(f))
    assert result["ok"] is True
    assert result["name"] == "prettier"


def test_formatter_run_one_failure(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    # Cross-platform non-zero exit — Windows has no `false` binary.
    spec = {"cmd": "{python} -c \"raise SystemExit(1)\"", "timeout": 5}
    result = supertool._formatter_run_one("prettier", spec, str(f))
    assert result["ok"] is False
    assert result["name"] == "prettier"


def test_formatter_run_one_substitutes_file_token(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    # Adapter writes argv[1] (substituted {file}) into sentinel — no shell redirect.
    sentinel = tmp_path / "seen"
    adapter = tmp_path / "write_sentinel.py"
    adapter.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(sentinel)!r}).write_text(sys.argv[1])\n"
    )
    # `python3` only exists by name on POSIX; Windows uses `python`.
    # as_posix avoids shlex backslash-escape mangling of Windows paths.
    spec = {"cmd": f"{{python}} {adapter.as_posix()} {{file}}", "timeout": 5}
    supertool._formatter_run_one("fmt", spec, str(f))
    assert sentinel.read_text(encoding="utf-8") == str(f)


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
    adapter = tmp_path / "append_nl.py"
    adapter.write_text(
        "import sys, pathlib\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "p.write_text(p.read_text() + '\\n')\n"
        f"pathlib.Path({str(sentinel)!r}).touch()\n"
    )
    _set_config({
        "formatters": {
            "mock-fmt": {
                "cmd": f"{{python}} {adapter.as_posix()} {{file}}",
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
                "cmd": "{python} -c \"raise SystemExit(1)\"",  # always fails
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
    assert f.read_text(encoding="utf-8") == '{"a":2}\n'
    assert "edited" in out
    assert "[formatters]" in out  # warning block present
    assert "fail" in out  # row shows failure


def test_formatter_fail_rollback_true_reverts_file(tmp_path: Path) -> None:
    """rollback_on_fail=true: file reverts when formatter fails."""
    f = tmp_path / "x.json"
    original = '{"a":1}\n'
    f.write_text(original)

    _set_config({
        "formatters": {
            "bad-fmt": {
                "cmd": "{python} -c \"raise SystemExit(1)\"",
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
    assert f.read_text(encoding="utf-8") == original
    assert "rolled back" in out


def test_formatter_multi_glob_match(tmp_path: Path) -> None:
    """Both .json and .md files trigger the prettier formatter."""
    # requires_config: the glob is what is under test, not the #393 opt-in gate.
    _set_config({
        "formatters": {
            "prettier": {
                "cmd": "true",
                "hooks_into": ["edit"],
                "match": "*.json",
                "requires_config": False,
            },
            "prettier-md": {
                "cmd": "true",
                "hooks_into": ["edit"],
                "match": "*.md",
                "requires_config": False,
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
    assert f.read_text(encoding="utf-8") == '{"a":2}\n'
    assert "edited" in out


# ---------------------------------------------------------------------------
# _formatter_render_row — unified row format
# ---------------------------------------------------------------------------

def test_formatter_render_row_noop_returns_none() -> None:
    """ok=True + 0/0 metrics → None (silent)."""
    result = {
        "name": "prettier", "tool": "prettier-write", "ok": True,
        "duration_ms": 42, "metrics": {"lines_added": 0, "lines_removed": 0},
    }
    assert supertool._formatter_render_row(result) is None


def test_formatter_render_row_changes_returns_row() -> None:
    """ok=True with changes → row with +N -N."""
    result = {
        "name": "prettier", "ok": True,
        "duration_ms": 55, "metrics": {"lines_added": 3, "lines_removed": 1},
    }
    row = supertool._formatter_render_row(result)
    assert row is not None
    assert "+3" in row
    assert "-1" in row
    assert "ok" in row


def test_formatter_render_row_failure_always_shows() -> None:
    """ok=False → row always rendered, even with 0/0 metrics."""
    result = {
        "name": "phpcbf", "ok": False,
        "duration_ms": 10,
        "errors": [{"line": None, "col": None, "severity": "error",
                    "code": "adapter", "msg": "PHPCBF_BIN not found: phpcbf"}],
        "metrics": {"lines_added": 0, "lines_removed": 0},
    }
    row = supertool._formatter_render_row(result)
    assert row is not None
    assert "fail" in row
    assert "phpcbf" in row


def test_formatter_render_row_verify_failed_is_not_silent() -> None:
    """MUST NOT BE SILENT (#2162): `ok=True` + 0/0 metrics is what a genuine
    no-op looks like -- but a formatter that ran, mutated the file, and then
    hit an `OSError` re-reading it to compute the diff reports the exact same
    shape today. Dropping this row (the "silent no-op" branch above) makes a
    real edit indistinguishable from nothing having happened.
    """
    result = {
        "name": "ruff-format", "ok": True,
        "duration_ms": 12, "metrics": {"lines_added": 0, "lines_removed": 0},
        "verify_failed": "[Errno 2] No such file or directory: 'x.py'",
    }
    row = supertool._formatter_render_row(result)
    assert row is not None, (
        "a re-read failure after a real format run must not render the same "
        "as a no-op -- the caller cannot tell 'nothing changed' from "
        "'something changed and we could not measure it'")
    assert "x.py" in row or "could not verify" in row.lower() or \
        "verify" in row.lower()


def test_formatter_render_row_noop_without_verify_failed_still_silent() -> None:
    """MUST FIRE: the ordinary no-op path is untouched by the new field."""
    result = {
        "name": "ruff-format", "ok": True,
        "duration_ms": 12, "metrics": {"lines_added": 0, "lines_removed": 0},
    }
    assert supertool._formatter_render_row(result) is None


# ---------------------------------------------------------------------------
# Silent-on-noop / block omission integration via _run_with_validators
# ---------------------------------------------------------------------------

def _make_schema_json_cmd(ok: bool, added: int, removed: int, tool: str) -> str:
    """Return a shell cmd that emits SCHEMA JSON and exits 0."""
    import json as _json
    payload = _json.dumps({
        "tool": tool, "file": "", "ok": ok, "count": 0,
        "errors": [], "duration_ms": 5,
        "metrics": {"lines_added": added, "lines_removed": removed},
    })
    # Use printf so no shell quoting issues with the JSON
    escaped = payload.replace("'", "'\\''")
    return f"printf '%s\\n' '{escaped}'"


def test_all_formatters_noop_no_block(tmp_path: Path) -> None:
    """3 formatters all ok+0/0 → no [formatters] block in output."""
    f = tmp_path / "x.json"
    f.write_text('{"a":1}\n')

    _set_config({
        "formatters": {
            "fmt-a": {"cmd": _make_schema_json_cmd(True, 0, 0, "fmt-a"),
                      "hooks_into": ["edit"], "match": "*.json"},
            "fmt-b": {"cmd": _make_schema_json_cmd(True, 0, 0, "fmt-b"),
                      "hooks_into": ["edit"], "match": "*.json"},
            "fmt-c": {"cmd": _make_schema_json_cmd(True, 0, 0, "fmt-c"),
                      "hooks_into": ["edit"], "match": "*.json"},
        },
        "validators": {},
    })

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], lambda: "edited\n")
    assert "[formatters]" not in out


def test_one_formatter_changes_file_shows_only_that_one(tmp_path: Path) -> None:
    """1 of 2 formatters has changes → [formatters] block with only that row."""
    f = tmp_path / "x.json"
    f.write_text('{"a":1}\n')

    _set_config({
        "formatters": {
            "noop-fmt": {"cmd": _make_schema_json_cmd(True, 0, 0, "noop-fmt"),
                         "hooks_into": ["edit"], "match": "*.json"},
            "active-fmt": {"cmd": _make_schema_json_cmd(True, 2, 1, "active-fmt"),
                           "hooks_into": ["edit"], "match": "*.json"},
        },
        "validators": {},
    })

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], lambda: "edited\n")
    assert "[formatters]" in out
    assert "active-fmt" in out
    assert "noop-fmt" not in out
    assert "+2" in out
    assert "-1" in out


def test_failed_formatter_row_shown(tmp_path: Path) -> None:
    """ok=False formatter → row in [formatters] block with error info."""
    f = tmp_path / "x.json"
    f.write_text('{"a":1}\n')

    import json as _json
    err_payload = _json.dumps({
        "tool": "bad-fmt", "file": str(f), "ok": False, "count": 1,
        "errors": [{"line": None, "col": None, "severity": "error",
                    "code": "adapter", "msg": "binary not found"}],
        "duration_ms": 3,
        "metrics": {"lines_added": 0, "lines_removed": 0},
    })
    escaped = err_payload.replace("'", "'\\''")
    cmd = f"printf '%s\\n' '{escaped}'"

    _set_config({
        "formatters": {
            "bad-fmt": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.json",
                        "rollback_on_fail": False},
        },
        "validators": {},
    })

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], lambda: "edited\n")
    assert "[formatters]" in out
    assert "fail" in out


# ---------------------------------------------------------------------------
# Legacy non-JSON adapter (raw output preserved)
# ---------------------------------------------------------------------------

def test_formatter_legacy_non_json_captures_raw_stdout(tmp_path: Path) -> None:
    """Adapter prints non-JSON stdout → result has 'raw' field, no 'metrics' parsing."""
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    spec = {"cmd": "echo 'reformatted x.json'", "timeout": 5}
    result = supertool._formatter_run_one("legacy-fmt", spec, str(f))
    assert result["name"] == "legacy-fmt"
    assert result["ok"] is True
    assert "raw" in result
    assert "reformatted x.json" in result["raw"]


def test_formatter_legacy_non_json_clean_empty_output_is_silent() -> None:
    """ok=true + empty raw → render returns None (quiet)."""
    result = {"name": "x", "ok": True, "raw": "", "duration_ms": 0,
              "metrics": {"lines_added": 0, "lines_removed": 0}}
    assert supertool._formatter_render_row(result) is None


def test_formatter_legacy_non_json_with_output_renders_verbatim() -> None:
    """ok=true + non-empty raw → render shows the raw output."""
    result = {"name": "fmt", "ok": True, "raw": "rewrote file", "duration_ms": 0,
              "metrics": {"lines_added": 0, "lines_removed": 0}}
    row = supertool._formatter_render_row(result)
    assert row is not None
    assert "rewrote file" in row
    assert "ok" in row


def test_formatter_legacy_non_json_failure_renders_with_raw() -> None:
    """ok=false + raw → row shows fail + raw output."""
    result = {"name": "fmt", "ok": False, "raw": "boom", "duration_ms": 0,
              "metrics": {"lines_added": 0, "lines_removed": 0}}
    row = supertool._formatter_render_row(result)
    assert row is not None
    assert "fail" in row
    assert "boom" in row


def test_formatter_legacy_non_json_failure_without_raw_still_renders() -> None:
    """ok=false + empty raw → row still shows fail (never silent on failure)."""
    result = {"name": "fmt", "ok": False, "raw": "", "duration_ms": 0,
              "metrics": {"lines_added": 0, "lines_removed": 0}}
    row = supertool._formatter_render_row(result)
    assert row is not None
    assert "fail" in row
