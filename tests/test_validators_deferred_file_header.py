"""Issue #234 — `[validators-deferred]` output must show which file each
validator row belongs to. Mirrors the formatter-deferred grouping."""
from __future__ import annotations

from typing import Any, Dict

import supertool


def _stub_runner(monkeypatch) -> None:
    """Stub _validator_run_one so we don't shell out to a real validator."""
    def fake(name: str, spec: Dict[str, Any], path: str) -> Dict[str, Any]:
        return {
            "tool": name,
            "file": path,
            "ok": False,
            "count": 1,
            "errors": [{"line": 1, "col": 1, "severity": "error",
                        "code": "E1", "msg": f"err in {path}"}],
            "duration_ms": 1,
            "elapsed_s": 0.01,
        }
    monkeypatch.setattr(supertool, "_validator_run_one", fake)


def _reset_queue() -> None:
    supertool._VALIDATOR_DEFER_QUEUE.clear()
    supertool._VALIDATOR_DEFER_SEEN.clear()


def test_deferred_output_groups_by_path_with_header(monkeypatch) -> None:
    """Two files, same slow validator — output must include both paths as headers."""
    _reset_queue()
    _stub_runner(monkeypatch)
    spec = {"cmd": "irrelevant", "match": "*.php", "tier": "slow"}
    supertool._VALIDATOR_DEFER_QUEUE.extend([
        ("phpstan", spec, "src/A.php"),
        ("phpstan", spec, "src/B.php"),
    ])

    out = supertool._drain_validator_queue()

    assert "[validators-deferred]" in out
    assert "src/A.php" in out, f"path header for src/A.php missing:\n{out}"
    assert "src/B.php" in out, f"path header for src/B.php missing:\n{out}"
    # path headers must appear BEFORE the validator row for that file
    a_idx = out.index("src/A.php")
    b_idx = out.index("src/B.php")
    a_err = out.index("err in src/A.php")
    b_err = out.index("err in src/B.php")
    assert a_idx < a_err
    assert b_idx < b_err


def test_deferred_output_single_file_still_has_header(monkeypatch) -> None:
    """One file should still emit its path header for consistency."""
    _reset_queue()
    _stub_runner(monkeypatch)
    spec = {"cmd": "irrelevant", "match": "*.php", "tier": "slow"}
    supertool._VALIDATOR_DEFER_QUEUE.append(("phpstan", spec, "src/Only.php"))

    out = supertool._drain_validator_queue()

    assert "[validators-deferred]" in out
    assert "src/Only.php" in out


def test_deferred_output_empty_queue_returns_empty(monkeypatch) -> None:
    """No queued validators → empty string (no spurious header)."""
    _reset_queue()
    out = supertool._drain_validator_queue()
    assert out == ""


def test_deferred_output_multiple_validators_same_file_grouped(monkeypatch) -> None:
    """Two slow validators on one path — both rows under one path header."""
    _reset_queue()
    _stub_runner(monkeypatch)
    spec_a = {"cmd": "a", "match": "*.php", "tier": "slow"}
    spec_b = {"cmd": "b", "match": "*.php", "tier": "slow"}
    supertool._VALIDATOR_DEFER_QUEUE.extend([
        ("phpstan", spec_a, "src/X.php"),
        ("phpmd", spec_b, "src/X.php"),
    ])

    out = supertool._drain_validator_queue()

    # path header (2-space indent + path) appears once, both validator rows underneath
    assert out.count("  src/X.php\n") == 1, f"path header should appear once:\n{out}"
    assert "phpstan" in out and "phpmd" in out
