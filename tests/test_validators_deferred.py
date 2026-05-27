"""Tests for deferred-validator behavior — issue #219.

Validators with tier="slow" queue as (name, path) pairs instead of running
per-op. main() drains once after all ops, deduped by (name, path).
"""
from __future__ import annotations

import sys
from pathlib import Path

import supertool


def _make_counting_validator(tmp_path: Path, *, ok: bool = True) -> tuple[str, Path]:
    """Build a validator cmd that appends a line to a counter file each call.

    Emits minimal SCHEMA.md JSON on stdout. Uses sys.executable -c style (via
    helper file) to avoid quoting/escaping issues cross-platform.
    """
    counter = tmp_path / "val_runs.log"
    counter.write_text("")
    helper = tmp_path / "_val_helper.py"
    if ok:
        result_json = '{"tool": "fakeval", "file": "x", "ok": true, "count": 0, "errors": [], "duration_ms": 1}'
    else:
        result_json = '{"tool": "fakeval", "file": "x", "ok": false, "count": 1, "errors": [{"line": null, "col": null, "severity": "error", "code": "e", "msg": "fail"}], "duration_ms": 1}'
    helper.write_text(
        "import sys\n"
        f"open({str(counter)!r}, 'a').write('run\\n')\n"
        f"print({result_json!r})\n"
    )
    exe = sys.executable.replace("\\", "/")
    helper_fwd = str(helper).replace("\\", "/")
    return f"{exe} {helper_fwd} {{file}}", counter


def _cfg(tmp_path: Path, validators: dict) -> None:
    supertool._CONFIG = {"validators": validators}
    supertool._CONFIG_CHECKED = True


def test_slow_validator_does_not_run_per_op_runs_once_at_end(tmp_path, monkeypatch) -> None:
    target = tmp_path / "a.php"
    target.write_text("<?php $x = 1;\n")
    cmd, counter = _make_counting_validator(tmp_path)
    _cfg(tmp_path, {"fakeval": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.php",
                                "tier": "slow", "cache": False}})

    monkeypatch.chdir(tmp_path)
    rc = supertool.main([
        f"edit:::$x = 1:::$x = 2:::{target}",
        f"edit:::$x = 2:::$x = 3:::{target}",
    ])
    assert rc == 0
    runs = counter.read_text().count("run\n")
    assert runs == 1, f"expected 1 deferred run, got {runs}"


def test_fast_validator_runs_per_op(tmp_path, monkeypatch) -> None:
    target = tmp_path / "a.php"
    target.write_text("<?php $x = 1;\n")
    cmd, counter = _make_counting_validator(tmp_path)
    _cfg(tmp_path, {"fakeval": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.php",
                                "tier": "fast", "cache": False}})

    monkeypatch.chdir(tmp_path)
    supertool.main([
        f"edit:::$x = 1:::$x = 2:::{target}",
        f"edit:::$x = 2:::$x = 3:::{target}",
    ])
    runs = counter.read_text().count("run\n")
    # before + after per op = 4 total for 2 ops (before-op1, after-op1, before-op2, after-op2)
    assert runs == 4, f"fast validator runs before+after per op (4 for 2 ops), got {runs}"


def test_default_tier_behaves_as_fast(tmp_path, monkeypatch) -> None:
    target = tmp_path / "a.php"
    target.write_text("<?php $x = 1;\n")
    cmd, counter = _make_counting_validator(tmp_path)
    _cfg(tmp_path, {"fakeval": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.php",
                                "cache": False}})

    monkeypatch.chdir(tmp_path)
    supertool.main([
        f"edit:::$x = 1:::$x = 2:::{target}",
        f"edit:::$x = 2:::$x = 3:::{target}",
    ])
    runs = counter.read_text().count("run\n")
    assert runs == 4, f"default tier must match fast behavior (4 runs for 2 ops), got {runs}"


def test_dedup_three_edits_same_file_runs_slow_once(tmp_path, monkeypatch) -> None:
    target = tmp_path / "a.php"
    target.write_text("<?php $x = 1;\n")
    cmd, counter = _make_counting_validator(tmp_path)
    _cfg(tmp_path, {"fakeval": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.php",
                                "tier": "slow", "cache": False}})

    monkeypatch.chdir(tmp_path)
    supertool.main([
        f"edit:::$x = 1:::$x = 2:::{target}",
        f"edit:::$x = 2:::$x = 3:::{target}",
        f"edit:::$x = 3:::$x = 4:::{target}",
    ])
    runs = counter.read_text().count("run\n")
    assert runs == 1, f"3 edits on same file → 1 slow run (dedup), got {runs}"


def test_multi_file_slow_validator_runs_per_file(tmp_path, monkeypatch) -> None:
    a = tmp_path / "a.php"
    b = tmp_path / "b.php"
    a.write_text("<?php $x = 1;\n")
    b.write_text("<?php $y = 1;\n")
    cmd, counter = _make_counting_validator(tmp_path)
    _cfg(tmp_path, {"fakeval": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.php",
                                "tier": "slow", "cache": False}})

    monkeypatch.chdir(tmp_path)
    supertool.main([
        f"edit:::$x = 1:::$x = 2:::{a}",
        f"edit:::$y = 1:::$y = 2:::{b}",
        f"edit:::$x = 2:::$x = 3:::{a}",
    ])
    runs = counter.read_text().count("run\n")
    assert runs == 2, f"two distinct files → 2 slow runs (one per file), got {runs}"


