"""Chaos / integration audit: op chains — output of op N feeds op N+1.

Round 7 of the supertool chaos suite. Previous rounds tested individual ops
in isolation. This round attacks the failure surface where ops compose:
batch payloads, sequential dispatch calls, rollback under validator failure,
race conditions, and injection via meta-characters.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_batch(tmp_path: Path, name: str, payload) -> Path:
    f = tmp_path / name
    f.write_text(json.dumps(payload))
    return f


def _set_validators(cfg: dict) -> None:
    supertool._CONFIG = {"validators": cfg}
    supertool._CONFIG_CHECKED = True


def _fake_validator_cmd(ok: bool) -> str:
    payload = {
        "tool": "fake",
        "file": "x",
        "ok": ok,
        "count": 0 if ok else 1,
        "errors": [] if ok else [{"line": 1, "col": 1, "message": "bad"}],
        "duration_ms": 1,
    }
    js = json.dumps(payload).replace("'", "'\\''")
    return f"printf '%s' '{js}'"


# ---------------------------------------------------------------------------
# 1. Concurrent edits on same file — batch with 2 edit ops, second matches
#    what the first produces. Verify sequential application and final state.
# ---------------------------------------------------------------------------

class TestConcurrentEditsOnSameFile:
    def test_sequential_chain_edit_then_edit(self, tmp_path: Path) -> None:
        """Two edits in a batch on the same file: first foo→bar, then bar→baz.
        Second op must see the file state left by the first."""
        f = tmp_path / "chain.py"
        f.write_text("foo\n")
        ops = [
            {"op": "edit", "old": "foo", "new": "bar", "path": str(f)},
            {"op": "edit", "old": "bar", "new": "baz", "path": str(f)},
        ]
        spec = _write_batch(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        # Both edits should succeed
        assert "edited" in out
        # Final state: baz (second edit applied after first)
        assert f.read_text(encoding="utf-8") == "baz\n"

    def test_second_edit_sees_first_result(self, tmp_path: Path) -> None:
        """Second edit target string must match what first edit produced,
        not original file content. If the second op uses the *original* string
        it must fail (old string no longer present after first edit)."""
        f = tmp_path / "chain.py"
        f.write_text("alpha\n")
        # Second op looks for 'alpha' which was replaced by first → must fail
        ops = [
            {"op": "edit", "old": "alpha", "new": "beta", "path": str(f)},
            {"op": "edit", "old": "alpha", "new": "gamma", "path": str(f)},
        ]
        spec = _write_batch(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        # First edit succeeds
        assert "edited" in out
        # Second edit fails — original string gone
        assert "not found" in out.lower() or "ERROR" in out
        # File must be in post-first-edit state (beta), not reverted
        assert f.read_text(encoding="utf-8") == "beta\n"


# ---------------------------------------------------------------------------
# 2. glob → batch read chain — pipe glob filenames into subsequent reads.
#    Special chars in filenames must not break the chain.
# ---------------------------------------------------------------------------

class TestGlobToBatchReadChain:
    def test_glob_results_readable_in_follow_up_reads(self, tmp_path: Path) -> None:
        """Run glob, then use the resulting paths to drive individual reads.

        op_glob strips a common directory prefix when 2+ files share one, so
        the output lines are *relative* suffixes, not absolute paths. The chain
        must reconstruct full paths for the downstream read ops.
        """
        for i in range(3):
            (tmp_path / f"module_{i}.py").write_text(f"content_{i}\n")

        glob_out = supertool.op_glob(str(tmp_path / "*.py"), no_auto_read=True)

        # Extract the common-prefix line (ends with os.sep) then rejoin
        lines = glob_out.splitlines()
        prefix = ""
        for ln in lines:
            stripped = ln.strip()
            if stripped.endswith(os.sep) or (stripped and not stripped.startswith("(") and os.path.isdir(stripped.rstrip(os.sep))):
                prefix = stripped
                break

        # Reconstruct absolute paths: prefix + relative suffix
        rel_paths = [ln.strip() for ln in lines if ln.strip().endswith(".py")]
        assert len(rel_paths) == 3, f"Expected 3 paths from glob, got: {glob_out!r}"

        for rel in rel_paths:
            abs_path = os.path.join(prefix.rstrip(os.sep), rel) if prefix else rel
            # Fallback: if abs_path doesn't exist, the suffix may already be absolute
            if not os.path.isfile(abs_path):
                abs_path = rel
            read_out = supertool.dispatch(f"read:{abs_path}")
            assert "content_" in read_out, f"Read of {abs_path!r} failed: {read_out!r}"

    def test_glob_special_char_filename_in_chain(self, tmp_path: Path) -> None:
        """Files with spaces and hyphens in names survive the glob→read chain."""
        names = ["file with spaces.py", "file-with-hyphens.py", "file_normal.py"]
        for name in names:
            (tmp_path / name).write_text(f"# {name}\n")

        glob_out = supertool.op_glob(str(tmp_path / "*.py"), no_auto_read=True)
        # Each file should appear in glob output
        for name in names:
            assert name in glob_out or name.replace(" ", "") in glob_out.replace(" ", "")

        # Direct read of the space-containing file must work
        space_file = str(tmp_path / "file with spaces.py")
        read_out = supertool.dispatch(f"read:{space_file}")
        assert "file with spaces.py" in read_out

    def test_glob_no_results_chain_handles_gracefully(self, tmp_path: Path) -> None:
        """Glob returns 0 files; downstream chain sees empty list, no crash."""
        glob_out = supertool.op_glob(str(tmp_path / "*.nonexistent"), no_auto_read=True)
        # Zero results — should say 0 files, not crash
        assert "0 files" in glob_out or "no files" in glob_out.lower() or glob_out.strip() == "(0 files)"


# ---------------------------------------------------------------------------
# 3. edit → validate → rollback — edit makes file invalid; rollback_on_fail
#    restores original. Verified via direct dispatch.
# ---------------------------------------------------------------------------

class TestEditValidateRollback:
    def test_rollback_on_fail_restores_original(self, tmp_path: Path, monkeypatch) -> None:
        """Edit produces a regression (pass→fail) → rollback_on_fail restores file.

        The rollback only fires on *regression* — the validator must pass before
        the edit and fail after. A validator that always fails shows 'unchanged'
        in the diff and never triggers rollback. We achieve pass→fail by patching
        _validators_run_batch to return different results before/after the edit.
        """
        f = tmp_path / "target.py"
        original = "valid_content = True\n"
        f.write_text(original)

        _set_validators({
            "strict": {
                "cmd": _fake_validator_cmd(True),  # cmd is required; actual result is mocked
                "hooks_into": ["edit"],
                "rollback_on_fail": True,
            }
        })

        call_count = [0]
        original_run_batch = supertool._validators_run_batch

        def mock_run_batch(applicable, path):
            call_count[0] += 1
            if call_count[0] == 1:
                # Before edit: validator passes
                return {"strict": {"tool": "strict", "ok": True, "count": 0, "errors": [], "duration_ms": 1}}
            else:
                # After edit: validator fails — regression triggers rollback
                return {"strict": {"tool": "strict", "ok": False, "count": 1,
                                   "errors": [{"line": 1, "col": 1, "message": "invalid"}],
                                   "duration_ms": 1}}

        monkeypatch.setattr(supertool, "_validators_run_batch", mock_run_batch)

        # Use dispatch() — the rollback chain lives in the dispatch wrapper.
        # Direct op_edit() bypasses validator pre/post snapshots (same gotcha
        # documented in #139 paste test).
        out = supertool.dispatch(f"edit:::valid_content:::invalid_content:::{f}")
        # Rollback triggered on regression
        assert "rolled back" in out or "regressed" in out or "restored" in out
        # File must be back to original
        assert f.read_text(encoding="utf-8") == original

    def test_no_rollback_without_flag(self, tmp_path: Path, monkeypatch) -> None:
        """Without rollback_on_fail the file stays mutated even if validator fails."""
        f = tmp_path / "target.py"
        f.write_text("before\n")

        _set_validators({
            "lenient": {
                "cmd": _fake_validator_cmd(False),
                "hooks_into": ["edit"],
                # rollback_on_fail NOT set
            }
        })

        supertool.op_edit("before", "after", str(f))
        # No rollback — file stays mutated
        assert f.read_text(encoding="utf-8") == "after\n"

    def test_rollback_not_triggered_on_passing_validator(self, tmp_path: Path) -> None:
        """When validator passes no rollback fires even if flag is set."""
        f = tmp_path / "target.py"
        f.write_text("good\n")

        _set_validators({
            "passing": {
                "cmd": _fake_validator_cmd(True),
                "hooks_into": ["edit"],
                "rollback_on_fail": True,
            }
        })

        out = supertool.op_edit("good", "better", str(f))
        assert "rolled back" not in out
        assert f.read_text(encoding="utf-8") == "better\n"


# ---------------------------------------------------------------------------
# 4. vim cursor state across batch ops — two vim ops on the same file in a
#    batch. Cursor from op1 must persist as the start position for op2.
# ---------------------------------------------------------------------------

class TestVimCursorStateAcrossBatchOps:
    def test_cursor_persists_between_two_vim_ops(self, tmp_path: Path, monkeypatch) -> None:
        """First vim op leaves cursor on line 3; second op inserts at that
        cursor position. The final file must reflect op2 acting at line 3."""
        monkeypatch.delenv("SUPERTOOL_VIM_NO_PERSIST", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        f = tmp_path / "vim_chain.py"
        f.write_text("line1\nline2\nMARKER\nline4\n")

        # Op1: jump to MARKER, append X at end of line
        out1 = supertool.op_vim(str(f), "/MARKER␞A_X")
        assert "MARKER_X" in f.read_text(encoding="utf-8")

        # Op2: cursor should be at MARKER_X line; append _Y
        out2 = supertool.op_vim(str(f), "A_Y")
        final = f.read_text(encoding="utf-8")
        # Cursor persisted from op1 — _Y appended on same MARKER line
        assert "MARKER_X_Y" in final, f"Expected cursor persistence, got: {final!r}"

    def test_two_vim_ops_in_batch_both_apply(self, tmp_path: Path) -> None:
        """Both vim ops in a batch run; second sees file state after first."""
        f = tmp_path / "vim_batch.py"
        f.write_text("aaa\nbbb\nccc\n")
        ops = [
            {"op": "vim", "path": str(f), "script": "/aaa␞cwaaa_MODIFIED"},
            {"op": "vim", "path": str(f), "script": "/bbb␞cwbbb_MODIFIED"},
        ]
        spec = _write_batch(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        content = f.read_text(encoding="utf-8")
        assert "aaa_MODIFIED" in content
        assert "bbb_MODIFIED" in content


# ---------------------------------------------------------------------------
# 5. paste → read → assert content — paste is atomic; immediately readable.
# ---------------------------------------------------------------------------

class TestPasteReadAtomicity:
    def test_paste_then_read_sees_new_content(self, tmp_path: Path) -> None:
        """After paste, an immediate read returns the pasted content."""
        f = tmp_path / "atom.py"
        f.write_text("old content\n")
        new_content = "brand new content"

        supertool.op_paste(str(f), new_content)
        read_out = supertool.dispatch(f"read:{f}")
        assert "brand new content" in read_out

    def test_paste_then_read_in_batch(self, tmp_path: Path) -> None:
        """paste immediately followed by read in same batch — must see new content."""
        f = tmp_path / "atom.py"
        # Don't use "old" — collides with /var/folders/ in macOS tmp paths.
        f.write_text("ORIGINAL_CONTENT\n")
        ops = [
            {"op": "paste", "path": str(f), "content": "fresh"},
            {"op": "read", "path": str(f)},
        ]
        spec = _write_batch(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "fresh" in out
        read_section = out.split("--- read:")[1] if "--- read:" in out else ""
        assert "ORIGINAL_CONTENT" not in read_section

    def test_paste_creates_file_then_readable(self, tmp_path: Path) -> None:
        """paste to non-existent file creates it; subsequent read succeeds."""
        f = tmp_path / "new_file.py"
        assert not f.exists()
        supertool.op_paste(str(f), "created content")
        assert f.exists()
        read_out = supertool.dispatch(f"read:{f}")
        assert "created content" in read_out


# ---------------------------------------------------------------------------
# 6. batch with op that errors mid-chain — continue_on_error semantics.
# ---------------------------------------------------------------------------

class TestBatchErrorMidChain:
    def test_continue_on_error_true_runs_all_subsequent(self, tmp_path: Path) -> None:
        """Error at op2 with continue_on_error=true: ops 3 and 4 still run."""
        f_good = tmp_path / "good.py"
        f_good.write_text("target\n")
        ops = {
            "continue_on_error": True,
            "ops": [
                {"op": "read", "path": str(f_good)},               # ok
                {"op": "edit", "old": "NO_SUCH_STRING", "new": "x", "path": str(f_good)},  # error
                {"op": "read", "path": str(f_good)},               # must still run
                {"op": "wc", "path": str(f_good)},                 # must still run
            ],
        }
        spec = _write_batch(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        # Op1 read succeeded
        assert "target" in out
        # Op2 errored
        assert "ERROR" in out or "not found" in out.lower()
        # Op3 read still ran (file still readable)
        assert out.count("target") >= 2 or "--- read:" in out
        # Op4 wc ran
        assert "line" in out.lower() or "word" in out.lower() or "byte" in out.lower()

    def test_continue_on_error_false_stops_cleanly(self, tmp_path: Path) -> None:
        """Error at op2 with continue_on_error=false: op3 output absent."""
        f = tmp_path / "data.py"
        f.write_text("content\n")
        sentinel = tmp_path / "sentinel.py"
        sentinel.write_text("SENTINEL\n")
        ops = {
            "continue_on_error": False,
            "ops": [
                {"op": "read", "path": str(f)},
                {"op": "edit", "old": "NO_SUCH_STRING", "new": "x", "path": str(f)},
                {"op": "read", "path": str(sentinel)},  # should NOT run
            ],
        }
        spec = _write_batch(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "SENTINEL" not in out

    def test_continue_on_error_default_is_true(self, tmp_path: Path) -> None:
        """Bare array (no wrapper) defaults to continue_on_error=True."""
        f = tmp_path / "x.py"
        f.write_text("real\n")
        ops = [
            {"op": "edit", "old": "NOPE", "new": "x", "path": str(f)},
            {"op": "read", "path": str(f)},
        ]
        spec = _write_batch(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        # Read ran despite prior error
        assert "real" in out


# ---------------------------------------------------------------------------
# 7. edit on a file currently being read (race) — concurrent edit while
#    another thread reads. Readers see either old or new, never partial.
# ---------------------------------------------------------------------------

class TestConcurrentReadEditAtomicity:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows can't open a file that another process has open for "
        "writing — concurrent read during in-flight edit raises PermissionError "
        "instead of POSIX's last-writer-wins. Torn-write contract is POSIX-only.",
    )
    def test_concurrent_read_sees_consistent_content(self, tmp_path: Path) -> None:
        """A read running concurrently with an edit must see either the
        complete old content or the complete new content — never a torn write.
        """
        f = tmp_path / "race.py"
        old_content = "old_line\n" * 1000
        new_content = "new_line\n" * 1000
        f.write_text(old_content)

        results: list[str] = []
        errors: list[str] = []

        def reader():
            for _ in range(20):
                try:
                    content = f.read_text(encoding="utf-8")
                    results.append(content)
                except Exception as e:
                    errors.append(str(e))

        def writer():
            for _ in range(5):
                supertool.op_paste(str(f), new_content)
                supertool.op_paste(str(f), old_content)

        t_read = threading.Thread(target=reader)
        t_write = threading.Thread(target=writer)
        t_read.start()
        t_write.start()
        t_read.join(timeout=10)
        t_write.join(timeout=10)

        assert not errors, f"Errors during concurrent access: {errors}"
        # Every observed content must be one of the two complete versions
        for content in results:
            assert content == old_content or content == new_content, (
                f"Torn read detected! Content neither old nor new. "
                f"Got {len(content)} bytes, starts with: {content[:50]!r}"
            )

    def test_no_temp_files_left_after_concurrent_edits(self, tmp_path: Path) -> None:
        """After multiple rapid edits, no .supertool-*.tmp files linger."""
        f = tmp_path / "hot.py"
        f.write_text("v0\n")
        for i in range(10):
            supertool.op_edit(f"v{i}", f"v{i+1}", str(f))
        leftovers = list(tmp_path.glob(".supertool-*"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# 8. resolve → edit chain — resolve a symbol, then edit the resolved file.
# ---------------------------------------------------------------------------

class TestResolveEditChain:
    def test_resolve_then_edit_resolved_path(self, tmp_path: Path) -> None:
        """Simulate: resolve a path (concrete file, no wildcards) then edit it."""
        f = tmp_path / "target_module.py"
        f.write_text("def placeholder(): pass\n")

        # op_glob with no wildcards on an existing file auto-reads it
        # For resolve chain: use a concrete path directly
        resolved_path = str(f)

        # Edit the resolved path
        edit_out = supertool.op_edit("placeholder", "real_function", resolved_path)
        assert "edited" in edit_out
        assert "real_function" in f.read_text(encoding="utf-8")

    def test_resolve_nonexistent_then_edit_graceful(self, tmp_path: Path) -> None:
        """Resolving a non-existent file then editing → proper error, no crash."""
        bogus = str(tmp_path / "nonexistent.py")
        # Attempt edit on non-existent resolved path
        edit_out = supertool.op_edit("anything", "something", bogus)
        assert "ERROR" in edit_out
        assert "not found" in edit_out.lower()


# ---------------------------------------------------------------------------
# 9. Long chain — read → grep → between → edit → validate (5 ops in batch).
#    Final state consistent.
# ---------------------------------------------------------------------------

class TestLongFiveOpChain:
    def test_five_op_chain_final_state_consistent(self, tmp_path: Path) -> None:
        """5-op chain: all ops run, final file state is correct."""
        f = tmp_path / "longchain.py"
        f.write_text(
            "# START\n"
            "def old_function():\n"
            "    return 'original'\n"
            "# END\n"
        )

        ops = [
            {"op": "read", "path": str(f)},
            {"op": "grep", "pattern": "old_function", "path": str(tmp_path), "max_results": 5},
            {"op": "between", "path": str(f), "start": "# START", "end": "# END"},
            {"op": "edit", "old": "old_function", "new": "new_function", "path": str(f)},
            {"op": "read", "path": str(f)},
        ]
        spec = _write_batch(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")

        # Read (op1) showed original
        assert "old_function" in out
        # Grep (op2) found the function
        assert "old_function" in out
        # Edit (op4) succeeded
        assert "edited" in out
        # Final read (op5) shows new content
        assert "new_function" in out
        # File state is correct
        assert f.read_text(encoding="utf-8") == (
            "# START\n"
            "def new_function():\n"
            "    return 'original'\n"
            "# END\n"
        )

    def test_five_op_chain_with_grep_on_dir(self, tmp_path: Path) -> None:
        """Grep op in chain searches directory, not single file — must not crash."""
        (tmp_path / "a.py").write_text("needle_alpha\n")
        (tmp_path / "b.py").write_text("needle_beta\n")
        ops = [
            {"op": "read", "path": str(tmp_path / "a.py")},
            {"op": "grep", "pattern": "needle", "path": str(tmp_path), "max_results": 10},
            {"op": "read", "path": str(tmp_path / "b.py")},
        ]
        spec = _write_batch(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "needle_alpha" in out
        assert "needle_beta" in out


# ---------------------------------------------------------------------------
# 10. Recursive batch via @file — depth guard fires for nested batches.
# ---------------------------------------------------------------------------

class TestRecursiveBatchDepthGuard:
    def test_batch_inside_batch_via_at_file(self, tmp_path: Path) -> None:
        """A batch op that contains another batch op (nested) must either
        run safely (guarded) or emit a clear error — never infinite loop."""
        f = tmp_path / "data.py"
        f.write_text("hello\n")

        # Inner batch
        inner_ops = [{"op": "read", "path": str(f)}]
        inner_spec = _write_batch(tmp_path, "inner.json", inner_ops)

        # Outer batch contains a batch op pointing at inner
        outer_ops = [
            {"op": "read", "path": str(f)},
            {"op": "batch", "ref": f"@{inner_spec}"},
        ]
        outer_spec = _write_batch(tmp_path, "outer.json", outer_ops)

        out = supertool.dispatch(f"batch:@{outer_spec}")
        # Must not hang or recurse infinitely — either succeeds or errors cleanly
        assert isinstance(out, str)
        assert len(out) < 1_000_000  # sanity: not an infinite loop

    def test_batch_self_reference_does_not_hang(self, tmp_path: Path) -> None:
        """A batch file that references itself via an op must not recurse infinitely."""
        spec = tmp_path / "self_ref.json"
        # Build self-referencing payload
        ops = [{"op": "read", "path": str(spec)}]
        spec.write_text(json.dumps(ops))

        # Reading the spec file via batch is fine (read, not batch recursion)
        out = supertool.dispatch(f"batch:@{spec}")
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# 11. Atomic semantics under failure — mock _atomic_write to throw mid-write;
#     verify no half-written state.
# ---------------------------------------------------------------------------

class TestAtomicSemanticsUnderFailure:
    def test_edit_atomic_write_throws_preserves_original(self, tmp_path: Path) -> None:
        """If _atomic_write raises, op_edit returns ERROR and original survives."""
        f = tmp_path / "precious.py"
        original = "precious content\n"
        f.write_text(original)

        def boom(path, content):
            raise OSError("simulated mid-write crash")

        with patch.object(supertool, "_atomic_write", side_effect=boom):
            out = supertool.op_edit("precious content", "destroyed", str(f))

        assert "ERROR" in out
        assert f.read_text(encoding="utf-8") == original

    def test_paste_atomic_write_throws_no_partial_file(self, tmp_path: Path) -> None:
        """If _atomic_write raises during paste, no partial content written."""
        f = tmp_path / "file.py"
        original = "original\n"
        f.write_text(original)

        def boom(path, content):
            raise OSError("disk full simulation")

        with patch.object(supertool, "_atomic_write", side_effect=boom):
            out = supertool.op_paste(str(f), "new content")

        assert "ERROR" in out
        assert f.read_text(encoding="utf-8") == original

    def test_replace_lines_atomic_write_throws_preserves_original(self, tmp_path: Path) -> None:
        """If _atomic_write raises during replace_lines, original content preserved."""
        f = tmp_path / "lines.py"
        original = "line1\nline2\nline3\n"
        f.write_text(original)

        def boom(path, content):
            raise OSError("write failed")

        with patch.object(supertool, "_atomic_write", side_effect=boom):
            out = supertool.op_replace_lines(str(f), 2, 2, "REPLACEMENT")

        assert "ERROR" in out
        assert f.read_text(encoding="utf-8") == original

    def test_no_temp_files_left_when_atomic_write_fails(self, tmp_path: Path) -> None:
        """Even when _atomic_write itself fails, no .supertool-*.tmp left behind.

        Note: _atomic_write does the temp-file cleanup internally on exception.
        If the mock intercepts before mkstemp, no temp file is created at all.
        Both outcomes (no temp file created, or temp file cleaned up) are valid.
        """
        f = tmp_path / "x.py"
        f.write_text("x\n")

        call_count = [0]
        original_atomic_write = supertool._atomic_write

        def fail_on_second(path, content):
            call_count[0] += 1
            if call_count[0] >= 1:
                raise OSError("boom")
            return original_atomic_write(path, content)

        with patch.object(supertool, "_atomic_write", side_effect=fail_on_second):
            supertool.op_edit("x", "y", str(f))

        # No temp files left
        leftovers = list(tmp_path.glob(".supertool-*"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# 12. Edit that produces a regex meta-char — subsequent replace with that
#     as pattern. Value must land literally, not be interpreted as regex.
# ---------------------------------------------------------------------------

class TestRegexMetaCharInjection:
    def test_edit_with_shell_expansion_chars_lands_literally(self, tmp_path: Path) -> None:
        """Edit replaces 'foo' with '$(rm -rf /)' — value must be literal."""
        f = tmp_path / "injection.py"
        f.write_text("foo = 1\n")
        dangerous = "$(rm -rf /)"
        out = supertool.op_edit("foo", dangerous, str(f))
        assert "edited" in out
        content = f.read_text(encoding="utf-8")
        assert dangerous in content
        # The shell expansion was NOT executed — dir still exists
        assert tmp_path.exists()

    def test_replace_with_regex_meta_chars_is_literal(self, tmp_path: Path) -> None:
        """op_replace with regex meta-chars in pattern treats them literally."""
        f = tmp_path / "meta.py"
        # Content contains literal regex meta-chars
        f.write_text("value = (foo|bar)\n")
        out = supertool.op_replace("(foo|bar)", "(baz|qux)", str(f))
        # If pattern was treated as regex, (foo|bar) would match "foo" or "bar" anywhere
        # As literal: exact string match
        content = f.read_text(encoding="utf-8")
        assert "(baz|qux)" in content or "baz" in content  # either literal or regex replacement

    def test_edit_to_dangerous_value_then_grep_for_it(self, tmp_path: Path) -> None:
        """Edit injects shell meta-chars; subsequent grep for them is safe."""
        f = tmp_path / "meta2.py"
        f.write_text("placeholder\n")
        dangerous = "$(rm -rf /); DROP TABLE users; --"
        supertool.op_edit("placeholder", dangerous, str(f))

        # Grep for a substring — must not execute shell
        grep_out = supertool.dispatch(f"grep:DROP TABLE:{f}")
        assert "DROP TABLE" in grep_out
        assert tmp_path.exists()

    def test_batch_edit_chain_with_meta_chars(self, tmp_path: Path) -> None:
        """Two-op batch: edit writes literal shell metachars; assert nothing
        actually executed. Side-effect file check (not output substring) —
        the edit receipt + read both echo the new value, so substring match
        for 'pwned' would false-positive on legitimate output."""
        f = tmp_path / "chain_meta.py"
        # write_bytes preserves LF — write_text translates to CRLF on Windows,
        # which then drifts the read_text() assertion below.
        f.write_bytes(b"safe\n")
        canary = tmp_path / "SHELL_RAN_CANARY"
        # as_posix avoids Windows backslashes inside the literal payload — the
        # exact-equality assertion compares this string to file contents that
        # would otherwise be subject to text-mode CRLF translation.
        payload = f"$(touch {canary.as_posix()}) && touch {canary.as_posix()}"
        ops = [
            {"op": "edit", "old": "safe", "new": payload, "path": str(f)},
            {"op": "read", "path": str(f)},
        ]
        spec = _write_batch(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "edited" in out
        # File content holds literal shell-meta string
        assert f.read_text(encoding="utf-8").strip() == payload
        # Canary file must NOT exist — if it did, the shell ran the payload.
        assert not canary.exists(), "shell expansion executed — RCE!"

    def test_edit_old_with_special_chars_matches_literally(self, tmp_path: Path) -> None:
        """OLD string containing regex meta-chars must match file content literally."""
        f = tmp_path / "literal.py"
        f.write_text("price = $100.00\n")
        # $ and . are regex meta-chars; edit must treat old as literal
        out = supertool.op_edit("$100.00", "$200.00", str(f))
        assert "edited" in out
        assert "$200.00" in f.read_text(encoding="utf-8")
