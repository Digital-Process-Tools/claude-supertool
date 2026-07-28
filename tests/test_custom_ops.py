"""Tests for custom ops and aliases in .supertool.json config."""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Placeholder substitution — values must never be re-scanned (#377)
# ---------------------------------------------------------------------------

class TestPlaceholderInjection:
    """An argument VALUE containing a placeholder token stays literal.

    Substitution used to run as a chain of str.replace calls, so a token that
    appeared inside an already-substituted value was expanded by a later pass.
    A commit message mentioning {argjoin} shattered its own shell quoting and
    leaked words into the command's argv.
    """

    @staticmethod
    def _script(tmp_path: Path, name: str, body: str) -> str:
        """Write a helper script and return a cmd-template-safe path.

        Forward slashes + shlex.quote: a raw Windows path would lose its
        backslashes to the shlex.split the cmd template goes through.
        """
        script = tmp_path / name
        script.write_text(body)
        return shlex.quote(script.as_posix())

    @classmethod
    def _echo_argv(cls, tmp_path: Path) -> str:
        """Script printing each argv entry on its own <N>=<value> line."""
        return cls._script(
            tmp_path, "argv.py",
            "import sys" + chr(10) +
            "for i, a in enumerate(sys.argv[1:], 1): print(str(i) + '=' + a)",
        )

    def test_argjoin_token_in_value_is_not_expanded(self, tmp_path: Path) -> None:
        script = self._echo_argv(tmp_path)
        supertool._CONFIG = {"ops": {"say": {"cmd": f"{{python}} {script} {{args}}"}}}
        value = "docs about {argjoin} tokens"
        result = supertool._resolve_custom_op("say", ["say", value])
        assert result is not None
        assert f"1={value}" in result
        assert "2=" not in result

    def test_args_token_in_value_is_not_expanded(self, tmp_path: Path) -> None:
        script = self._echo_argv(tmp_path)
        supertool._CONFIG = {"ops": {"say": {"cmd": f"{{python}} {script} {{argjoin}}"}}}
        result = supertool._resolve_custom_op("say", ["say", "mentions {args} here", "second"])
        assert result is not None
        assert "1=mentions {args} here:::second" in result
        assert "2=" not in result

    def test_file_token_in_value_is_not_expanded(self, tmp_path: Path) -> None:
        script = self._echo_argv(tmp_path)
        supertool._CONFIG = {"ops": {"say": {"cmd": f"{{python}} {script} {{args}}"}}}
        value = "talks about {file} and {dir}"
        result = supertool._resolve_custom_op("say", ["say", value])
        assert result is not None
        assert f"1={value}" in result

    def test_python_token_in_value_is_not_expanded(self, tmp_path: Path) -> None:
        """Guard, not a regression repro: under the old chain {python} was
        substituted FIRST, so a value arriving later via {args} was never
        rescanned by it — only tokens substituted AFTER the carrier were
        vulnerable. Kept so a future rewrite that reintroduces sequential
        substitution fails here too, not just on the {argjoin} cases."""
        script = self._echo_argv(tmp_path)
        supertool._CONFIG = {"ops": {"say": {"cmd": f"{{python}} {script} {{args}}"}}}
        value = "mentions {python} in prose"
        result = supertool._resolve_custom_op("say", ["say", value])
        assert result is not None
        assert f"1={value}" in result
        assert "2=" not in result

    def test_value_with_placeholder_keeps_argv_intact(self, tmp_path: Path) -> None:
        """The whole value stays ONE argv entry — the quoting-shatter regression."""
        script = self._script(tmp_path, "argc.py",
                              "import sys; print('ARGC', len(sys.argv) - 1)")
        supertool._CONFIG = {"ops": {"argc": {"cmd": f"{{python}} {script} {{args}}"}}}
        result = supertool._resolve_custom_op("argc", ["argc", "subject {argjoin} more words"])
        assert result is not None
        assert "ARGC 1" in result

    def test_alias_placeholder_in_value_is_not_expanded(self, tmp_path: Path) -> None:
        f = tmp_path / "{args}.txt"
        f.write_text("ok")
        supertool._CONFIG = {
            "ops": {"say": {"cmd": "echo {args}"}},
            "aliases": {"twice": {"ops": ["say:{file}"]}},
        }
        result = supertool._resolve_alias("twice", ["twice", str(f)])
        assert result is not None
        assert "{args}" in result


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

    def test_timeout_keeps_partial_output(self, tmp_path: Path) -> None:
        """A killed op's output is evidence, not noise — it must survive (#399).

        Discarding it left the caller with a bare `FAIL (timeout …)` for an op
        whose own receipt already said the work had landed.
        """
        script = tmp_path / "slow.py"
        script.write_text(
            "import sys, time" + chr(10) +
            "print('Status: pushed')" + chr(10) +
            "sys.stdout.flush()" + chr(10) +
            "time.sleep(10)" + chr(10)
        )
        supertool._CONFIG = {"ops": {"slow": {
            "cmd": "{python} " + shlex.quote(script.as_posix()), "timeout": 2}}}
        result = supertool._resolve_custom_op("slow", ["slow"])
        assert result is not None
        lines = result.splitlines()
        assert lines[0].startswith("FAIL (timeout ")
        assert lines[1] == "--- partial output before timeout ---"
        assert lines[2] == "Status: pushed"

    def test_timeout_without_output_adds_no_partial_block(self, tmp_path: Path) -> None:
        """Nothing printed → no empty 'partial output' header."""
        supertool._CONFIG = {"ops": {"quiet": {"cmd": "sleep 10", "timeout": 1}}}
        result = supertool._resolve_custom_op("quiet", ["quiet"])
        assert result is not None
        lines = result.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("FAIL (timeout ")

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

    def test_restart_mcp_true_stops_all_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """restartMcp: true stops every server in the mcp block after a successful cmd."""
        stopped: list[str] = []
        monkeypatch.setattr(supertool, "_mcp_stop_server", lambda name: stopped.append(name))
        monkeypatch.setattr(supertool, "_mcp_specs", {"phpstan-warm": {}, "rector-warm": {}})
        supertool._CONFIG = {"ops": {"clean": {"cmd": "echo ok", "restartMcp": True}}}
        result = supertool._resolve_custom_op("clean", ["clean", "x"])
        assert result is not None
        assert "PASS" in result
        assert sorted(stopped) == ["phpstan-warm", "rector-warm"]
        assert "restarted 2 daemon(s)" in result

    def test_restart_mcp_list_stops_only_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """restartMcp: [names] stops only the listed servers."""
        stopped: list[str] = []
        monkeypatch.setattr(supertool, "_mcp_stop_server", lambda name: stopped.append(name))
        monkeypatch.setattr(supertool, "_mcp_specs", {"phpstan-warm": {}, "rector-warm": {}})
        supertool._CONFIG = {"ops": {"clean": {"cmd": "echo ok", "restartMcp": ["rector-warm"]}}}
        result = supertool._resolve_custom_op("clean", ["clean", "x"])
        assert result is not None
        assert stopped == ["rector-warm"]
        assert "phpstan-warm" not in stopped

    def test_restart_mcp_single_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """restartMcp: "name" (a single string) stops just that server."""
        stopped: list[str] = []
        monkeypatch.setattr(supertool, "_mcp_stop_server", lambda name: stopped.append(name))
        monkeypatch.setattr(supertool, "_mcp_specs", {"phpstan-warm": {}, "rector-warm": {}})
        supertool._CONFIG = {"ops": {"clean": {"cmd": "echo ok", "restartMcp": "phpstan-warm"}}}
        result = supertool._resolve_custom_op("clean", ["clean", "x"])
        assert result is not None
        assert stopped == ["phpstan-warm"]
        assert "restarted 1 daemon(s)" in result

    def test_restart_mcp_unknown_name_not_counted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A name absent from the mcp block is reported as ignored, not restarted."""
        stopped: list[str] = []
        monkeypatch.setattr(supertool, "_mcp_stop_server", lambda name: stopped.append(name))
        monkeypatch.setattr(supertool, "_mcp_specs", {"phpstan-warm": {}})
        supertool._CONFIG = {"ops": {"clean": {"cmd": "echo ok", "restartMcp": ["phpstan-warm", "typo-warm"]}}}
        result = supertool._resolve_custom_op("clean", ["clean", "x"])
        assert result is not None
        assert stopped == ["phpstan-warm"]
        assert "restarted 1 daemon(s)" in result
        assert "unknown server(s) ignored (typo-warm)" in result

    def test_restart_mcp_skipped_on_cmd_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed cmd must not restart any daemon."""
        stopped: list[str] = []
        monkeypatch.setattr(supertool, "_mcp_stop_server", lambda name: stopped.append(name))
        monkeypatch.setattr(supertool, "_mcp_specs", {"phpstan-warm": {}})
        supertool._CONFIG = {"ops": {"clean": {"cmd": "false", "restartMcp": True}}}
        result = supertool._resolve_custom_op("clean", ["clean", "x"])
        assert result is not None
        assert "FAIL" in result
        assert stopped == []

    def test_no_restart_mcp_leaves_daemons(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An op without restartMcp never touches the daemons."""
        stopped: list[str] = []
        monkeypatch.setattr(supertool, "_mcp_stop_server", lambda name: stopped.append(name))
        monkeypatch.setattr(supertool, "_mcp_specs", {"phpstan-warm": {}})
        supertool._CONFIG = {"ops": {"plain": {"cmd": "echo ok"}}}
        result = supertool._resolve_custom_op("plain", ["plain", "x"])
        assert result is not None
        assert "PASS" in result
        assert stopped == []

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

    def test_arg_placeholder_replaced(self) -> None:
        """The {arg} placeholder is replaced with the raw argument value."""
        supertool._CONFIG = {
            "ops": {"lookup": {"cmd": "echo {arg}"}}
        }
        result = supertool._resolve_custom_op("lookup", ["lookup", "12345"])
        assert result is not None
        assert "PASS" in result
        assert "12345" in result

    def test_arg_placeholder_shell_safe(self) -> None:
        """The {arg} placeholder is shell-quoted for safety."""
        supertool._CONFIG = {
            "ops": {"unsafe": {"cmd": "echo {arg}"}}
        }
        result = supertool._resolve_custom_op("unsafe", ["unsafe", "hello world"])
        assert result is not None
        assert "PASS" in result

    def test_args_placeholder_all_parts(self) -> None:
        """The {args} placeholder expands all arguments."""
        supertool._CONFIG = {
            "ops": {"multi": {"cmd": "echo {args}"}}
        }
        result = supertool._resolve_custom_op("multi", ["multi", "12345", "200"])
        assert result is not None
        assert "PASS" in result
        assert "12345" in result
        assert "200" in result

    def test_args_placeholder_single_arg(self) -> None:
        """The {args} placeholder works with a single argument too."""
        supertool._CONFIG = {
            "ops": {"single": {"cmd": "echo {args}"}}
        }
        result = supertool._resolve_custom_op("single", ["single", "42"])
        assert result is not None
        assert "42" in result

    def test_args_placeholder_empty(self) -> None:
        """The {args} placeholder is empty when no arguments given."""
        supertool._CONFIG = {
            "ops": {"noarg": {"cmd": "echo start {args} end"}}
        }
        result = supertool._resolve_custom_op("noarg", ["noarg"])
        assert result is not None
        assert "start  end" in result or "start end" in result

    def test_argjoin_placeholder_rejoins_with_triple_colon(self) -> None:
        """The {argjoin} placeholder rejoins all parts with ':::' as one shell-quoted arg."""
        supertool._CONFIG = {
            "ops": {"echo3": {"cmd": "echo {argjoin}"}}
        }
        result = supertool._resolve_custom_op("echo3", ["echo3", "A", "B", "C"])
        assert result is not None
        assert "PASS" in result
        assert "A:::B:::C" in result

    def test_argjoin_placeholder_single_part(self) -> None:
        """The {argjoin} placeholder works with a single part (no ':::' inserted)."""
        supertool._CONFIG = {
            "ops": {"echo1": {"cmd": "echo {argjoin}"}}
        }
        result = supertool._resolve_custom_op("echo1", ["echo1", "solo"])
        assert result is not None
        assert "solo" in result
        assert ":::" not in result

    def test_argjoin_placeholder_empty(self) -> None:
        """The {argjoin} placeholder is empty when no arguments given."""
        supertool._CONFIG = {
            "ops": {"emptyargjoin": {"cmd": "echo start{argjoin}end"}}
        }
        result = supertool._resolve_custom_op("emptyargjoin", ["emptyargjoin"])
        assert result is not None
        # Empty argjoin shell-quotes to '' — between start and end we get start''end
        assert "startend" in result or "start''end" in result

    def test_extra_config_keys_as_env_vars(self) -> None:
        """Extra keys in op config are passed as SUPERTOOL_ env vars."""
        supertool._CONFIG = {
            "ops": {"tool": {
                "cmd": "echo $SUPERTOOL_LINES $SUPERTOOL_MODE",
                "lines": 200,
                "mode": "verbose",
            }}
        }
        result = supertool._resolve_custom_op("tool", ["tool", "x"])
        assert result is not None
        assert "200" in result
        assert "verbose" in result

    def test_nonscalar_config_value_passed_as_json(self) -> None:
        """List/dict config values are JSON-encoded (so presets can json.loads them),
        not str()'d into a Python repr that json.loads can't parse."""
        import json as _json
        supertool._CONFIG = {
            "ops": {"tool": {
                "cmd": "env",
                "job_patterns": [{"job": "rector", "patterns": ["applied_rectors"]}],
            }}
        }
        result = supertool._resolve_custom_op("tool", ["tool", "x"])
        assert result is not None
        line = next(ln for ln in result.splitlines() if ln.startswith("SUPERTOOL_JOB_PATTERNS="))
        payload = line.split("=", 1)[1]
        # Must be valid JSON, not a Python repr with single quotes
        parsed = _json.loads(payload)
        assert parsed == [{"job": "rector", "patterns": ["applied_rectors"]}]

    def test_reserved_keys_not_in_env(self) -> None:
        """Reserved keys (cmd, timeout, description, etc.) are NOT passed as env vars."""
        supertool._CONFIG = {
            "ops": {"tool": {
                "cmd": "env",  # shell-less: dump all env, assertions filter via substring
                "timeout": 10,
                "description": "test",
                "custom_key": "yes",
            }}
        }
        result = supertool._resolve_custom_op("tool", ["tool", "x"])
        assert result is not None
        assert "SUPERTOOL_CUSTOM_KEY=yes" in result
        assert "SUPERTOOL_CMD" not in result
        assert "SUPERTOOL_TIMEOUT" not in result
        assert "SUPERTOOL_DESCRIPTION" not in result

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

    def test_arg_placeholder_in_alias(self) -> None:
        """The {arg} placeholder is replaced in alias ops."""
        supertool._CONFIG = {
            "ops": {"show": {"cmd": "echo {arg}"}},
            "aliases": {"lookup": {"ops": ["show:{arg}"]}}
        }
        result = supertool._resolve_alias("lookup", ["lookup", "42"])
        assert result is not None
        assert "42" in result

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
        log_content = log_file.read_text(encoding="utf-8")
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
        assert "both:" in log_file.read_text(encoding="utf-8")

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
        assert "ops=2" in log_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# {arg} vs {args} placeholder behavior — colon-bearing inputs
#
# Bug surfaced 2026-05-02: devto_react with `{arg}` template received only the
# first ':'-separated chunk of the input. URLs are now kept whole by
# _split_arg's URL-greedy absorb (PR #7), but other colon-bearing inputs
# (ratios, ISO timestamps, namespaced identifiers) still split. {args} is the
# safe template; {arg} only handles single-token inputs.
# ---------------------------------------------------------------------------

class TestArgPlaceholderColonHandling:

    def test_arg_placeholder_only_passes_first_chunk(self, tmp_path: Path) -> None:
        """`{arg}` substitutes parts[1] only — colons in the input split it."""
        captured = tmp_path / "out.txt"
        # Script writes ALL its argv (excluding final output-file path) to disk.
        script = tmp_path / "echo_argv.py"
        script.write_text(
            "import sys; open(sys.argv[-1], 'w').write(repr(sys.argv[1:-1]))\n"
        )
        supertool._CONFIG = {
            "ops": {"echo": {"cmd": f"{{python}} {script.as_posix()} {{arg}} {captured.as_posix()}"}}
        }
        # Input: 'echo:16:9' tokenizes to parts = ['echo', '16', '9'].
        # With {arg}, only '16' goes through (ratio is NOT a URL — no greedy absorb).
        result = supertool.dispatch("echo:16:9")
        assert "PASS" in result
        argv = eval(captured.read_text(encoding="utf-8"))
        assert argv == ["16"], f"expected ['16'], got {argv!r}"

    def test_args_placeholder_passes_all_chunks(self, tmp_path: Path) -> None:
        """`{args}` substitutes parts[1:] joined by space — all chunks survive."""
        captured = tmp_path / "out.txt"
        script = tmp_path / "echo_argv.py"
        script.write_text(
            "import sys; open(sys.argv[-1], 'w').write(repr(sys.argv[1:-1]))\n"
        )
        supertool._CONFIG = {
            "ops": {"echo": {"cmd": f"{{python}} {script.as_posix()} {{args}} {captured.as_posix()}"}}
        }
        result = supertool.dispatch("echo:16:9")
        assert "PASS" in result
        argv = eval(captured.read_text(encoding="utf-8"))
        # All ':'-tokenized parts arrive as separate argv entries.
        assert argv == ["16", "9"], f"got {argv!r}"

    def test_args_placeholder_rejoinable_by_script(self, tmp_path: Path) -> None:
        """End-to-end: with {args}, a script that does ':'.join(argv[1:]) gets
        the original colon-bearing input back — pattern that
        preset/devto/comment.py uses to survive supertool's ':' tokenizer."""
        captured = tmp_path / "out.txt"
        script = tmp_path / "rejoin.py"
        script.write_text(
            "import sys\n"
            "rejoined = ':'.join(sys.argv[1:-1])\n"
            "open(sys.argv[-1], 'w').write(rejoined)\n"
        )
        supertool._CONFIG = {
            "ops": {"echo": {"cmd": f"{{python}} {script.as_posix()} {{args}} {captured.as_posix()}"}}
        }
        # Compound colon-separated string (NOT a URL — _split_arg won't absorb).
        compound = "16:9:hd"
        result = supertool.dispatch(f"echo:{compound}")
        assert "PASS" in result
        assert captured.read_text(encoding="utf-8") == compound

    def test_args_placeholder_url_arrives_whole(self, tmp_path: Path) -> None:
        """URLs are absorbed by _split_arg (PR #7) so they arrive as a single
        argv entry. Documents the post-PR-#7 behavior."""
        captured = tmp_path / "out.txt"
        script = tmp_path / "echo_argv.py"
        script.write_text(
            "import sys; open(sys.argv[-1], 'w').write(repr(sys.argv[1:-1]))\n"
        )
        supertool._CONFIG = {
            "ops": {"echo": {"cmd": f"{{python}} {script.as_posix()} {{args}} {captured.as_posix()}"}}
        }
        url = "https://dev.to/marcosomma/the-real-token-economy-3j3e"
        result = supertool.dispatch(f"echo:{url}")
        assert "PASS" in result
        argv = eval(captured.read_text(encoding="utf-8"))
        assert argv == [url], f"URL should arrive whole, got {argv!r}"

    def test_args_placeholder_with_pipe_separator(self, tmp_path: Path) -> None:
        """`|` is NOT a tokenizer separator (only `:` is) — pipe-separated
        args arrive as a single chunk, preserving 'aid|MSG|parent' shape."""
        captured = tmp_path / "out.txt"
        script = tmp_path / "echo_argv.py"
        script.write_text(
            "import sys; open(sys.argv[-1], 'w').write(repr(sys.argv[1:-1]))\n"
        )
        supertool._CONFIG = {
            "ops": {"echo": {"cmd": f"{{python}} {script.as_posix()} {{args}} {captured.as_posix()}"}}
        }
        result = supertool.dispatch("echo:1234|hello world|99")
        assert "PASS" in result
        argv = eval(captured.read_text(encoding="utf-8"))
        assert argv == ["1234|hello world|99"]

    def test_args_placeholder_with_iso_timestamp(self, tmp_path: Path) -> None:
        """ISO timestamps (HH:MM:SSZ) are split by `:` — {args} must rejoin
        them. Same pattern devto_status_since uses."""
        captured = tmp_path / "out.txt"
        script = tmp_path / "rejoin.py"
        script.write_text(
            "import sys\n"
            "rejoined = ':'.join(sys.argv[1:-1])\n"
            "open(sys.argv[-1], 'w').write(rejoined)\n"
        )
        supertool._CONFIG = {
            "ops": {"echo": {"cmd": f"{{python}} {script.as_posix()} {{args}} {captured.as_posix()}"}}
        }
        ts = "2026-05-02T15:01:45+00:00"
        result = supertool.dispatch(f"echo:{ts}")
        assert "PASS" in result
        assert captured.read_text(encoding="utf-8") == ts


class TestShellMetacharsNotExecuted:
    """Regression: a malicious cmd template containing ; | && $() does NOT
    invoke the shell. Templates are tokenized via shlex.split and run with
    shell=False, so metachars become literal argv tokens. Closes #145."""

    def test_semicolon_chain_not_executed(self, tmp_path: Path) -> None:
        marker = tmp_path / "pwned"
        supertool._CONFIG = {
            "ops": {"x": {"cmd": f"echo {{file}}; touch {marker}"}}
        }
        supertool._resolve_custom_op("x", ["x", "safe.txt"])
        assert not marker.exists(), "shell metachars must not chain commands"

    def test_pipe_not_executed(self, tmp_path: Path) -> None:
        marker = tmp_path / "piped"
        supertool._CONFIG = {
            "ops": {"x": {"cmd": f"echo {{file}} | tee {marker}"}}
        }
        supertool._resolve_custom_op("x", ["x", "safe.txt"])
        assert not marker.exists(), "pipe must not be interpreted by shell"

    def test_command_substitution_not_executed(self, tmp_path: Path) -> None:
        marker = tmp_path / "subst"
        supertool._CONFIG = {
            "ops": {"x": {"cmd": f"echo $(touch {marker})"}}
        }
        supertool._resolve_custom_op("x", ["x", "safe.txt"])
        assert not marker.exists(), "$() must not be interpreted by shell"

    def test_backtick_not_executed(self, tmp_path: Path) -> None:
        marker = tmp_path / "backtick"
        supertool._CONFIG = {
            "ops": {"x": {"cmd": f"echo `touch {marker}`"}}
        }
        supertool._resolve_custom_op("x", ["x", "safe.txt"])
        assert not marker.exists(), "backticks must not be interpreted by shell"

    def test_redirect_not_executed(self, tmp_path: Path) -> None:
        marker = tmp_path / "redir"
        supertool._CONFIG = {
            "ops": {"x": {"cmd": f"echo data > {marker}"}}
        }
        supertool._resolve_custom_op("x", ["x", "safe.txt"])
        assert not marker.exists(), "> must not redirect via shell"

    def test_and_chain_not_executed(self, tmp_path: Path) -> None:
        marker = tmp_path / "and"
        supertool._CONFIG = {
            "ops": {"x": {"cmd": f"true && touch {marker}"}}
        }
        supertool._resolve_custom_op("x", ["x", "safe.txt"])
        assert not marker.exists(), "&& must not be interpreted by shell"

    def test_or_chain_not_executed(self, tmp_path: Path) -> None:
        marker = tmp_path / "or"
        supertool._CONFIG = {
            "ops": {"x": {"cmd": f"false || touch {marker}"}}
        }
        supertool._resolve_custom_op("x", ["x", "safe.txt"])
        assert not marker.exists(), "|| must not be interpreted by shell"

    def test_args_placeholder_expands_to_multiple_argv(self, tmp_path: Path) -> None:
        """{args} expands to N argv tokens after shlex.split."""
        captured = tmp_path / "argv.txt"
        script = tmp_path / "dump_argv.py"
        script.write_text(
            "import sys\n"
            f"open({str(captured)!r}, 'w').write(repr(sys.argv[1:]))\n"
        )
        supertool._CONFIG = {
            "ops": {"echo": {"cmd": f"{{python}} {script.as_posix()} {{args}}"}}
        }
        # Three separate args via `:` delimiter → three argv elements
        result = supertool.dispatch("echo:alpha:beta:gamma")
        assert "PASS" in result
        argv = eval(captured.read_text(encoding="utf-8"))
        assert argv == ["alpha", "beta", "gamma"], (
            f"{{args}} did not expand to N argv tokens: {argv!r}"
        )

    def test_arg_value_with_space_survives_shlex_split(self, tmp_path: Path) -> None:
        """A single arg value containing a space stays one argv token.

        Pass via `argjoin` (which keeps the raw value with `:::` separator,
        so embedded `:` and ` ` in the value survive both dispatch parsing
        and shlex.split downstream).
        """
        captured = tmp_path / "argv.txt"
        script = tmp_path / "dump_argv.py"
        script.write_text(
            "import sys\n"
            f"open({str(captured)!r}, 'w').write(repr(sys.argv[1:]))\n"
        )
        supertool._CONFIG = {
            "ops": {"echo": {"cmd": f"{{python}} {script.as_posix()} {{arg}}"}}
        }
        # Single arg with embedded space
        result = supertool._resolve_custom_op("echo", ["echo", "hello world"])
        assert result is not None and "PASS" in result
        argv = eval(captured.read_text(encoding="utf-8"))
        assert argv == ["hello world"], (
            f"value with space did not survive shlex.split: {argv!r}"
        )

    def test_unknown_env_var_left_literal(self, tmp_path: Path) -> None:
        """$UNDEFINED stays literal — not replaced with empty string.

        Shell behaviour: $UNDEFINED → '' (silent). Supertool's regex sub leaves
        the literal `$UNDEFINED` instead, which is safer (no silent data loss).
        """
        captured = tmp_path / "out.txt"
        script = tmp_path / "echo_argv.py"
        script.write_text(
            "import sys\n"
            f"open({str(captured)!r}, 'w').write(repr(sys.argv[1:]))\n"
        )
        supertool._CONFIG = {
            "ops": {"x": {"cmd": f"{{python}} {script.as_posix()} $UNDEFINED_VAR_XYZ"}}
        }
        result = supertool._resolve_custom_op("x", ["x", "unused.txt"])
        assert result is not None and "PASS" in result
        argv = eval(captured.read_text(encoding="utf-8"))
        assert argv == ["$UNDEFINED_VAR_XYZ"], (
            f"undefined var should stay literal, got: {argv!r}"
        )


class TestFormatterMetacharsNotExecuted:
    """Same regression test for _formatter_run_one — defence in depth.
    Validator gets covered by integration via _run_with_validators."""

    def test_formatter_semicolon_not_executed(self, tmp_path: Path) -> None:
        f = tmp_path / "x.json"
        f.write_text("{}\n")
        marker = tmp_path / "fmt_pwned"
        spec = {"cmd": f"true; touch {marker}", "timeout": 5}
        supertool._formatter_run_one("fmt", spec, str(f))
        assert not marker.exists(), "formatter shell metachars must not chain"
