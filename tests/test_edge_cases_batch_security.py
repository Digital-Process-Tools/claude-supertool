"""Batch op edge-case and security audit.

Covers: DoS (huge batch), recursive batch, same-path race, mixed ops,
malformed items, continue_on_error semantics, unknown op, NUL byte injection,
and shell-expansion-looking paths.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(tmp_path: Path, name: str, payload) -> Path:
    f = tmp_path / name
    f.write_text(json.dumps(payload))
    return f


# ---------------------------------------------------------------------------
# 1. Huge batch (10 000 ops) — DoS via memory / CPU
# ---------------------------------------------------------------------------

class TestHugeBatch:
    """MAX_BATCH_OPS cap enforced — prevents DoS via huge payload."""

    def test_huge_batch_rejected_above_cap(self, tmp_path: Path) -> None:
        """A batch exceeding MAX_BATCH_OPS returns a clean ERROR — no OOM,
        no hang. Replaces the pre-cap 30-second perf budget."""
        target = tmp_path / "x.txt"
        target.write_text("hi\n")
        ops = [{"op": "read", "path": str(target)}] * (supertool.MAX_BATCH_OPS + 1)
        spec = _write_json(tmp_path, "ops.json", ops)

        start = time.monotonic()
        out = supertool.dispatch(f"batch:@{spec}")
        elapsed = time.monotonic() - start

        assert "ERROR" in out
        assert "max_ops cap" in out
        # Cap rejection is O(1) — must be near-instant.
        assert elapsed < 5, f"Cap rejection took {elapsed:.1f}s — should be near-instant"

    def test_batch_at_cap_runs_to_completion(self, tmp_path: Path) -> None:
        """A batch exactly at MAX_BATCH_OPS runs without rejection (cap is exclusive
        of the threshold)."""
        target = tmp_path / "x.txt"
        target.write_text("hi\n")
        ops = [{"op": "read", "path": str(target)}] * supertool.MAX_BATCH_OPS
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        # All ops dispatched (no cap rejection)
        assert "max_ops cap" not in out
        assert out.count("--- read:") == supertool.MAX_BATCH_OPS


# ---------------------------------------------------------------------------
# 2. Recursive batch — batch op inside a batch payload
# ---------------------------------------------------------------------------

class TestRecursiveBatch:
    def test_recursive_batch_does_not_infinite_loop(self, tmp_path: Path) -> None:
        """A batch whose op is 'batch' referencing another file must not
        infinite-loop. Either it works (nested batch is supported) or it errors
        cleanly within a finite time.

        BUG (medium severity): no recursion guard on the batch op — a
        self-referencing payload will recurse until Python hits its default
        recursion limit (~1000 frames) and raises RecursionError, which is
        NOT caught and leaks as an unhandled exception.
        """
        inner_target = tmp_path / "inner.txt"
        inner_target.write_text("inner_content\n")
        inner_ops = [{"op": "read", "path": str(inner_target)}]
        inner_spec = _write_json(tmp_path, "inner.json", inner_ops)

        outer_ops = [{"op": "batch", "file": f"@{inner_spec}"}]
        outer_spec = _write_json(tmp_path, "outer.json", outer_ops)

        start = time.monotonic()
        try:
            out = supertool.dispatch(f"batch:@{outer_spec}")
        except RecursionError:
            elapsed = time.monotonic() - start
            pytest.fail(
                f"RecursionError raised after {elapsed:.2f}s — batch has no "
                "recursion guard. Fix: track dispatch depth and return ERROR "
                "when batch tries to dispatch another batch."
            )
        elapsed = time.monotonic() - start
        assert elapsed < 10, f"Recursive batch hung for {elapsed:.1f}s"
        assert isinstance(out, str)

    def test_self_referencing_batch_does_not_loop(self, tmp_path: Path) -> None:
        """A batch file that references itself must terminate cleanly."""
        spec = tmp_path / "self.json"
        # Write a placeholder first so the path is valid, then overwrite
        self_ops = [{"op": "batch", "file": f"@{spec}"}]
        spec.write_text(json.dumps(self_ops))

        start = time.monotonic()
        try:
            out = supertool.dispatch(f"batch:@{spec}")
        except RecursionError:
            elapsed = time.monotonic() - start
            pytest.fail(
                f"Self-referencing batch caused RecursionError after {elapsed:.2f}s "
                "— no recursion depth guard on batch dispatch."
            )
        elapsed = time.monotonic() - start
        assert elapsed < 10, f"Self-referencing batch hung for {elapsed:.1f}s"
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# 3. Race on same path — two edits targeting the same file
# ---------------------------------------------------------------------------

class TestSamePathEdits:
    def test_two_sequential_edits_same_path_predictable(self, tmp_path: Path) -> None:
        """Two edits on the same file in one batch must execute sequentially
        (batch is currently single-threaded), so the second op sees the result
        of the first.

        This documents CORRECT behavior: batch is not parallel, so no race.
        If parallelism is ever added, this test becomes a canary.
        """
        target = tmp_path / "x.txt"
        target.write_text("a\n")
        ops = [
            {"op": "edit", "path": str(target), "old": "a", "new": "b"},
            {"op": "edit", "path": str(target), "old": "b", "new": "c"},
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")

        # Both edits should succeed sequentially
        assert "ERROR" not in out, f"Unexpected error: {out}"
        final = target.read_text()
        assert final == "c\n", (
            f"Expected 'c\\n' after two sequential edits — got {final!r}. "
            "If batch is parallelised, this is a data race."
        )

    def test_two_conflicting_edits_second_fails_gracefully(self, tmp_path: Path) -> None:
        """If the second edit's 'old' string no longer exists after the first
        edit mutated the file, the second op must return a clean error (not
        corrupt the file or crash).
        """
        target = tmp_path / "x.txt"
        target.write_text("a\n")
        ops = [
            {"op": "edit", "path": str(target), "old": "a", "new": "b"},
            # After the first edit, "a" no longer exists — this should error
            {"op": "edit", "path": str(target), "old": "a", "new": "z"},
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")

        # First edit succeeded, second should report not-found (not crash)
        assert target.read_text() == "b\n"
        assert "ERROR" in out or "not found" in out.lower()


# ---------------------------------------------------------------------------
# 4. Mixed parallel-safe + mutating ops
# ---------------------------------------------------------------------------

class TestMixedOps:
    def test_read_then_edit_then_read_in_batch(self, tmp_path: Path) -> None:
        """read (safe) followed by edit (mutating) followed by read must give
        consistent results — first read sees 'before', second sees 'after'."""
        target = tmp_path / "x.txt"
        target.write_text("before\n")
        ops = [
            {"op": "read", "path": str(target)},
            {"op": "edit", "path": str(target), "old": "before", "new": "after"},
            {"op": "read", "path": str(target)},
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")

        assert "before" in out   # first read
        assert "after" in out    # edit receipt or second read
        assert target.read_text() == "after\n"

    def test_read_and_wc_together(self, tmp_path: Path) -> None:
        """Two read-only ops in the same batch both execute."""
        target = tmp_path / "x.txt"
        target.write_text("hello world\n")
        ops = [
            {"op": "read", "path": str(target)},
            {"op": "wc", "path": str(target)},
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "hello world" in out
        assert "wc" in out.lower() or "lines" in out.lower() or "bytes" in out.lower()


# ---------------------------------------------------------------------------
# 5. Malformed item — missing required fields
# ---------------------------------------------------------------------------

class TestMalformedItem:
    def test_edit_missing_old_and_new_returns_error(self, tmp_path: Path) -> None:
        """An edit op with no 'old'/'new' fields must return a clean ERROR,
        not raise an unhandled exception."""
        target = tmp_path / "x.txt"
        target.write_text("hello\n")
        ops = [{"op": "edit", "path": str(target)}]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" in out
        assert "Traceback" not in out

    def test_malformed_item_continue_on_error_true_keeps_going(self, tmp_path: Path) -> None:
        """With continue_on_error=True (default), a bad item must not stop
        subsequent good ops."""
        target = tmp_path / "x.txt"
        target.write_text("sentinel\n")
        payload = {
            "continue_on_error": True,
            "ops": [
                {"op": "edit"},   # missing path, old, new
                {"op": "read", "path": str(target)},
            ],
        }
        spec = _write_json(tmp_path, "ops.json", payload)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" in out           # malformed op reported
        assert "sentinel" in out        # second op ran


# ---------------------------------------------------------------------------
# 6. continue_on_error=false stops on first failure
# ---------------------------------------------------------------------------

class TestContinueOnErrorFalse:
    def test_stops_after_first_error(self, tmp_path: Path) -> None:
        """When continue_on_error=false, the batch must stop after the first
        failing op and NOT run subsequent ops."""
        good1 = tmp_path / "good1.txt"
        good1.write_text("good1\n")
        good2 = tmp_path / "good2.txt"
        good2.write_text("good2\n")

        payload = {
            "continue_on_error": False,
            "ops": [
                # Op 1: always-failing edit (no match)
                {"op": "edit", "path": str(good1), "old": "NO_SUCH_STRING", "new": "x"},
                # Op 2: should NOT run
                {"op": "read", "path": str(good2)},
            ],
        }
        spec = _write_json(tmp_path, "ops.json", payload)
        out = supertool.dispatch(f"batch:@{spec}")

        assert "good2" not in out, (
            "Op 2 ran even though continue_on_error=false and op 1 failed"
        )

    def test_stops_after_missing_op_field(self, tmp_path: Path) -> None:
        """An item missing the 'op' field is an error; with continue_on_error=false
        the batch must halt."""
        target = tmp_path / "x.txt"
        target.write_text("should_not_appear\n")
        payload = {
            "continue_on_error": False,
            "ops": [
                {"path": "irrelevant"},           # missing 'op'
                {"op": "read", "path": str(target)},
            ],
        }
        spec = _write_json(tmp_path, "ops.json", payload)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "should_not_appear" not in out


# ---------------------------------------------------------------------------
# 7. continue_on_error=true keeps going past failures
# ---------------------------------------------------------------------------

class TestContinueOnErrorTrue:
    def test_keeps_going_after_bad_op(self, tmp_path: Path) -> None:
        """Default (continue_on_error=true): all ops run, errors are collected."""
        target = tmp_path / "x.txt"
        target.write_text("sentinel\n")
        payload = {
            "continue_on_error": True,
            "ops": [
                {"op": "edit", "path": str(target), "old": "NO_MATCH", "new": "x"},
                {"op": "read", "path": str(target)},
                {"op": "edit", "path": str(target), "old": "ALSO_NO_MATCH", "new": "y"},
                {"op": "read", "path": str(target)},
            ],
        }
        spec = _write_json(tmp_path, "ops.json", payload)
        out = supertool.dispatch(f"batch:@{spec}")

        # Both errors reported
        assert out.count("ERROR") >= 2 or out.lower().count("not found") >= 2
        # Both reads ran
        assert out.count("sentinel") >= 2

    def test_default_is_continue_on_error_true(self, tmp_path: Path) -> None:
        """Bare array payload (no continue_on_error key) defaults to True."""
        target = tmp_path / "x.txt"
        target.write_text("after_error\n")
        ops = [
            {"op": "edit", "path": str(target), "old": "NOPE", "new": "x"},
            {"op": "read", "path": str(target)},
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "after_error" in out


# ---------------------------------------------------------------------------
# 8. Unknown op in batch
# ---------------------------------------------------------------------------

class TestUnknownOp:
    def test_unknown_op_returns_clean_error(self, tmp_path: Path) -> None:
        """An unrecognised op name must produce a clean error string, not crash."""
        ops = [{"op": "frobnicate", "path": "/tmp/x"}]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert isinstance(out, str)
        assert "Traceback" not in out
        # The output should contain some indication it failed or is unknown
        # (ERROR, unknown, or similar)

    def test_unknown_op_continue_on_error_true_runs_next(self, tmp_path: Path) -> None:
        """Unknown op is an error; subsequent ops still run under continue_on_error=true."""
        target = tmp_path / "x.txt"
        target.write_text("after_unknown\n")
        ops = [
            {"op": "frobnicate"},
            {"op": "read", "path": str(target)},
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "after_unknown" in out


# ---------------------------------------------------------------------------
# 9. NUL byte injected via path field
# ---------------------------------------------------------------------------

class TestNulByteInjection:
    def test_nul_in_path_does_not_crash(self, tmp_path: Path) -> None:
        """A NUL byte in the 'path' field must not crash supertool — the
        underlying op should reject it with a clean ERROR."""
        ops = [{"op": "read", "path": f"{tmp_path}/foo\x00.bak"}]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert isinstance(out, str)
        assert "Traceback" not in out

    def test_nul_in_edit_path_does_not_crash(self, tmp_path: Path) -> None:
        """NUL in path for a mutating op (edit) must also be handled cleanly."""
        ops = [{
            "op": "edit",
            "path": f"{tmp_path}/target\x00.txt",
            "old": "a",
            "new": "b",
        }]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert isinstance(out, str)
        assert "Traceback" not in out

    def test_nul_in_old_string_does_not_crash(self, tmp_path: Path) -> None:
        """NUL byte embedded in the 'old' search string — clean error, no crash."""
        target = tmp_path / "x.txt"
        target.write_text("hello\n")
        ops = [{
            "op": "edit",
            "path": str(target),
            "old": "hel\x00lo",
            "new": "world",
        }]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert isinstance(out, str)
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 10. Path with command-substitution-looking chars — no shell expansion
# ---------------------------------------------------------------------------

class TestNoShellExpansion:
    def test_command_substitution_in_path_treated_as_literal(self, tmp_path: Path) -> None:
        """A path containing $(cmd) must be treated as a literal filesystem
        path, not expanded by the shell. The op should fail with a file-not-found
        error, not execute the embedded command."""
        dangerous_path = "$(rm -rf /tmp/supertool-test-canary)"
        # Create the canary so we can verify it wasn't deleted
        canary = tmp_path / "canary.txt"
        canary.write_text("alive\n")

        ops = [{"op": "read", "path": dangerous_path}]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")

        # Canary must survive
        assert canary.exists(), "Canary was deleted — shell expansion occurred!"
        # The path should be treated as a literal (not found)
        assert isinstance(out, str)
        assert "Traceback" not in out

    def test_backtick_in_path_treated_as_literal(self, tmp_path: Path) -> None:
        """Backtick command substitution in path — literal, no execution."""
        canary = tmp_path / "canary2.txt"
        canary.write_text("alive\n")

        ops = [{"op": "read", "path": "`touch /tmp/supertool-backtick-fired`"}]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")

        import os
        assert not os.path.exists("/tmp/supertool-backtick-fired"), (
            "Backtick command executed — shell injection in batch path"
        )
        assert canary.exists()
        assert isinstance(out, str)

    def test_semicolon_in_path_treated_as_literal(self, tmp_path: Path) -> None:
        """Semicolon in path must not execute a second shell command.

        Side-effect file test rather than substring match — the literal path
        echoes back in the 'file not found' error message, so a substring
        check would false-fail.
        """
        canary = tmp_path / "INJECTED_CANARY"
        ops = [{"op": "read", "path": f"/tmp/nope; touch {canary}"}]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert isinstance(out, str)
        assert not canary.exists(), "shell ran touch — path arg got executed"

    def test_edit_with_shell_meta_in_old_string_literal(self, tmp_path: Path) -> None:
        """Shell metacharacters in the 'old' search string are literal — no
        expansion, no execution."""
        target = tmp_path / "x.txt"
        target.write_text("$(echo gotcha)\n")
        ops = [{
            "op": "edit",
            "path": str(target),
            "old": "$(echo gotcha)",
            "new": "safe",
        }]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        # The literal string "$(echo gotcha)" was in the file and must be found
        assert "ERROR" not in out, f"Literal $ in old-string was not matched: {out}"
        assert target.read_text() == "safe\n"
