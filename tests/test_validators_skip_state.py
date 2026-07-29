"""Skipped is a third validator state — not clean, not broken (issue #406).

A validator that declined to analyse a path has produced no information about
it. Reporting that as one error makes an unmeasured file indistinguishable from
a measured broken one, inflates the before/after delta by +1, and — for a
validator with rollback_on_fail — can revert a perfectly good edit.

These tests pin all three facts: the adapter reports SKIPPED, the delta stays 0,
and the edit survives.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import supertool

_VALIDATORS = Path(__file__).parent.parent / "validators"
_ADAPTER = _VALIDATORS / "phpstan-mcp" / "phpstan-mcp.py"
_PHPMD_ADAPTER = _VALIDATORS / "phpmd-mcp" / "phpmd-mcp.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_adapter():
    return _load(_ADAPTER, "phpstan_mcp_adapter")


def _mcp_resp(**structured) -> dict:
    return {"jsonrpc": "2.0", "id": 2,
            "result": {"structuredContent": structured}}


ALLOWLIST_MSG = "analyse: path is outside the configured --paths allowlist."


# ---------------------------------------------------------------------------
# Adapter: refusal-to-run → skipped, findings → errors
# ---------------------------------------------------------------------------

def test_allowlist_refusal_reports_skipped_not_an_error() -> None:
    """The reproduction: phpstan declining to analyse a path is not an error."""
    mod = _load_adapter()
    out = mod.format_response(
        "tests/FooTest.php",
        _mcp_resp(errors=[], exit_code=1, error=ALLOWLIST_MSG),
        120,
    )
    assert out.get("skipped"), f"expected a skipped state, got {out!r}"
    assert "--paths" in out["skipped"] or "allowlist" in out["skipped"]
    # #515: a skip omits the verdict keys. It reports no errors by carrying no
    # verdict at all, rather than by carrying a clean one.
    assert "count" not in out and "errors" not in out and "ok" not in out


def test_no_files_found_reports_skipped() -> None:
    mod = _load_adapter()
    out = mod.format_response(
        "tests/FooTest.php",
        _mcp_resp(errors=[], exit_code=1, error="No files found to analyse."),
        10,
    )
    assert out.get("skipped")
    assert "count" not in out and "ok" not in out  # #515


def test_extra_skip_patterns_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHPSTAN_MCP_SKIP_PATTERNS", "house rule refusal")
    mod = _load_adapter()
    out = mod.format_response(
        "src/Foo.php",
        _mcp_resp(errors=[], exit_code=2, error="phpstan: house rule refusal here"),
        10,
    )
    assert out.get("skipped")
    assert "count" not in out and "ok" not in out  # #515


def test_real_findings_still_reported_as_errors() -> None:
    """Guard: the fix must not swallow genuine phpstan findings."""
    mod = _load_adapter()
    out = mod.format_response(
        "src/Foo.php",
        _mcp_resp(errors=[{"line": 12, "identifier": "return.type",
                           "message": "bad return"}], exit_code=1),
        50,
    )
    assert "skipped" not in out
    assert out["ok"] is False
    assert out["count"] == 1
    assert out["errors"][0]["code"] == "return.type"


def test_unrecognised_nonzero_exit_still_reported_as_error() -> None:
    """Guard: an exit we cannot explain stays an error. No silent swallowing."""
    mod = _load_adapter()
    out = mod.format_response(
        "src/Foo.php",
        _mcp_resp(errors=[], exit_code=255, error="Internal error: segfault"),
        50,
    )
    assert "skipped" not in out
    assert out["ok"] is False
    assert out["count"] == 1
    assert out["errors"][0]["code"] == "phpstan.exit"


def test_transport_error_still_reported_as_error() -> None:
    mod = _load_adapter()
    out = mod.format_response("src/Foo.php", {"error": "socket closed"}, 5)
    assert "skipped" not in out
    assert out["ok"] is False
    assert out["count"] == 1


def test_clean_analysis_passes_through_main(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    """Guard: a file phpstan does analyse still reports a plain pass, not a skip."""
    mod = _load_adapter()
    target = tmp_path / "Foo.php"
    target.write_text("<?php\n")
    monkeypatch.setattr(mod, "ensure_daemon", lambda cwd: "/sock")
    monkeypatch.setattr(mod, "ndjson_call",
                        lambda s, f: _mcp_resp(errors=[], exit_code=0))
    captured: list = []
    monkeypatch.setattr("builtins.print", lambda s: captured.append(s))
    assert mod.main(["phpstan-mcp.py", str(target)]) == 0
    data = json.loads(captured[-1])
    assert "skipped" not in data
    assert data["ok"] is True


# ---------------------------------------------------------------------------
# phpmd-mcp shares the exit-string-to-one-error shape (same helper, same rule)
# ---------------------------------------------------------------------------

def _phpmd(structured: dict) -> dict:
    mod = _load(_PHPMD_ADAPTER, "phpmd_mcp_adapter")
    return mod.format_response("tests/FooTest.php",
                               {"result": {"structuredContent": structured}}, 30)


def test_phpmd_refusal_reports_skipped() -> None:
    out = _phpmd({"error": "analyse: path is outside the configured --paths allowlist."})
    assert out.get("skipped")
    assert "count" not in out and "errors" not in out and "ok" not in out  # #515


def test_phpmd_runtime_error_still_reported_as_error() -> None:
    """Guard: a SecurityError is a real failure, not a refusal."""
    out = _phpmd({"error": "SecurityError: refused to read outside the project",
                  "error_class": "SecurityError"})
    assert "skipped" not in out
    assert out["ok"] is False
    assert out["count"] == 1
    assert out["errors"][0]["code"] == "SecurityError"


# ---------------------------------------------------------------------------
# Core rendering: skipped rows carry their reason and no delta
# ---------------------------------------------------------------------------

def test_render_diff_skipped_names_the_reason() -> None:
    row = supertool._validator_render_diff(
        None, {"tool": "phpstan-mcp", "skipped": "path outside --paths allowlist",
               "elapsed_s": 0.1})[0]
    assert "skipped" in row
    assert "path outside --paths allowlist" in row
    assert supertool.mark("✗") not in row
    assert "(+1)" not in row and "0 → 1" not in row


def test_render_row_skipped_names_the_reason() -> None:
    row = supertool._validator_render_row(
        {"tool": "phpstan-mcp", "skipped": "path outside --paths allowlist"})[0]
    assert "skipped" in row
    assert "path outside --paths allowlist" in row


def test_skipped_result_is_not_cached() -> None:
    """A skip is config-derived, not content-derived — freezing it on a content
    hash would keep skipping a file that later comes into scope."""
    assert supertool._validator_result_is_cacheable(
        {"tool": "t", "ok": True, "count": 0, "errors": [],
         "skipped": "out of scope"}) is False
    assert supertool._validator_result_is_cacheable(
        {"tool": "t", "ok": True, "count": 0, "errors": []}) is True


# ---------------------------------------------------------------------------
# Core rollback: a skip must never revert an edit; a real regression still must
# ---------------------------------------------------------------------------

def _set_validators(cfg: dict) -> None:
    supertool._CONFIG = {"validators": cfg}
    supertool._CONFIG_CHECKED = True


def _switching_cmd(tmp_path: Path, first: dict, second: dict) -> str:
    state = tmp_path / "n"
    state.write_text("0")
    script = tmp_path / "_switch.py"
    script.write_text(
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(state)!r})\n"
        "n = int(p.read_text())\n"
        "p.write_text(str(n + 1))\n"
        f"sys.stdout.write({json.dumps(first)!r} if n == 0 else {json.dumps(second)!r})\n"
    )
    return f"{{python}} {script.as_posix()}"


_EDITED = "<?php\n// edited\n"
_ORIGINAL = "<?php\n// original\n"


def _run_edit(tmp_path: Path, post: dict, name: str = "phpstan-mcp") -> tuple:
    f = tmp_path / "x.php"
    f.write_text(_ORIGINAL)
    pre = {"tool": name, "file": str(f), "ok": True, "count": 0,
           "errors": [], "duration_ms": 1}
    _set_validators({name: {"cmd": _switching_cmd(tmp_path, pre, post),
                            "hooks_into": ["edit"], "match": "*.php",
                            "rollback_on_fail": True, "cache": False}})

    def do_edit() -> str:
        f.write_text(_EDITED)
        return "edited\n"

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_edit)
    return out, f


def test_skipped_validator_does_not_roll_back_the_edit(tmp_path: Path) -> None:
    """The load-bearing assertion: a skip leaves the edit on disk."""
    out, f = _run_edit(tmp_path, {"tool": "phpstan-mcp", "ok": True, "count": 0,
                                  "errors": [], "duration_ms": 1,
                                  "skipped": "path outside --paths allowlist"})
    assert f.read_text(encoding="utf-8") == _EDITED
    assert "rolled back" not in out
    assert "skipped" in out
    assert "path outside --paths allowlist" in out
    assert "(+1)" not in out


def test_skipped_result_claiming_failure_still_does_not_roll_back(tmp_path: Path) -> None:
    """Even an adapter that mislabels its skip as ok=False/count=1 must not
    revert the edit — `skipped` decides, not the leftover legacy fields."""
    out, f = _run_edit(tmp_path, {"tool": "phpstan-mcp", "ok": False, "count": 1,
                                  "errors": [{"line": None, "col": None,
                                              "severity": "error",
                                              "code": "phpstan.exit",
                                              "msg": ALLOWLIST_MSG}],
                                  "duration_ms": 1,
                                  "skipped": "path outside --paths allowlist"})
    assert f.read_text(encoding="utf-8") == _EDITED
    assert "rolled back" not in out


def test_real_new_error_still_rolls_back_and_reports_plus_one(tmp_path: Path) -> None:
    """Guard: the rollback mechanism itself is untouched."""
    out, f = _run_edit(tmp_path, {"tool": "phpstan-mcp", "ok": False, "count": 1,
                                  "errors": [{"line": 2, "col": None,
                                              "severity": "error",
                                              "code": "return.type",
                                              "msg": "bad return"}],
                                  "duration_ms": 1})
    assert f.read_text(encoding="utf-8") == _ORIGINAL
    assert "rolled back" in out
    assert "(+1)" in out
    assert supertool.mark("✗") in out


def test_rollback_is_not_triggered_by_a_name_prefix_neighbour(tmp_path: Path) -> None:
    """`phpstan` must not be rolled back because `phpstan-mcp` went red.

    The decision used to be a substring scan of rendered rows, so a validator
    whose name is a prefix of a failing neighbour's inherited its ✗.
    """
    f = tmp_path / "x.php"
    f.write_text(_ORIGINAL)
    clean = {"tool": "phpstan", "file": str(f), "ok": True, "count": 0,
             "errors": [], "duration_ms": 1}
    clean_mcp = {**clean, "tool": "phpstan-mcp"}
    red = {"tool": "phpstan-mcp", "file": str(f), "ok": False, "count": 1,
           "errors": [{"line": 2, "col": None, "severity": "error",
                       "code": "return.type", "msg": "bad return"}],
           "duration_ms": 1}
    sub = tmp_path / "neighbour"
    sub.mkdir()
    _set_validators({
        "phpstan": {"cmd": _switching_cmd(tmp_path, clean, clean),
                    "hooks_into": ["edit"], "match": "*.php",
                    "rollback_on_fail": True, "cache": False},
        "phpstan-mcp": {"cmd": _switching_cmd(sub, clean_mcp, red),
                        "hooks_into": ["edit"], "match": "*.php",
                        "rollback_on_fail": False, "cache": False},
    })

    def do_edit() -> str:
        f.write_text(_EDITED)
        return "edited\n"

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_edit)
    assert "rolled back" not in out
    assert f.read_text(encoding="utf-8") == _EDITED
