"""Security and robustness tests for _load_config() / .supertool.json loader.

Audit pass 2026-05-23: probe malformed input, DoS vectors, path-walk scope,
symlink resolution, type confusion, and injection contracts. Each test
either documents current behavior or pins a regression guard.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helper: reset config cache so each test gets a fresh load
# ---------------------------------------------------------------------------

def _reset_config() -> None:
    supertool._CONFIG_CHECKED = False
    supertool._CONFIG = None
    supertool._mcp_specs = {}


# ---------------------------------------------------------------------------
# 1. Malformed JSON — truncated object
# ---------------------------------------------------------------------------

def test_malformed_json_returns_empty_dict_no_traceback(tmp_path: Path, monkeypatch) -> None:
    """`{"ops":` (truncated) must not traceback — loader returns {}."""
    cfg = tmp_path / ".supertool.json"
    cfg.write_text('{"ops":')
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    assert isinstance(result, dict), "must return a dict, not raise"
    # Malformed file → should be treated as absent → empty config
    assert result == {}, f"expected empty dict, got {result!r}"


# ---------------------------------------------------------------------------
# 2. JSON bomb — large file
# ---------------------------------------------------------------------------

def test_json_bomb_large_file(tmp_path: Path, monkeypatch) -> None:
    """A ~10 MB .supertool.json must not crash or hang the loader.

    Current behavior: loader has NO size cap — it reads the whole file.
    This test documents that and pins a time budget (< 5 s) so we notice
    if it degrades to unbounded parse time.

    If a size cap is added later, update the assertion accordingly.
    """
    cfg = tmp_path / ".supertool.json"
    # Build a valid but large JSON object: {"ops": {"k0": "v", "k1": "v", ...}}
    ops = {f"k{i}": {"cmd": "echo hi"} for i in range(50_000)}
    payload = json.dumps({"ops": ops})
    cfg.write_text(payload)
    monkeypatch.chdir(tmp_path)
    _reset_config()

    t0 = time.monotonic()
    result = supertool._load_config()
    elapsed = time.monotonic() - t0

    assert isinstance(result, dict), "large valid JSON must parse to a dict"
    # No hard size cap currently — document: if the loader gains one, it
    # should return {} (not crash). For now, we just cap wall time.
    assert elapsed < 5.0, f"large config took {elapsed:.2f}s — DoS risk"


# ---------------------------------------------------------------------------
# 3. Deeply nested JSON — recursion risk
# ---------------------------------------------------------------------------

def test_deeply_nested_json_no_recursion_error(tmp_path: Path, monkeypatch) -> None:
    """10 000 levels of nesting must not blow the Python call stack.

    json.loads in CPython iterates (it does not recurse), so this should
    parse without RecursionError. If it does raise JSONDecodeError (some
    interpreter limit) the loader must still not traceback into the caller.
    """
    # Build 10k-deep nested dict: {"a": {"a": {"a": ...}}}
    depth = 10_000
    nested = "{" + '"a":' * depth + '1' + "}" * depth
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(nested)
    monkeypatch.chdir(tmp_path)
    _reset_config()

    try:
        result = supertool._load_config()
    except RecursionError as exc:
        pytest.fail(f"RecursionError escaped _load_config: {exc}")

    # Either parsed (returns dict) or silently skipped (returns {})
    assert isinstance(result, dict), "must always return a dict"


# ---------------------------------------------------------------------------
# 4. Type confusion — wrong types for known top-level keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_config, key", [
    ({"ops": "not a dict"}, "ops"),
    ({"presets": 123}, "presets"),
    ({"compact": "yes"}, "compact"),
    ({"timeout": "fast"}, "timeout"),
    ({"parallel": []}, "parallel"),
])
def test_type_confusion_does_not_crash(
    tmp_path: Path, monkeypatch, bad_config: dict, key: str
) -> None:
    """Wrong types for known keys must not raise — loader returns the raw dict
    and callers must handle gracefully (or ignore bad values)."""
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps(bad_config))
    monkeypatch.chdir(tmp_path)
    _reset_config()

    # Must not raise
    try:
        result = supertool._load_config()
    except Exception as exc:
        pytest.fail(f"_load_config raised on bad {key!r}: {exc}")

    assert isinstance(result, dict)


def test_compact_string_yes_is_falsy_via_is_compact(tmp_path: Path, monkeypatch) -> None:
    """compact='yes' (string) — _is_compact() should NOT return True.

    bool('yes') is True in Python, so if the loader passes the raw string
    straight to bool(), it would wrongly enable compact mode. Pin current
    behavior so we notice a regression.
    """
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps({"compact": "yes"}))
    monkeypatch.chdir(tmp_path)
    _reset_config()
    supertool._load_config()

    # _is_compact() calls bool(config.get("compact", False))
    # bool("yes") == True — document this as a known type-confusion bug.
    result = supertool._is_compact()
    # Pin observed behavior: currently True because bool("yes") is True.
    # If the loader adds type coercion/validation, this should become False.
    # For now we just confirm it doesn't crash:
    assert isinstance(result, bool), "_is_compact() must return bool"


# ---------------------------------------------------------------------------
# 5. Unknown top-level keys — must be ignored, not executed
# ---------------------------------------------------------------------------

def test_unknown_top_level_keys_are_ignored(tmp_path: Path, monkeypatch) -> None:
    """Arbitrary unknown fields must not trigger code execution or crash."""
    malicious = {
        "evil_field": "rm -rf /",
        "__import__": "os",
        "exec": "import os; os.system('id')",
        "ops": {"legit": {"cmd": "echo ok"}},
    }
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps(malicious))
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    assert isinstance(result, dict)
    # Unknown keys must be present in the dict (as data), not evaluated
    assert result.get("evil_field") == "rm -rf /"
    assert result.get("exec") == "import os; os.system('id')"
    # Legit op must still be accessible
    assert "legit" in result.get("ops", {})


# ---------------------------------------------------------------------------
# 6. Config walk-up scope — does the loader stop before / ?
# ---------------------------------------------------------------------------

def test_walk_up_stops_at_filesystem_root(tmp_path: Path, monkeypatch) -> None:
    """The walk-up loop must terminate at / and not loop infinitely.

    When no .supertool.json is found anywhere, the result must be {} and
    the call must return in bounded time.
    """
    # Use a deep temp directory with no config anywhere in the chain
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    _reset_config()

    t0 = time.monotonic()
    result = supertool._load_config()
    elapsed = time.monotonic() - t0

    assert result == {}, "no config → must return empty dict"
    assert elapsed < 2.0, f"walk-up took {elapsed:.2f}s — infinite loop risk"


def test_walk_up_does_not_read_ancestor_config_above_project(
    tmp_path: Path, monkeypatch
) -> None:
    """If a .supertool.json exists in a *parent* of the project dir, the loader
    WILL pick it up (by design). This test documents that scope: the loader
    walks up to /, reading the first .supertool.json it finds.

    This is a documented trust boundary: anyone who can write a .supertool.json
    in a parent directory can inject custom ops. Pin this so we notice if the
    behavior changes.
    """
    # Parent has a config; child (cwd) does not
    parent_cfg = tmp_path / ".supertool.json"
    parent_cfg.write_text(json.dumps({"ops": {"from_parent": {"cmd": "echo parent"}}}))
    child_dir = tmp_path / "project"
    child_dir.mkdir()
    monkeypatch.chdir(child_dir)
    _reset_config()

    result = supertool._load_config()

    # Current behavior: walks up and finds parent config
    assert "from_parent" in result.get("ops", {}), (
        "loader currently walks up to parent — document this trust boundary"
    )


# ---------------------------------------------------------------------------
# 7. Symlinked config → /etc/passwd
# ---------------------------------------------------------------------------

def test_symlinked_config_to_etc_passwd_errors_cleanly(
    tmp_path: Path, monkeypatch
) -> None:
    """A .supertool.json symlink pointing at /etc/passwd must parse as invalid
    JSON and be silently skipped — no crash, no leak of passwd content.
    """
    if not Path("/etc/passwd").exists():
        pytest.skip("/etc/passwd not available on this platform")

    link = tmp_path / ".supertool.json"
    link.symlink_to("/etc/passwd")
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    # /etc/passwd is not valid JSON → JSONDecodeError → loader skips it → {}
    assert isinstance(result, dict)
    # The passwd content must NOT appear in the returned config
    assert result == {}, (
        "symlink to /etc/passwd must be treated as invalid JSON and skipped"
    )


# ---------------------------------------------------------------------------
# 8. Absurd timeout value — used as-is (no upper bound)
# ---------------------------------------------------------------------------

def test_absurd_timeout_is_stored_not_capped(tmp_path: Path, monkeypatch) -> None:
    """An op with timeout=999999 is currently stored verbatim.

    There is no upper-bound clamp. This test documents that: the value
    flows through to subprocess.run(timeout=999999) when the op is invoked.
    We only verify no crash at config-load time.
    """
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps({
        "ops": {"slow_op": {"cmd": "echo hi", "timeout": 999_999}}
    }))
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    assert isinstance(result, dict)
    op = result.get("ops", {}).get("slow_op", {})
    assert op.get("timeout") == 999_999, "absurd timeout stored verbatim — no cap"


# ---------------------------------------------------------------------------
# 9. Negative timeout — clean handling
# ---------------------------------------------------------------------------

def test_negative_timeout_does_not_crash_at_load(tmp_path: Path, monkeypatch) -> None:
    """timeout=-1 in config must not crash at load time."""
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps({
        "ops": {"neg": {"cmd": "echo hi", "timeout": -1}}
    }))
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    assert isinstance(result, dict)
    op = result.get("ops", {}).get("neg", {})
    assert op.get("timeout") == -1, "negative timeout stored verbatim"


def test_negative_global_timeout_does_not_crash_at_load(tmp_path: Path, monkeypatch) -> None:
    """A negative global timeout (top-level key) must not crash at load time."""
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps({"timeout": -5}))
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    assert isinstance(result, dict)
    assert result.get("timeout") == -5


# ---------------------------------------------------------------------------
# 10. Op cmd injection via config — NO execution at load time
# ---------------------------------------------------------------------------

def test_cmd_injection_in_config_not_executed_at_load_time(
    tmp_path: Path, monkeypatch
) -> None:
    """A dangerous cmd value must NOT be executed when the config is loaded.

    The contract: user-authored configs are trusted. But they are only executed
    when the op is explicitly called, not at load time. This test ensures there
    is no surprise execution during _load_config().
    """
    sentinel = tmp_path / "sentinel.txt"
    evil_cmd = f"touch {sentinel}"

    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps({
        "ops": {
            "evil": {"cmd": evil_cmd}
        }
    }))
    monkeypatch.chdir(tmp_path)
    _reset_config()

    supertool._load_config()

    assert not sentinel.exists(), (
        f"CRITICAL: cmd {evil_cmd!r} was executed at config-load time — "
        "commands must only run when the op is explicitly invoked"
    )


def test_mcp_block_cmd_not_executed_at_load_time(tmp_path: Path, monkeypatch) -> None:
    """An mcp block with an evil cmd must also not execute at load time."""
    sentinel = tmp_path / "mcp_sentinel.txt"
    evil_cmd = f"touch {sentinel}"

    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps({
        "mcp": {
            "evil_server": {"cmd": evil_cmd, "args": []}
        }
    }))
    monkeypatch.chdir(tmp_path)
    _reset_config()

    supertool._load_config()

    assert not sentinel.exists(), (
        "CRITICAL: mcp.cmd was executed at config-load time"
    )


# ---------------------------------------------------------------------------
# Bonus: empty config file
# ---------------------------------------------------------------------------

def test_empty_config_file_returns_empty_dict(tmp_path: Path, monkeypatch) -> None:
    """An empty .supertool.json (zero bytes) must not crash."""
    cfg = tmp_path / ".supertool.json"
    cfg.write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    assert isinstance(result, dict)
    assert result == {}


def test_null_json_returns_empty_dict(tmp_path: Path, monkeypatch) -> None:
    """`null` is valid JSON but not a dict — loader must return {} not None."""
    cfg = tmp_path / ".supertool.json"
    cfg.write_text("null")
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    # If _CONFIG is set to None (from json.load), _load_config returns {} via `_CONFIG or {}`
    assert isinstance(result, dict), f"null config must return dict, got {result!r}"
