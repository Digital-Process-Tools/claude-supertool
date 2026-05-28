"""Tests for the validator hook framework (PR1)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_validators(cfg: dict) -> None:
    """Inject a validators block into the cached config."""
    supertool._CONFIG = {"validators": cfg}
    supertool._CONFIG_CHECKED = True


def _fake_cmd(payload: dict) -> str:
    """Build a cross-platform cmd that prints the given JSON payload.

    Base64-encodes the JSON so it survives nested-quote escaping across both
    POSIX and Windows. printf is POSIX-only; this {python} -c approach runs
    on Windows runners too.
    """
    import base64
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return (
        f'{{python}} -c "import sys, base64; '
        f"sys.stdout.write(base64.b64decode('{encoded}').decode())"
        f'"'
    )


# ---------------------------------------------------------------------------
# _applicable_validators
# ---------------------------------------------------------------------------

def test_applicable_no_validators_returns_empty() -> None:
    _set_validators({})
    assert supertool._applicable_validators("edit", "x.php") == {}


def test_applicable_filters_by_hooks_into() -> None:
    _set_validators({
        "a": {"cmd": "x", "hooks_into": ["edit"]},
        "b": {"cmd": "x", "hooks_into": ["paste"]},
    })
    out = supertool._applicable_validators("edit", "x.php")
    assert set(out.keys()) == {"a"}


def test_applicable_filters_by_match_glob() -> None:
    _set_validators({
        "php": {"cmd": "x", "hooks_into": ["edit"], "match": "*.php"},
        "py":  {"cmd": "x", "hooks_into": ["edit"], "match": "*.py"},
    })
    assert set(supertool._applicable_validators("edit", "a.php")) == {"php"}
    assert set(supertool._applicable_validators("edit", "a.py")) == {"py"}


def test_applicable_skips_opt_in() -> None:
    _set_validators({
        "auto":   {"cmd": "x", "hooks_into": ["edit"]},
        "manual": {"cmd": "x", "hooks_into": ["edit"], "opt_in": True},
    })
    assert set(supertool._applicable_validators("edit", "a.php")) == {"auto"}


def test_applicable_ignores_malformed_specs() -> None:
    _set_validators({"bad": "not-a-dict", "good": {"cmd": "x", "hooks_into": ["edit"]}})
    assert set(supertool._applicable_validators("edit", "a.php")) == {"good"}


# ---------------------------------------------------------------------------
# _validator_resolve
# ---------------------------------------------------------------------------

def test_resolve_returns_file_when_no_resolve_cmd() -> None:
    assert supertool._validator_resolve({}, "a.php") == "a.php"


def test_resolve_invokes_cmd_and_returns_first_line() -> None:
    spec = {"resolve": '{python} -c "import sys; sys.stdout.write(\'tests/aTest.php\\\\nextra\')"'}
    assert supertool._validator_resolve(spec, "a.php") == "tests/aTest.php"


def test_resolve_returns_none_on_empty_output() -> None:
    spec = {"resolve": "{python} -c \"pass\""}  # exit 0, no stdout
    assert supertool._validator_resolve(spec, "a.php") is None


# ---------------------------------------------------------------------------
# _validator_run_one
# ---------------------------------------------------------------------------

def test_run_one_parses_adapter_json() -> None:
    payload = {"tool": "fake", "file": "x.php", "ok": True, "count": 0,
               "errors": [], "duration_ms": 5}
    spec = {"cmd": _fake_cmd(payload), "timeout": 5}
    out = supertool._validator_run_one("fake", spec, "x.php")
    assert out["ok"] is True
    assert out["count"] == 0


def test_run_one_handles_no_output() -> None:
    spec = {"cmd": "{python} -c \"pass\"", "timeout": 5}
    out = supertool._validator_run_one("fake", spec, "x.php")
    assert out["ok"] is False
    assert "no output" in out["errors"][0]["msg"]


def test_run_one_handles_bad_json() -> None:
    spec = {"cmd": "{python} -c \"import sys; sys.stdout.write('not json')\"", "timeout": 5}
    out = supertool._validator_run_one("fake", spec, "x.php")
    assert out["ok"] is False
    assert "bad json" in out["errors"][0]["msg"]


def test_run_one_substitutes_file_token(tmp_path: Path) -> None:
    payload = {"tool": "fake", "file": "x.php", "ok": True, "count": 0,
               "errors": [], "duration_ms": 1}
    # Adapter checks argv[1] == 'a.php' before emitting payload (gates on substituted {file}).
    adapter = tmp_path / "check_file.py"
    adapter.write_text(
        "import sys\n"
        "if sys.argv[1] != 'a.php':\n"
        "    sys.exit(0)\n"
        f"sys.stdout.write({json.dumps(payload)!r})\n"
    )
    spec = {"cmd": f"{{python}} {adapter.as_posix()} {{file}}", "timeout": 5}
    out = supertool._validator_run_one("fake", spec, "a.php")
    assert out["ok"] is True


def _counter_cmd(state_path: Path, ok_payload: str, fail_marker: str = "BROKEN") -> str:
    """Returns ok_payload on first call, fail_marker on subsequent calls.

    Uses a Python wrapper (no shell) so it works under shell=False dispatch.
    """
    script = state_path.parent / f"_counter_{state_path.name}.py"
    script.write_text(
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(state_path)!r})\n"
        "n = int(p.read_text())\n"
        "p.write_text(str(n+1))\n"
        "if n == 0:\n"
        f"    sys.stdout.write({ok_payload!r})\n"
        "else:\n"
        f"    sys.stdout.write({fail_marker!r})\n"
    )
    return f"{{python}} {script.as_posix()}"


def test_cache_hit_skips_adapter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_validator_cache_path",
                        lambda k: tmp_path / "cache" / f"{k}.json")
    f = tmp_path / "x.php"
    f.write_text("<?php\n")
    state = tmp_path / "n"
    state.write_text("0")
    ok = json.dumps({"tool": "t", "file": "x", "ok": True, "count": 0,
                     "errors": [], "duration_ms": 1}).replace("'", "'\\''")
    spec = {"cmd": _counter_cmd(state, ok), "timeout": 5}
    out1 = supertool._validator_run_one("t", spec, str(f))
    assert out1["ok"] is True
    # Second call: file unchanged → cache hit → adapter NOT re-run
    out2 = supertool._validator_run_one("t", spec, str(f))
    assert out2["ok"] is True
    # Counter only incremented once (first call); cache hit prevented second run
    assert state.read_text().strip() == "1"


def test_cache_invalidates_on_file_change(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_validator_cache_path",
                        lambda k: tmp_path / "cache" / f"{k}.json")
    f = tmp_path / "x.php"
    f.write_text("<?php\n")
    payload = {"tool": "t", "file": "x", "ok": True, "count": 0,
               "errors": [], "duration_ms": 1}
    spec = {"cmd": _fake_cmd(payload), "timeout": 5}
    supertool._validator_run_one("t", spec, str(f))
    # Change file content → cache key changes → adapter must re-run
    f.write_text("<?php\n$x = 1;\n")
    # Use stateful counter to detect re-run
    state = tmp_path / "n"
    state.write_text("0")
    ok = json.dumps(payload).replace("'", "'\\''")
    spec2 = {"cmd": _counter_cmd(state, ok), "timeout": 5}
    supertool._validator_run_one("t", spec2, str(f))
    assert state.read_text().strip() == "1"  # adapter ran


def test_env_var_disables_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_validator_cache_path",
                        lambda k: tmp_path / "cache" / f"{k}.json")
    monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")
    f = tmp_path / "x.php"
    f.write_text("<?php\n")
    state = tmp_path / "n"
    state.write_text("0")
    ok = json.dumps({"tool": "t", "file": "x", "ok": True, "count": 0,
                     "errors": [], "duration_ms": 1}).replace("'", "'\\''")
    spec = {"cmd": _counter_cmd(state, ok), "timeout": 5}
    supertool._validator_run_one("t", spec, str(f))
    # Cache disabled → second call re-runs adapter → counter increments
    out2 = supertool._validator_run_one("t", spec, str(f))
    assert state.read_text().strip() == "2"
    # Second call hits the BROKEN branch → no output → schema error
    assert out2["ok"] is False


def test_run_one_substitutes_supertool_dir_token() -> None:
    # cmd embeds the {supertool_dir} token; subprocess emits it inside the JSON
    # `d` field. Use {python} + sys.argv pass-through so we don't have to
    # escape JSON inside a shell-quoted -c string (Windows-incompatible).
    cmd = (
        '{python} -c "import sys, json; sys.stdout.write(json.dumps({'
        "'tool':'t','file':'x','ok':True,'count':0,'errors':[],"
        "'duration_ms':1,'d':sys.argv[1]"
        '}))" "{supertool_dir}"'
    )
    spec = {"cmd": cmd, "timeout": 5}
    out = supertool._validator_run_one("t", spec, "x")
    assert out["d"] == supertool._INSTALL_DIR


def test_run_one_adds_resolved_to_when_resolve_returns_other_path(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "aTest.php"
    target.parent.mkdir()
    target.write_text("<?php\n")
    payload = {"tool": "fake", "file": str(target), "ok": True, "count": 0,
               "errors": [], "duration_ms": 1}
    spec = {
        # printf is POSIX-only; {python} writes the resolved path to stdout.
        "resolve": f'{{python}} -c "import sys; sys.stdout.write({target.as_posix()!r})"',
        "cmd": _fake_cmd(payload),
        "timeout": 5,
    }
    out = supertool._validator_run_one("fake", spec, "a.php")
    # `resolve` stdout returns as_posix; the test compares against the same
    # form to be platform-independent (str(target) uses `\` on Windows).
    assert out["resolved_to"] == target.as_posix()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render_row_ok() -> None:
    data = {"tool": "phplint", "ok": True, "count": 0, "errors": [], "duration_ms": 12}
    lines = supertool._validator_render_row(data)
    assert lines[0].startswith("phplint ")
    assert "ok" in lines[0]
    assert "(12ms)" in lines[0]


def test_render_row_shows_errors_capped_at_5() -> None:
    errors = [{"line": i, "code": "x", "msg": f"e{i}"} for i in range(1, 8)]
    data = {"tool": "t", "ok": False, "count": 7, "errors": errors, "duration_ms": 1}
    lines = supertool._validator_render_row(data)
    assert any("+2 more" in l for l in lines)
    assert sum(1 for l in lines if l.startswith("  L")) == 5


def test_render_diff_unchanged() -> None:
    before = {"tool": "t", "ok": True, "count": 0, "errors": []}
    after = {"tool": "t", "ok": True, "count": 0, "errors": [], "elapsed_s": 1.2}
    lines = supertool._validator_render_diff(before, after)
    assert "(unchanged)" in lines[0]
    assert "1.2s" in lines[0]


def test_render_diff_regression_shows_new_errors_only() -> None:
    before = {"tool": "t", "ok": False, "count": 1, "errors": [{"msg": "old"}]}
    after = {"tool": "t", "ok": False, "count": 2, "errors": [{"msg": "old"}, {"msg": "new"}], "elapsed_s": 0.5}
    lines = supertool._validator_render_diff(before, after)
    assert "1 → 2" in lines[0]
    assert "✗" in lines[0]
    assert "0.5s" in lines[0]
    assert any("new" in l for l in lines[1:])
    assert not any("old" in l for l in lines[1:])


def test_render_diff_improvement_marker() -> None:
    before = {"tool": "t", "ok": False, "count": 3, "errors": []}
    after = {"tool": "t", "ok": False, "count": 1, "errors": [], "elapsed_s": 2.0}
    lines = supertool._validator_render_diff(before, after)
    assert "⚠" in lines[0]
    assert "2.0s" in lines[0]


def test_render_diff_elapsed_missing_no_crash() -> None:
    before = {"tool": "t", "ok": True, "count": 0, "errors": []}
    after = {"tool": "t", "ok": True, "count": 0, "errors": []}
    lines = supertool._validator_render_diff(before, after)
    assert "(unchanged)" in lines[0]


def test_render_diff_skipped_shows_dash() -> None:
    after = {"tool": "mything", "skipped": "no target resolved"}
    lines = supertool._validator_render_diff(None, after)
    assert lines[0].endswith("-")


def test_render_diff_elapsed_appears_in_run_with_validators_output(tmp_path) -> None:
    import re
    f = tmp_path / "x.php"
    f.write_text("<?php\n")
    payload_ok = {"tool": "fake", "file": str(f), "ok": True, "count": 0,
                  "errors": [], "duration_ms": 1}
    _set_validators({
        "fake": {"cmd": _fake_cmd(payload_ok), "hooks_into": ["edit"], "match": "*.php"},
    })
    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], lambda: "edited\n")
    assert "[validators]" in out
    assert re.search(r"\d+\.\d+s", out)


# ---------------------------------------------------------------------------
# _run_with_validators integration
# ---------------------------------------------------------------------------

def test_run_with_validators_passthrough_when_no_config(tmp_path: Path) -> None:
    _set_validators({})
    called = []
    out = supertool._run_with_validators("edit", ["edit", "", "", str(tmp_path / "x.php")],
                                         lambda: called.append(1) or "edited\n")
    assert out == "edited\n"
    assert called == [1]


def test_run_with_validators_appends_diff_block(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    f.write_text("<?php\n")
    payload_ok = {"tool": "fake", "file": str(f), "ok": True, "count": 0,
                  "errors": [], "duration_ms": 1}
    _set_validators({
        "fake": {"cmd": _fake_cmd(payload_ok), "hooks_into": ["edit"], "match": "*.php"},
    })
    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], lambda: "edited\n")
    assert "[validators]" in out
    assert "fake" in out


def test_run_with_validators_rollback_on_regression(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    original = "<?php\n// original\n"
    f.write_text(original)

    # Adapter that always reports 1 error → simulates post-edit regression
    payload_fail = {"tool": "fake", "file": str(f), "ok": False, "count": 1,
                    "errors": [{"line": 1, "msg": "boom", "code": "x", "severity": "error"}],
                    "duration_ms": 1}
    payload_ok = {"tool": "fake", "file": str(f), "ok": True, "count": 0,
                  "errors": [], "duration_ms": 1}

    # Switch behavior: first call (pre) returns ok, second (post) returns fail.
    # Achieved by writing a counter file.
    state = tmp_path / "n"
    state.write_text("0")
    cmd = _counter_cmd(state, json.dumps(payload_ok), json.dumps(payload_fail))
    _set_validators({
        "fake": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.php",
                 "rollback_on_fail": True},
    })

    def do_edit() -> str:
        f.write_text("<?php\n// broken\n")
        return "edited\n"

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_edit)
    assert "rolled back" in out
    assert f.read_text() == original


def test_run_with_validators_skips_when_op_returns_error(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    f.write_text("<?php\n")
    _set_validators({
        "fake": {"cmd": "echo BOOM", "hooks_into": ["edit"], "match": "*.php"},
    })
    out = supertool._run_with_validators(
        "edit", ["edit", "", "", str(f)], lambda: "ERROR: nope\n"
    )
    assert out == "ERROR: nope\n"
    assert "[validators]" not in out


def test_run_with_validators_unregistered_op_passthrough(tmp_path: Path) -> None:
    _set_validators({"fake": {"cmd": "x", "hooks_into": ["grep"]}})
    out = supertool._run_with_validators("grep", ["grep", "p", "."], lambda: "result\n")
    assert out == "result\n"


# ---------------------------------------------------------------------------
# op_validate (manual one-shot)
# ---------------------------------------------------------------------------

def test_op_validate_no_validators_configured() -> None:
    _set_validators({})
    out = supertool.op_validate("x.php")
    assert "no validators" in out


def test_op_validate_runs_matching_validators() -> None:
    payload = {"tool": "fake", "file": "x.php", "ok": True, "count": 0,
               "errors": [], "duration_ms": 1}
    _set_validators({"fake": {"cmd": _fake_cmd(payload), "match": "*.php"}})
    out = supertool.op_validate("x.php")
    assert "fake" in out
    assert "ok" in out


def test_op_validate_with_tool_filter() -> None:
    payload = {"tool": "a", "file": "x.php", "ok": True, "count": 0,
               "errors": [], "duration_ms": 1}
    _set_validators({
        "a": {"cmd": _fake_cmd(payload), "match": "*.php"},
        "b": {"cmd": "echo SHOULD_NOT_RUN", "match": "*.php"},
    })
    out = supertool.op_validate("x.php", ["a"])
    assert "a " in out or "a:" in out
    assert "SHOULD_NOT_RUN" not in out


# ---------------------------------------------------------------------------
# _validator_render_row verbose mode
# ---------------------------------------------------------------------------

def test_render_row_verbose_shows_all_errors() -> None:
    errors = [{"line": i, "code": "x", "msg": f"e{i}"} for i in range(1, 8)]
    data = {"tool": "t", "ok": False, "count": 7, "errors": errors, "duration_ms": 1}
    lines = supertool._validator_render_row(data, verbose=True)
    assert not any("+2 more" in l for l in lines), "verbose must not cap errors"
    assert sum(1 for l in lines if l.startswith("  L")) == 7


def test_render_row_verbose_no_cap_marker() -> None:
    errors = [{"line": i, "code": "x", "msg": f"e{i}"} for i in range(1, 10)]
    data = {"tool": "t", "ok": False, "count": 9, "errors": errors, "duration_ms": 1}
    lines = supertool._validator_render_row(data, verbose=True)
    assert not any("more" in l for l in lines)


def test_render_row_verbose_shows_raw_stdout() -> None:
    data = {
        "tool": "t", "ok": False, "count": 1,
        "errors": [{"line": 1, "code": "x", "msg": "bad"}],
        "duration_ms": 1,
        "raw_stdout": "full adapter output line1\nline2",
    }
    lines = supertool._validator_render_row(data, verbose=True)
    assert any("[stdout]" in l for l in lines)
    assert any("full adapter output line1" in l for l in lines)
    assert any("line2" in l for l in lines)


def test_render_row_verbose_shows_raw_stderr() -> None:
    data = {
        "tool": "t", "ok": False, "count": 1,
        "errors": [{"line": 1, "code": "x", "msg": "bad"}],
        "duration_ms": 1,
        "raw_stderr": "stderr output here",
    }
    lines = supertool._validator_render_row(data, verbose=True)
    assert any("[stderr]" in l for l in lines)
    assert any("stderr output here" in l for l in lines)


def test_render_row_default_still_caps_at_5() -> None:
    errors = [{"line": i, "code": "x", "msg": f"e{i}"} for i in range(1, 8)]
    data = {"tool": "t", "ok": False, "count": 7, "errors": errors, "duration_ms": 1}
    lines = supertool._validator_render_row(data)
    assert any("+2 more" in l for l in lines)
    assert sum(1 for l in lines if l.startswith("  L")) == 5


def test_render_row_verbose_source_context_renders_under_error() -> None:
    ctx = ["40:     return foo;", "41: ", "42→     bar();", "43: }", "44: "]
    errors = [{"line": 42, "code": "E1", "msg": "bad call", "source_context": ctx}]
    data = {"tool": "t", "ok": False, "count": 1, "errors": errors, "duration_ms": 1}
    lines = supertool._validator_render_row(data, verbose=True)
    error_idx = next(i for i, l in enumerate(lines) if "bad call" in l)
    ctx_lines = lines[error_idx + 1: error_idx + 1 + len(ctx)]
    assert len(ctx_lines) == len(ctx)
    assert all(l.startswith("    ") for l in ctx_lines)
    assert "42→     bar();" in ctx_lines[2]


def test_render_row_verbose_source_context_absent_no_extra_lines() -> None:
    errors = [{"line": 1, "code": "E1", "msg": "oops"}]
    data = {"tool": "t", "ok": False, "count": 1, "errors": errors, "duration_ms": 1}
    lines = supertool._validator_render_row(data, verbose=True)
    # header + one error line only (no raw keys, no source_context)
    assert len(lines) == 2


def test_render_row_verbose_diff_renders_at_bottom() -> None:
    errors = [{"line": 1, "code": "E1", "msg": "msg"}]
    diff = "-old line\n+new line"
    data = {"tool": "t", "ok": False, "count": 1, "errors": errors, "duration_ms": 1, "diff": diff}
    lines = supertool._validator_render_row(data, verbose=True)
    assert any("[diff]" in l for l in lines)
    assert any("-old line" in l for l in lines)
    assert any("+new line" in l for l in lines)
    assert any("[/diff]" in l for l in lines)
    diff_idx = next(i for i, l in enumerate(lines) if "[diff]" in l)
    error_idx = next(i for i, l in enumerate(lines) if "msg" in l)
    assert diff_idx > error_idx


def test_render_row_verbose_diff_absent_no_diff_block() -> None:
    errors = [{"line": 1, "code": "E1", "msg": "msg"}]
    data = {"tool": "t", "ok": False, "count": 1, "errors": errors, "duration_ms": 1}
    lines = supertool._validator_render_row(data, verbose=True)
    assert not any("[diff]" in l for l in lines)


def test_render_row_non_verbose_ignores_source_context_and_diff() -> None:
    ctx = ["40:     return foo;", "42→     bar();"]
    errors = [{"line": 1, "code": "E1", "msg": "msg", "source_context": ctx}]
    diff = "-old\n+new"
    data = {"tool": "t", "ok": False, "count": 1, "errors": errors, "duration_ms": 1, "diff": diff}
    lines = supertool._validator_render_row(data, verbose=False)
    assert not any("42→" in l for l in lines)
    assert not any("[diff]" in l for l in lines)
    assert not any("-old" in l for l in lines)


# ---------------------------------------------------------------------------
# op_validate verbose mode
# ---------------------------------------------------------------------------

def test_op_validate_verbose_shows_all_errors() -> None:
    errors = [{"line": i, "col": None, "severity": "error", "code": "x", "msg": f"e{i}"}
              for i in range(1, 8)]
    payload = {"tool": "fake", "file": "x.php", "ok": False, "count": 7,
               "errors": errors, "duration_ms": 1}
    _set_validators({"fake": {"cmd": _fake_cmd(payload), "match": "*.php"}})
    out = supertool.op_validate("x.php", verbose=True)
    assert "+2 more" not in out
    for i in range(1, 8):
        assert f"e{i}" in out


def test_op_validate_non_verbose_caps_at_5() -> None:
    errors = [{"line": i, "col": None, "severity": "error", "code": "x", "msg": f"e{i}"}
              for i in range(1, 8)]
    payload = {"tool": "fake", "file": "x.php", "ok": False, "count": 7,
               "errors": errors, "duration_ms": 1}
    _set_validators({"fake": {"cmd": _fake_cmd(payload), "match": "*.php"}})
    out = supertool.op_validate("x.php", verbose=False)
    assert "+2 more" in out


# ---------------------------------------------------------------------------
# dispatch: validate verbose parsing
# ---------------------------------------------------------------------------

def test_dispatch_validate_verbose_flag() -> None:
    errors = [{"line": i, "col": None, "severity": "error", "code": "x", "msg": f"e{i}"}
              for i in range(1, 8)]
    payload = {"tool": "fake", "file": "x.php", "ok": False, "count": 7,
               "errors": errors, "duration_ms": 1}
    _set_validators({"fake": {"cmd": _fake_cmd(payload), "match": "*.php"}})
    out = supertool.dispatch("validate:x.php:verbose")
    assert "+2 more" not in out
    for i in range(1, 8):
        assert f"e{i}" in out


def test_dispatch_validate_tools_and_verbose() -> None:
    payload = {"tool": "fake", "file": "x.php", "ok": True, "count": 0,
               "errors": [], "duration_ms": 1}
    _set_validators({
        "fake": {"cmd": _fake_cmd(payload), "match": "*.php"},
        "other": {"cmd": "echo SHOULD_NOT_RUN", "match": "*.php"},
    })
    out = supertool.dispatch("validate:x.php:fake:verbose")
    assert "fake" in out
    assert "SHOULD_NOT_RUN" not in out


def test_dispatch_validate_no_verbose_flag_still_caps() -> None:
    errors = [{"line": i, "col": None, "severity": "error", "code": "x", "msg": f"e{i}"}
              for i in range(1, 8)]
    payload = {"tool": "fake", "file": "x.php", "ok": False, "count": 7,
               "errors": errors, "duration_ms": 1}
    _set_validators({"fake": {"cmd": _fake_cmd(payload), "match": "*.php"}})
    out = supertool.dispatch("validate:x.php")
    assert "+2 more" in out


# ---------------------------------------------------------------------------
# phplint.py reference adapter
# ---------------------------------------------------------------------------

PHPLINT = Path(__file__).parent.parent / "validators" / "phplint" / "phplint.py"


@pytest.mark.skipif(not shutil.which("php"), reason="php not installed")
def test_phplint_adapter_valid_php(tmp_path: Path) -> None:
    f = tmp_path / "ok.php"
    f.write_text("<?php\n$x = 1;\n")
    r = subprocess.run(["python3", str(PHPLINT), str(f)],
                       capture_output=True, text=True, timeout=10)
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phplint"
    assert data["ok"] is True
    assert data["count"] == 0


@pytest.mark.skipif(not shutil.which("php"), reason="php not installed")
def test_phplint_adapter_broken_php_reports_line(tmp_path: Path) -> None:
    f = tmp_path / "bad.php"
    f.write_text("<?php\nfunction broken( {\n")
    r = subprocess.run(["python3", str(PHPLINT), str(f)],
                       capture_output=True, text=True, timeout=10)
    data = json.loads(r.stdout.strip())
    assert data["ok"] is False
    assert data["count"] == 1
    assert data["errors"][0]["line"] == 2


def test_phplint_adapter_no_arg_returns_schema_error() -> None:
    r = subprocess.run(["python3", str(PHPLINT)],
                       capture_output=True, text=True, timeout=5)
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phplint"
    assert data["ok"] is False
    assert "no file arg" in data["errors"][0]["msg"]


# ---------------------------------------------------------------------------
# _advise_new_test — paste-time "write a test" advisory
# ---------------------------------------------------------------------------

# resolve cmd that signals a miss: exit 3 + would-be target on stderr.
_RESOLVE_MISS = (
    '{python} -c "import sys; '
    "sys.stderr.write('tests/unit/SiX/FooTest.php'); sys.exit(3)\""
)
# resolve cmd that signals a hit: exit 0 + test path on stdout.
_RESOLVE_HIT = '{python} -c "import sys; sys.stdout.write(\'tests/unit/SiX/FooTest.php\')"'


def _set_advice_config(enabled: bool, resolve: str | None = _RESOLVE_MISS) -> None:
    validators: dict = {}
    if resolve is not None:
        validators["phpunit"] = {"cmd": "noop", "resolve": resolve}
    supertool._CONFIG = {"adviseForNewTest": enabled, "validators": validators}
    supertool._CONFIG_CHECKED = True


def test_advise_emitted_for_new_class_without_test() -> None:
    _set_advice_config(True)
    out = supertool._advise_new_test("paste", "Dvsi/.../Foo.class.php", pre_existed=False)
    assert "[advice]" in out
    assert "new class without test" in out
    assert "tests/unit/SiX/FooTest.php" in out


def test_advise_silent_when_flag_disabled() -> None:
    _set_advice_config(False)
    assert supertool._advise_new_test("paste", "Foo.class.php", pre_existed=False) == ""


def test_advise_silent_when_file_already_existed() -> None:
    _set_advice_config(True)
    assert supertool._advise_new_test("paste", "Foo.class.php", pre_existed=True) == ""


def test_advise_silent_for_non_paste_op() -> None:
    _set_advice_config(True)
    assert supertool._advise_new_test("edit", "Foo.class.php", pre_existed=False) == ""


def test_advise_silent_for_non_php_file() -> None:
    _set_advice_config(True)
    assert supertool._advise_new_test("paste", "styles.scss", pre_existed=False) == ""


def test_advise_silent_when_test_exists() -> None:
    _set_advice_config(True, resolve=_RESOLVE_HIT)
    assert supertool._advise_new_test("paste", "Foo.class.php", pre_existed=False) == ""


def test_advise_silent_when_no_resolve_cmd() -> None:
    _set_advice_config(True, resolve=None)
    assert supertool._advise_new_test("paste", "Foo.class.php", pre_existed=False) == ""


# ---------------------------------------------------------------------------
# Integration: advisory driven by a real external resolver script
# (a resolve_test.sh equivalent) — proves the subprocess + exit-3 + stderr
# contract end-to-end, not just inline {python} -c stand-ins.
# ---------------------------------------------------------------------------

def _write_resolver_script(tmp_path: Path) -> Path:
    """A cross-platform resolve_test.sh equivalent.

    Mirrors the real script's contract against the actual filesystem:
      already a test (*Test.php)  -> echo it on stdout, exit 0
      mirror test exists on disk  -> echo it on stdout, exit 0
      no test                     -> mirror target on stderr, exit 3
    Mirror rule: /src/ -> /tests/, Foo.php -> FooTest.php.
    """
    script = tmp_path / "resolve_test_equiv.py"
    script.write_text(
        "import os, sys\n"
        "src = sys.argv[1]\n"
        "if src.endswith('Test.php'):\n"
        "    sys.stdout.write(src); sys.exit(0)\n"
        "mirror = src.replace('/src/', '/tests/')\n"
        "if mirror.endswith('.php'):\n"
        "    mirror = mirror[:-4] + 'Test.php'\n"
        "if os.path.isfile(mirror):\n"
        "    sys.stdout.write(mirror); sys.exit(0)\n"
        "sys.stderr.write(mirror); sys.exit(3)\n"
    )
    return script


def _set_advice_config_with_script(script: Path) -> None:
    resolve = f'{{python}} {script.as_posix()} {{file}}'
    supertool._CONFIG = {
        "adviseForNewTest": True,
        "validators": {"phpunit": {"cmd": "noop", "resolve": resolve}},
    }
    supertool._CONFIG_CHECKED = True


def test_advise_real_script_emits_when_no_test_on_disk(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "Mod"
    src_dir.mkdir(parents=True)
    src = src_dir / "Foo.php"
    src.write_text("<?php\nclass Foo {}\n")
    _set_advice_config_with_script(_write_resolver_script(tmp_path))

    out = supertool._advise_new_test("paste", str(src), pre_existed=False)
    assert "[advice]" in out
    # The would-be target the real script wrote to stderr (exit 3).
    expected = str(src).replace("/src/", "/tests/")[:-4] + "Test.php"
    assert expected in out


def test_advise_real_script_silent_when_test_on_disk(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "Mod"
    src_dir.mkdir(parents=True)
    src = src_dir / "Foo.php"
    src.write_text("<?php\nclass Foo {}\n")
    # Create the mirror test so the script takes the hit branch (exit 0 + stdout).
    test_dir = tmp_path / "tests" / "Mod"
    test_dir.mkdir(parents=True)
    (test_dir / "FooTest.php").write_text("<?php\nclass FooTest {}\n")
    _set_advice_config_with_script(_write_resolver_script(tmp_path))

    assert supertool._advise_new_test("paste", str(src), pre_existed=False) == ""