def test_mixed_fast_and_slow_on_same_op(tmp_path, monkeypatch) -> None:
    target = tmp_path / "a.php"
    target.write_text("<?php $x = 1;\n")

    fast_counter = tmp_path / "fast_runs.log"
    fast_counter.write_text("")
    slow_counter = tmp_path / "slow_runs.log"
    slow_counter.write_text("")

    exe = sys.executable.replace("\\", "/")
    fast_result = '{"tool": "fastval", "file": "x", "ok": true, "count": 0, "errors": [], "duration_ms": 1}'
    slow_result = '{"tool": "slowval", "file": "x", "ok": true, "count": 0, "errors": [], "duration_ms": 1}'

    fast_helper = tmp_path / "_fast.py"
    fast_helper.write_text(
        "import sys\n"
        f"open({str(fast_counter)!r}, 'a').write('run\\n')\n"
        f"print({fast_result!r})\n"
    )

    slow_helper = tmp_path / "_slow.py"
    slow_helper.write_text(
        "import sys\n"
        f"open({str(slow_counter)!r}, 'a').write('run\\n')\n"
        f"print({slow_result!r})\n"
    )

    fast_fwd = str(fast_helper).replace("\\", "/")
    slow_fwd = str(slow_helper).replace("\\", "/")

    supertool._CONFIG = {"validators": {
        "fastval": {"cmd": f"{exe} {fast_fwd} {{file}}", "hooks_into": ["edit"],
                    "match": "*.php", "tier": "fast", "cache": False},
        "slowval": {"cmd": f"{exe} {slow_fwd} {{file}}", "hooks_into": ["edit"],
                    "match": "*.php", "tier": "slow", "cache": False},
    }}
    supertool._CONFIG_CHECKED = True

    monkeypatch.chdir(tmp_path)
    supertool.main([
        f"edit:::$x = 1:::$x = 2:::{target}",
        f"edit:::$x = 2:::$x = 3:::{target}",
    ])

    fast_runs = fast_counter.read_text().count("run\n")
    slow_runs = slow_counter.read_text().count("run\n")
    # fast: before+after per op = 4 for 2 ops
    assert fast_runs == 4, f"fast validator runs before+after per op (4), got {fast_runs}"
    assert slow_runs == 1, f"slow validator deferred+deduped = 1 run, got {slow_runs}"


def test_deferred_failure_does_not_rollback_prior_fast_passed_ops(tmp_path, monkeypatch, capsys) -> None:
    """Slow validator fails at end-of-call → output contains failure, file not rolled back."""
    target = tmp_path / "a.php"
    target.write_text("<?php $x = 1;\n")
    cmd, counter = _make_counting_validator(tmp_path, ok=False)
    _cfg(tmp_path, {"fakeval": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.php",
                                "tier": "slow", "rollback_on_fail": False, "cache": False}})

    monkeypatch.chdir(tmp_path)
    supertool.main([
        f"edit:::$x = 1:::$x = 2:::{target}",
        f"edit:::$x = 2:::$x = 3:::{target}",
    ])
    # Edits landed — no rollback
    assert target.read_text() == "<?php $x = 3;\n"
    # Deferred validator ran once
    assert counter.read_text().count("run\n") == 1
    # Output contains the deferred block
    captured = capsys.readouterr()
    assert "[validators-deferred]" in captured.out


def test_deferred_output_appears_under_validators_deferred_header(tmp_path, monkeypatch, capsys) -> None:
    target = tmp_path / "a.php"
    target.write_text("<?php $x = 1;\n")
    cmd, counter = _make_counting_validator(tmp_path)
    _cfg(tmp_path, {"fakeval": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.php",
                                "tier": "slow", "cache": False}})

    monkeypatch.chdir(tmp_path)
    supertool.main([
        f"edit:::$x = 1:::$x = 2:::{target}",
        f"edit:::$x = 2:::$x = 3:::{target}",
    ])
    captured = capsys.readouterr()
    assert "[validators-deferred]" in captured.out


def test_defer_state_reset_between_invocations(tmp_path, monkeypatch) -> None:
    """Module-level queue must not leak across main() calls."""
    target = tmp_path / "c.php"
    target.write_text("<?php $x = 1;\n")
    cmd, counter = _make_counting_validator(tmp_path)
    _cfg(tmp_path, {"fakeval": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.php",
                                "tier": "slow", "cache": False}})

    monkeypatch.chdir(tmp_path)
    supertool.main([f"edit:::$x = 1:::$x = 2:::{target}", f"edit:::$x = 2:::$x = 3:::{target}"])
    assert supertool._VALIDATOR_DEFER_QUEUE == []
    assert supertool._VALIDATOR_DEFER_SEEN == set()


def test_single_op_slow_validator_runs_inline(tmp_path, monkeypatch) -> None:
    """Single-op call: no defer mode — slow validator runs inline like fast (before + after)."""
    target = tmp_path / "a.php"
    target.write_text("<?php $x = 1;\n")
    cmd, counter = _make_counting_validator(tmp_path)
    _cfg(tmp_path, {"fakeval": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.php",
                                "tier": "slow", "cache": False}})

    monkeypatch.chdir(tmp_path)
    supertool.main([f"edit:::$x = 1:::$x = 2:::{target}"])
    runs = counter.read_text().count("run\n")
    # Single op = no defer mode, slow treated as fast: before + after = 2 runs
    assert runs == 2, f"single-op: slow runs inline (before+after = 2), got {runs}"
