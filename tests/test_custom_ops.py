"""Tests for custom ops and aliases in .supertool.json config."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# _resolve_custom_op
# ---------------------------------------------------------------------------

class TestResolveCustomOp:
    """Custom ops: shell commands defined in config["ops"]."""

    def test_basic_custom_op(self, tmp_path: Path) -> None:
        """A custom op runs its cmd and returns PASS on success."""
        f = tmp_path / "hello.txt"
        f.write_text("world\n")
        supertool._CONFIG = {
            "ops": {"greet": {"cmd": f"cat {{file}}"}}
        }
        result = supertool._resolve_custom_op("greet", ["greet", str(f)])
        assert result is not None
        assert "PASS" in result
        assert "world" in result

    def test_file_placeholder_replaced(self, tmp_path: Path) -> None:
        """The {file} placeholder is replaced with the path argument."""
        f = tmp_path / "target.txt"
        f.write_text("content\n")
        supertool._CONFIG = {
            "ops": {"show": {"cmd": "echo {file}"}}
        }
        result = supertool._resolve_custom_op("show", ["show", str(f)])
        assert result is not None
        assert str(f) in result

    def test_timeout_from_op_config(self, tmp_path: Path) -> None:
        """Per-op timeout is respected."""
        supertool._CONFIG = {
            "ops": {"slow": {"cmd": "sleep 10", "timeout": 1}}
        }
        result = supertool._resolve_custom_op("slow", ["slow", "x"])
        assert result is not None
        assert "FAIL" in result

    def test_timeout_fallback_to_top_level(self, tmp_path: Path) -> None:
        """When op has no timeout, uses top-level timeout setting."""
        supertool._CONFIG = {
            "timeout": 1,
            "ops": {"slow": {"cmd": "sleep 10"}}
        }
        result = supertool._resolve_custom_op("slow", ["slow", "x"])
        assert result is not None
        assert "FAIL" in result

    def test_timeout_default_60(self, tmp_path: Path) -> None:
        """When no timeout anywhere, defaults to 60s."""
        supertool._CONFIG = {
            "ops": {"fast": {"cmd": "echo ok"}}
        }
        # Just verify it doesn't crash — 60s is plenty for echo
        result = supertool._resolve_custom_op("fast", ["fast", "x"])
        assert result is not None
        assert "PASS" in result

    def test_unknown_op_returns_none(self) -> None:
        """An op not in config returns None (falls through)."""
        supertool._CONFIG = {"ops": {"known": {"cmd": "echo hi"}}}
        result = supertool._resolve_custom_op("unknown", ["unknown", "x"])
        assert result is None

    def test_no_ops_section_returns_none(self) -> None:
        """Config with no ops section returns None."""
        supertool._CONFIG = {"compact": True}
        result = supertool._resolve_custom_op("anything", ["anything", "x"])
        assert result is None

    def test_empty_cmd_returns_error(self) -> None:
        """An op with an empty cmd returns ERROR."""
        supertool._CONFIG = {"ops": {"bad": {"cmd": ""}}}
        result = supertool._resolve_custom_op("bad", ["bad", "x"])
        assert result is not None
        assert "ERROR" in result

    def test_cmd_without_file_placeholder(self) -> None:
        """A cmd without {file} runs as-is (global command)."""
        supertool._CONFIG = {
            "ops": {"version": {"cmd": "echo v1.0"}}
        }
        result = supertool._resolve_custom_op("version", ["version"])
        assert result is not None
        assert "PASS" in result
        assert "v1.0" in result

    def test_failing_command_returns_fail(self) -> None:
        """A command that exits non-zero returns FAIL."""
        supertool._CONFIG = {
            "ops": {"fail": {"cmd": "exit 1"}}
        }
        result = supertool._resolve_custom_op("fail", ["fail", "x"])
        assert result is not None
        assert "FAIL" in result

    def test_stderr_captured(self) -> None:
        """Stderr from the command is included in output."""
        supertool._CONFIG = {
            "ops": {"warn": {"cmd": "echo warning >&2 && exit 1"}}
        }
        result = supertool._resolve_custom_op("warn", ["warn", "x"])
        assert result is not None
        assert "warning" in result

    def test_string_shorthand_cmd(self) -> None:
        """An op can be a plain string instead of {cmd: ...}."""
        supertool._CONFIG = {
            "ops": {"hi": "echo hello"}
        }
        result = supertool._resolve_custom_op("hi", ["hi", "x"])
        assert result is not None
        assert "PASS" in result
        assert "hello" in result

    def test_dir_placeholder_replaced(self, tmp_path: Path) -> None:
        """The {dir} placeholder is replaced with dirname of the path arg."""
        f = tmp_path / "sub" / "file.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n")
        supertool._CONFIG = {
            "ops": {"lsdir": {"cmd": "ls {dir}"}}
        }
        result = supertool._resolve_custom_op("lsdir", ["lsdir", str(f)])
        assert result is not None
        assert "file.txt" in result

    def test_output_format_matches_check(self, tmp_path: Path) -> None:
        """Custom op output format: timing line + stdout."""
        f = tmp_path / "t.txt"
        f.write_text("data\n")
        supertool._CONFIG = {
            "ops": {"peek": {"cmd": f"cat {{file}}"}}
        }
        result = supertool._resolve_custom_op("peek", ["peek", str(f)])
        assert result is not None
        # Should have timing like "PASS (0.01s)"
        assert "s)" in result


# ---------------------------------------------------------------------------
# _resolve_alias
# ---------------------------------------------------------------------------

class TestResolveAlias:
    """Aliases: batch expansion of multiple ops."""

    def test_alias_expands_builtin_ops(self, tmp_path: Path) -> None:
        """An alias expanding to built-in ops executes all of them."""
        f = tmp_path / "code.py"
        f.write_text("def foo():\n    pass\n")
        supertool._CONFIG = {
            "aliases": {"inspect": {"ops": [f"read:{f}", f"wc:{f}"]}}
        }
        result = supertool._resolve_alias("inspect", ["inspect", str(f)])
        assert result is not None
        assert "--- read:" in result
        assert "--- wc:" in result

    def test_file_placeholder_in_alias(self, tmp_path: Path) -> None:
        """The {file} placeholder is replaced in all expanded ops."""
        f = tmp_path / "x.py"
        f.write_text("hello\n")
        supertool._CONFIG = {
            "aliases": {"look": {"ops": ["read:{file}", "wc:{file}"]}}
        }
        result = supertool._resolve_alias("look", ["look", str(f)])
        assert result is not None
        assert "hello" in result

    def test_dir_placeholder_in_alias(self, tmp_path: Path) -> None:
        """The {dir} placeholder is replaced with dirname in alias ops."""
        f = tmp_path / "sub" / "x.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("code\n")
        supertool._CONFIG = {
            "aliases": {"context": {"ops": ["read:{file}", "ls:{dir}"]}}
        }
        result = supertool._resolve_alias("context", ["context", str(f)])
        assert result is not None
        assert "code" in result
        assert "x.py" in result

    def test_alias_with_custom_ops(self, tmp_path: Path) -> None:
        """An alias can reference custom ops defined in the same config."""
        f = tmp_path / "t.txt"
        f.write_text("test\n")
        supertool._CONFIG = {
            "ops": {"shout": {"cmd": "echo LOUD"}},
            "aliases": {"combo": {"ops": ["shout:{file}", "read:{file}"]}}
        }
        result = supertool._resolve_alias("combo", ["combo", str(f)])
        assert result is not None
        assert "LOUD" in result
        assert "test" in result

    def test_alias_unknown_op_errors_others_run(self, tmp_path: Path) -> None:
        """If one op in an alias is unknown, it errors but others still run."""
        f = tmp_path / "x.py"
        f.write_text("hi\n")
        supertool._CONFIG = {
            "aliases": {"mixed": {"ops": [f"read:{f}", "nosuchop:arg"]}}
        }
        result = supertool._resolve_alias("mixed", ["mixed", str(f)])
        assert result is not None
        assert "hi" in result
        assert "ERROR" in result

    def test_empty_alias_returns_empty(self) -> None:
        """An alias with an empty ops list returns empty string."""
        supertool._CONFIG = {"aliases": {"noop": {"ops": []}}}
        result = supertool._resolve_alias("noop", ["noop", "x"])
        assert result is not None
        assert result == ""

    def test_unknown_alias_returns_none(self) -> None:
        """An alias not in config returns None (falls through)."""
        supertool._CONFIG = {"aliases": {"known": {"ops": ["read:x"]}}}
        result = supertool._resolve_alias("unknown", ["unknown", "x"])
        assert result is None

    def test_no_aliases_section_returns_none(self) -> None:
        """Config with no aliases section returns None."""
        supertool._CONFIG = {"compact": True}
        result = supertool._resolve_alias("anything", ["anything", "x"])
        assert result is None

    def test_no_recursive_aliases(self, tmp_path: Path) -> None:
        """An alias referencing another alias treats it as unknown op."""
        f = tmp_path / "x.py"
        f.write_text("data\n")
        supertool._CONFIG = {
            "aliases": {
                "inner": {"ops": [f"read:{f}"]},
                "outer": {"ops": ["inner:{file}"]}
            }
        }
        result = supertool._resolve_alias("outer", ["outer", str(f)])
        assert result is not None
        # inner is not a built-in op or custom op, so it should error
        assert "ERROR" in result


# ---------------------------------------------------------------------------
# dispatch integration — priority order
# ---------------------------------------------------------------------------

class TestDispatchPriority:
    """Dispatch order: built-in > custom op > alias."""

    def test_builtin_wins_over_custom_op(self, tmp_path: Path) -> None:
        """A custom op named 'read' does NOT override the built-in read."""
        f = tmp_path / "x.txt"
        f.write_text("original\n")
        supertool._CONFIG = {
            "ops": {"read": {"cmd": "echo CUSTOM"}}
        }
        out = supertool.dispatch(f"read:{f}")
        assert "original" in out
        assert "CUSTOM" not in out

    def test_builtin_wins_over_alias(self, tmp_path: Path) -> None:
        """An alias named 'grep' does NOT override the built-in grep."""
        f = tmp_path / "x.txt"
        f.write_text("needle\nhaystack\n")
        supertool._CONFIG = {
            "aliases": {"grep": {"ops": ["read:{file}"]}}
        }
        out = supertool.dispatch(f"grep:needle:{f}")
        # Should behave like real grep, not alias
        assert "needle" in out

    def test_custom_op_wins_over_alias(self) -> None:
        """When both custom op and alias exist with same name, op wins."""
        supertool._CONFIG = {
            "ops": {"tool": {"cmd": "echo FROM_OP"}},
            "aliases": {"tool": {"ops": ["read:something"]}}
        }
        out = supertool.dispatch("tool:x")
        assert "FROM_OP" in out

    def test_custom_op_dispatched_via_dispatch(self) -> None:
        """A custom op works through the main dispatch function."""
        supertool._CONFIG = {
            "ops": {"ping": {"cmd": "echo pong"}}
        }
        out = supertool.dispatch("ping:x")
        assert "--- ping:x ---" in out
        assert "pong" in out

    def test_alias_dispatched_via_dispatch(self, tmp_path: Path) -> None:
        """An alias works through the main dispatch function."""
        f = tmp_path / "x.py"
        f.write_text("content\n")
        supertool._CONFIG = {
            "aliases": {"look": {"ops": [f"read:{f}", f"wc:{f}"]}}
        }
        out = supertool.dispatch(f"look:{f}")
        assert "--- look:" in out
        assert "content" in out

    def test_unknown_everything_still_errors(self) -> None:
        """When nothing matches, the error message is returned."""
        supertool._CONFIG = {"ops": {}, "aliases": {}}
        out = supertool.dispatch("nope:x")
        assert "ERROR: unknown operation: nope" in out


# ---------------------------------------------------------------------------
# backward compat — check: op reads from ops section
# ---------------------------------------------------------------------------

class TestCheckFromOps:
    """check: op resolves from ops section in .supertool.json."""

    def test_check_reads_from_ops_section(self, tmp_path: Path) -> None:
        """check:preset works when preset is defined in ops section."""
        f = tmp_path / "test.txt"
        f.write_text("hello\n")
        supertool._CONFIG = {
            "ops": {"lint": {"cmd": f"cat {{file}}"}}
        }
        out = supertool.dispatch(f"check:lint:{f}")
        assert "PASS" in out
        assert "hello" in out

    def test_check_unknown_errors_with_available(self) -> None:
        """Unknown preset lists available ops from .supertool.json."""
        supertool._CONFIG = {
            "ops": {"phpstan": {"cmd": "echo x"}, "phpmd": {"cmd": "echo y"}}
        }
        out = supertool.dispatch("check:unknown:file.php")
        assert "ERROR" in out
        assert "unknown" in out
        assert "phpstan" in out
        assert "phpmd" in out

    def test_check_no_ops_errors(self) -> None:
        """No ops section gives clear error."""
        supertool._CONFIG = {}
        out = supertool.dispatch("check:lint:file.php")
        assert "ERROR" in out
        assert "no ops defined" in out

    def test_direct_op_and_check_both_work(self) -> None:
        """phpstan:file and check:phpstan:file both resolve the same op."""
        supertool._CONFIG = {
            "ops": {"phpstan": {"cmd": "echo analysis"}}
        }
        direct = supertool.dispatch("phpstan:file.php")
        check = supertool.dispatch("check:phpstan:file.php")
        assert "analysis" in direct
        assert "analysis" in check


# ---------------------------------------------------------------------------
# main() integration with custom ops
# ---------------------------------------------------------------------------

class TestMainIntegration:
    """End-to-end tests through main()."""

    def test_main_with_custom_op(self, capsys, tmp_path: Path, monkeypatch) -> None:
        log_file = tmp_path / "calls.log"
        monkeypatch.setattr(supertool, "LOG_FILE", str(log_file))
        supertool._CONFIG = {
            "ops": {"hi": {"cmd": "echo hello"}}
        }
        ret = supertool.main(["hi:test"])
        captured = capsys.readouterr()
        assert ret == 0
        assert "hello" in captured.out
        # Log should record the custom op
        log_content = log_file.read_text()
        assert "hi:test" in log_content

    def test_main_with_alias(self, capsys, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "x.py"
        f.write_text("code\n")
        log_file = tmp_path / "calls.log"
        monkeypatch.setattr(supertool, "LOG_FILE", str(log_file))
        supertool._CONFIG = {
            "aliases": {"both": {"ops": [f"read:{f}", f"wc:{f}"]}}
        }
        ret = supertool.main([f"both:{f}"])
        captured = capsys.readouterr()
        assert ret == 0
        assert "code" in captured.out
        assert "both:" in log_file.read_text()

    def test_main_mixed_builtin_and_custom(self, capsys, tmp_path: Path, monkeypatch) -> None:
        """A single call can mix built-in ops and custom ops."""
        f = tmp_path / "x.py"
        f.write_text("data\n")
        log_file = tmp_path / "calls.log"
        monkeypatch.setattr(supertool, "LOG_FILE", str(log_file))
        supertool._CONFIG = {
            "ops": {"ping": {"cmd": "echo pong"}}
        }
        ret = supertool.main([f"read:{f}", "ping:x"])
        captured = capsys.readouterr()
        assert ret == 0
        assert "data" in captured.out
        assert "pong" in captured.out
        assert "ops=2" in log_file.read_text()
