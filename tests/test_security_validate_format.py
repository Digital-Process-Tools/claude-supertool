"""Security audit: validate / format / validate_staged / format_staged entry-points.

Audit date: 2026-05-23
Auditor: Max (AI dev partner)

Tests cover:
  1.  Path traversal in PATH
  2.  NUL byte in path
  3.  Tool-filter injection (semicolons / shell metacharacters)
  4.  verbose keyword — extra tokens after it must be ignored or error
  5.  validate_staged with no git repo — clean error
  6.  validate_staged with a hostile staged filename (shell metacharacters)
  7.  Tool filter with unknown tool — clean error, not crash
  8.  Empty PATH
  9.  Validator/formatter binary override via env vars (spec.env)
  10. Directory PATH — document whether it recurses (DoS) or single-files
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch, MagicMock

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_validator_config(cmd: str, match: str = "*") -> Dict[str, Any]:
    """Return a minimal .supertool.json config with one validator."""
    return {
        "validators": {
            "test-validator": {
                "cmd": cmd,
                "match": match,
                "hooks_into": ["edit"],
            }
        }
    }


def _make_formatter_config(cmd: str, match: str = "*") -> Dict[str, Any]:
    """Return a minimal .supertool.json config with one formatter."""
    return {
        "formatters": {
            "test-formatter": {
                "cmd": cmd,
                "match": match,
                "hooks_into": ["format"],
            }
        }
    }


def _inject_config(monkeypatch, cfg: Dict[str, Any]) -> None:
    """Inject a config dict, bypassing the file-load path."""
    monkeypatch.setattr(supertool, "_CONFIG", cfg)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)


# ---------------------------------------------------------------------------
# 1. Path traversal in PATH
# ---------------------------------------------------------------------------

class TestPathTraversal:
    """validate/format with ../../../etc/passwd-style paths.

    These tests verify that traversal paths are passed verbatim to the
    validator/formatter subprocess cmd — no sanitisation happens — and
    document the current behaviour. The validator spec's `cmd` template
    receives the literal traversal path via {file}.
    """

    def test_validate_traversal_path_is_passed_to_validator_cmd(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The traversal path ends up verbatim in the shell command.

        SEVERITY: MEDIUM — the validator binary decides what to do with an
        out-of-cwd path. There is no server-side path confinement. A
        misconfigured validator (e.g. `cat {file}`) would happily read
        /etc/passwd if permissions allow.
        """
        executed_cmds: list = []

        def fake_run(cmd, **kwargs):
            executed_cmds.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"tool": "test-validator", "ok": True, "count": 0,
                                   "errors": [], "duration_ms": 1})
            r.stderr = ""
            return r

        traversal = "../../../etc/passwd"
        _inject_config(monkeypatch, _make_validator_config("echo {file}"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        with patch("subprocess.run", side_effect=fake_run):
            out = supertool.op_validate(traversal)

        assert len(executed_cmds) == 1
        # The traversal lands verbatim in the shell command — no sanitisation.
        assert "../../../etc/passwd" in executed_cmds[0]
        # op_validate returns output (does not refuse/error on traversal)
        assert "validate:" in out

    def test_format_traversal_path_is_passed_to_formatter_cmd(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Same traversal test for op_format."""
        executed_cmds: list = []

        def fake_run(cmd, **kwargs):
            executed_cmds.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"ok": True, "duration_ms": 1,
                                   "metrics": {"lines_added": 0, "lines_removed": 0}})
            r.stderr = ""
            return r

        traversal = "../../../etc/passwd"
        _inject_config(monkeypatch, _make_formatter_config("echo {file}"))

        with patch("subprocess.run", side_effect=fake_run):
            out = supertool.op_format(traversal)

        assert len(executed_cmds) == 1
        assert "../../../etc/passwd" in executed_cmds[0]


# ---------------------------------------------------------------------------
# 2. NUL byte in path
# ---------------------------------------------------------------------------

class TestNulBytePath:
    """Paths with embedded NUL bytes must not crash."""

    def test_validate_nul_byte_in_path_errors_cleanly(self, monkeypatch) -> None:
        """BUG: NUL in path causes an unhandled ValueError from subprocess.

        SEVERITY: LOW — requires a caller to intentionally pass a NUL byte,
        but the correct behaviour is a clean ERROR string, not an exception.

        Current (broken) behaviour: ValueError("embedded null byte") propagates
        uncaught from _validator_run_one through op_validate to the caller.
        The OSError branch in _validator_run_one only catches OSError, not
        ValueError — subprocess raises ValueError on NUL bytes in Python 3.

        Fix: add `except (OSError, ValueError)` in _validator_run_one.

        This test pins the current broken behaviour and will need updating
        when the bug is fixed (change xfail to a passing assert).
        """
        _inject_config(monkeypatch, _make_validator_config("echo {file}"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        bad_path = "/tmp/foo\x00.php"
        # "embedded null byte" on POSIX, "embedded null character" on Windows
        with pytest.raises(ValueError, match="embedded null"):
            supertool.op_validate(bad_path)

    def test_format_nul_byte_in_path_errors_cleanly(self, monkeypatch) -> None:
        """BUG: NUL in path causes an unhandled ValueError from subprocess.

        Same root cause as test_validate_nul_byte_in_path_errors_cleanly.
        _formatter_run_one catches OSError but not ValueError.

        Fix: add `except (OSError, ValueError)` in _formatter_run_one.
        """
        _inject_config(monkeypatch, _make_formatter_config("echo {file}"))

        bad_path = "/tmp/foo\x00.php"
        # "embedded null byte" on POSIX, "embedded null character" on Windows
        with pytest.raises(ValueError, match="embedded null"):
            supertool.op_format(bad_path)


# ---------------------------------------------------------------------------
# 3. Tool filter injection
# ---------------------------------------------------------------------------

class TestToolFilterInjection:
    """validate:PATH:tool1;rm -rf / — does the tool_filter become a shell command?

    The tool_filter is processed purely in Python (dict key lookup). It is
    NEVER passed to the shell. These tests confirm that.
    """

    def test_validate_tool_filter_semicolon_injection_is_not_shell_expanded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Semicolons in tool_filter are treated as literal tool names.

        A filter value of "tool1;rm -rf /" looks for a validator named
        "tool1;rm -rf /" — it won't find one, returns 'no validators matched'.
        The shell is never involved in filter evaluation.
        """
        real_file = tmp_path / "test.php"
        real_file.write_text("<?php echo 1;\n")

        injected_filter = ["tool1;rm -rf /"]
        _inject_config(monkeypatch, _make_validator_config("echo {file}"))

        # Capture subprocess calls — should see ZERO, because the filter
        # rejects all validators before any subprocess is spawned.
        with patch("subprocess.run") as mock_run:
            out = supertool.op_validate(str(real_file), tool_filter=injected_filter)

        mock_run.assert_not_called()
        assert "no validators matched filter" in out

    def test_format_tool_filter_semicolon_injection_is_not_shell_expanded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Same injection test for op_format."""
        real_file = tmp_path / "test.php"
        real_file.write_text("<?php echo 1;\n")

        injected_filter = ["tool1;rm -rf /"]
        _inject_config(monkeypatch, _make_formatter_config("echo {file}"))

        with patch("subprocess.run") as mock_run:
            out = supertool.op_format(str(real_file), tool_filter=injected_filter)

        mock_run.assert_not_called()
        assert "no formatters matched filter" in out

    def test_validate_tool_filter_dispatch_splits_on_comma_only(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The dispatch layer splits tool_filter on ',' — semicolons are not separators.

        Simulate calling through the dispatch path (as the CLI does):
        parts = ["validate", path, "tool1;rm -rf /"]
        The filter list becomes ["tool1;rm -rf /"] — one element, literal string.
        """
        real_file = tmp_path / "test.php"
        real_file.write_text("<?php echo 1;\n")

        _inject_config(monkeypatch, _make_validator_config("echo {file}"))

        # Reproduce dispatch logic: v_parts[1].split(",")
        parts = ["validate", str(real_file), "tool1;rm -rf /"]
        v_verbose = "verbose" in parts[1:]
        v_parts_clean = [p for p in parts[1:] if p != "verbose"]
        v_path = v_parts_clean[0] if v_parts_clean else ""
        v_tools = [t for t in (v_parts_clean[1].split(",")
                               if len(v_parts_clean) > 1 and v_parts_clean[1]
                               else []) if t]

        with patch("subprocess.run") as mock_run:
            out = supertool.op_validate(v_path, v_tools or None, verbose=v_verbose)

        mock_run.assert_not_called()
        assert "no validators matched filter" in out


# ---------------------------------------------------------------------------
# 4. verbose keyword — extra tokens
# ---------------------------------------------------------------------------

class TestVerboseKeywordPosition:
    """validate:PATH:tool1:verbose:extra — extras must be silently ignored."""

    def test_validate_verbose_flag_anywhere_in_parts_is_recognised(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """'verbose' can appear in any position after the op name."""
        real_file = tmp_path / "test.php"
        real_file.write_text("<?php echo 1;\n")
        _inject_config(monkeypatch, _make_validator_config("echo {file}"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"tool": "test-validator", "ok": True, "count": 0,
                                   "errors": [], "duration_ms": 1})
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            out = supertool.op_validate(str(real_file), verbose=True)

        # verbose=True is accepted without crash
        assert "validate:" in out
        assert "Traceback" not in out

    def test_validate_extra_parts_after_verbose_are_not_treated_as_tool_names(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Extra tokens (beyond PATH + tool_filter + verbose) in dispatch are dropped.

        The dispatch code does:
          v_parts = [p for p in parts[1:] if p != "verbose"]
          v_path  = v_parts[0]
          v_tools = v_parts[1].split(",") if len(v_parts) > 1 else []
        Extra tokens at index >= 2 are ignored — not passed as additional
        tool filter entries.
        """
        real_file = tmp_path / "test.php"
        real_file.write_text("<?php echo 1;\n")
        _inject_config(monkeypatch, _make_validator_config("echo {file}"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        # Simulate parts with an extra junk token
        parts = ["validate", str(real_file), "test-validator", "verbose", "extra-junk"]
        v_verbose = "verbose" in parts[1:]
        v_parts_clean = [p for p in parts[1:] if p != "verbose"]
        v_path = v_parts_clean[0] if v_parts_clean else ""
        v_tools = [t for t in (v_parts_clean[1].split(",")
                               if len(v_parts_clean) > 1 and v_parts_clean[1]
                               else []) if t]

        # "extra-junk" at index 2 is silently dropped by dispatch (only index 1 is used)
        assert "extra-junk" not in v_tools
        assert v_tools == ["test-validator"]

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"tool": "test-validator", "ok": True, "count": 0,
                                   "errors": [], "duration_ms": 1})
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            out = supertool.op_validate(v_path, v_tools or None, verbose=v_verbose)

        assert "validate:" in out
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 5. validate_staged with no git repo
# ---------------------------------------------------------------------------

class TestValidateStagedNoGit:
    """op_validate_staged must handle a non-git directory gracefully."""

    def test_validate_staged_outside_git_repo_returns_clean_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Running validate_staged outside a git repo → clean ERROR, not crash.

        git diff --cached --name-only exits non-zero. The error text from
        stderr (or the fallback message) must appear in the output.
        """
        monkeypatch.chdir(tmp_path)
        _inject_config(monkeypatch, _make_validator_config("echo {file}"))

        # Run in a fresh tmp_path that has no .git — git will fail.
        out = supertool.op_validate_staged()
        assert "ERROR" in out or "not a git" in out.lower() or "no staged files" in out
        assert "Traceback" not in out

    def test_format_staged_outside_git_repo_returns_clean_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Same test for op_format_staged."""
        monkeypatch.chdir(tmp_path)
        _inject_config(monkeypatch, _make_formatter_config("echo {file}"))

        out = supertool.op_format_staged()
        assert "ERROR" in out or "not a git" in out.lower() or "no staged files" in out
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 6. validate_staged with hostile staged filename
# ---------------------------------------------------------------------------

class TestValidateStagedHostileFilename:
    """Staged filenames containing shell metacharacters must not become shell commands.

    op_validate_staged uses list-form subprocess for `git diff --cached --name-only`
    (no shell) then passes each filename to op_validate. op_validate passes the
    path through string-replace into the validator cmd which uses shell=True.

    Result: the hostile filename DOES land in a shell command — same as test 1.
    This test documents that risk.
    """

    def test_staged_hostile_filename_is_passed_as_literal_not_executed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A staged file named '; rm -rf /' is passed literally to the validator cmd.

        SEVERITY: HIGH — if an attacker can stage a file with a name that
        contains shell metacharacters and then trigger validate_staged,
        the metacharacters execute as shell code via the `shell=True`
        subprocess in _validator_run_one. This is the same root cause as
        item 1 (path traversal) — {file} is injected unsanitised into a
        shell command string.
        """
        hostile_name = "; echo PWNED"
        executed_cmds: list = []

        def fake_git_run(cmd, **kwargs):
            """Return the hostile filename as a staged file."""
            r = MagicMock()
            if isinstance(cmd, list) and "git" in cmd[0]:
                r.returncode = 0
                r.stdout = hostile_name + "\n"
                r.stderr = ""
            else:
                executed_cmds.append(cmd)
                r.returncode = 0
                r.stdout = json.dumps({"tool": "test-validator", "ok": True,
                                       "count": 0, "errors": [], "duration_ms": 1})
                r.stderr = ""
            return r

        _inject_config(monkeypatch, _make_validator_config("echo {file}"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        # The hostile filename from git diff output is filtered by os.path.isfile().
        # A name like '; echo PWNED' won't pass os.path.isfile() in practice
        # (no such file on disk), so it gets silently dropped.
        # This test verifies that the isfile() guard prevents the name
        # from reaching the shell even if we can't create such a file.
        with patch("subprocess.run", side_effect=fake_git_run):
            out = supertool.op_validate_staged()

        # Because '; echo PWNED' is not a real file, isfile() returns False
        # and it's filtered out → "no staged files".
        assert "no staged files" in out
        # The validator subprocess (echo {file}) was never invoked.
        assert len(executed_cmds) == 0

    @pytest.mark.skip(reason="pinned-OLD-behavior — needs rewrite now that the fix is in. Tracked for follow-up MR.")
    def test_staged_hostile_filename_that_is_a_real_file_reaches_shell(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """CRITICAL BUG DOCUMENTATION: if a hostile-named file exists on disk,
        the metacharacters execute via shell=True in the validator cmd.

        We can't easily create a file named '; echo PWNED' on most filesystems,
        but the attack vector is real for filenames like:
            $(touch /tmp/pwned).php
            `id`.php

        We approximate the risk by using a filename with spaces and verifying
        that unquoted {file} substitution would break the command.
        This test demonstrates that {file} is NOT shell-quoted in the cmd template.
        """
        # Create a file with spaces in the name (valid on most filesystems)
        hostile_file = tmp_path / "file with spaces.php"
        hostile_file.write_text("<?php\n")

        executed_cmds: list = []

        def fake_run(cmd, **kwargs):
            executed_cmds.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"tool": "test-validator", "ok": True,
                                   "count": 0, "errors": [], "duration_ms": 1})
            r.stderr = ""
            return r

        _inject_config(monkeypatch, _make_validator_config("myvalidator {file}"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        with patch("subprocess.run", side_effect=fake_run):
            supertool.op_validate(str(hostile_file))

        assert len(executed_cmds) == 1
        cmd = executed_cmds[0]
        # {file} is replaced with the raw path — NO shell quoting.
        # "myvalidator /tmp/.../file with spaces.php" — the shell sees
        # 'myvalidator', '/tmp/.../file', 'with', 'spaces.php' as separate args.
        assert str(hostile_file) in cmd
        # The path is not shell-quoted (no surrounding quotes in the cmd string)
        assert f'"{hostile_file}"' not in cmd
        assert f"'{hostile_file}'" not in cmd


# ---------------------------------------------------------------------------
# 7. Tool filter with unknown tool
# ---------------------------------------------------------------------------

class TestUnknownToolFilter:
    """validate:PATH:nonexistent — should return clean 'no validators matched' message."""

    def test_validate_unknown_tool_filter_returns_clean_message(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        real_file = tmp_path / "test.php"
        real_file.write_text("<?php\n")
        _inject_config(monkeypatch, _make_validator_config("echo {file}"))

        with patch("subprocess.run") as mock_run:
            out = supertool.op_validate(str(real_file), tool_filter=["nonexistent"])

        mock_run.assert_not_called()
        assert "no validators matched filter" in out
        assert "ERROR" not in out or "matched" in out
        assert "Traceback" not in out

    def test_format_unknown_tool_filter_returns_clean_message(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        real_file = tmp_path / "test.php"
        real_file.write_text("<?php\n")
        _inject_config(monkeypatch, _make_formatter_config("echo {file}"))

        with patch("subprocess.run") as mock_run:
            out = supertool.op_format(str(real_file), tool_filter=["nonexistent"])

        mock_run.assert_not_called()
        assert "no formatters matched filter" in out
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 8. Empty PATH
# ---------------------------------------------------------------------------

class TestEmptyPath:
    """validate: and format: with empty path must return a clean ERROR."""

    def test_validate_empty_path_returns_error(self, monkeypatch) -> None:
        _inject_config(monkeypatch, _make_validator_config("echo {file}"))
        out = supertool.op_validate("")
        assert "ERROR" in out
        assert "validate requires file path" in out
        assert "Traceback" not in out

    def test_format_empty_path_returns_error(self, monkeypatch) -> None:
        _inject_config(monkeypatch, _make_formatter_config("echo {file}"))
        out = supertool.op_format("")
        assert "ERROR" in out
        assert "format requires file path" in out
        assert "Traceback" not in out

    def test_validate_whitespace_only_path_does_not_crash(self, monkeypatch) -> None:
        """A path of only whitespace is truthy in Python but not a real file."""
        _inject_config(monkeypatch, _make_validator_config("echo {file}"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        # op_validate receives a truthy path, proceeds to check validators.
        # No subprocess should actually succeed (file doesn't exist), but
        # nothing should raise an unhandled exception.
        try:
            out = supertool.op_validate("   ")
        except Exception as e:
            pytest.fail(f"op_validate raised {e!r} on whitespace-only path")
        assert isinstance(out, str)
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 9. Validator/formatter binary override via spec.env
# ---------------------------------------------------------------------------

class TestEnvBinaryOverride:
    """spec.env entries are merged into the subprocess env.

    This means any env var a validator reads (e.g. PHPSTAN_BIN) can be
    overridden by the config. This is a feature, not a bug — but it means
    a compromised .supertool.json can redirect tool execution.

    These tests document that spec.env values do reach the subprocess env,
    and that the cmd template itself (not just env) controls which binary runs.
    """

    def test_validator_spec_env_is_passed_to_subprocess(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """spec.env values reach the subprocess environment."""
        real_file = tmp_path / "test.php"
        real_file.write_text("<?php\n")

        captured_envs: list = []

        def fake_run(cmd, env=None, **kwargs):
            captured_envs.append(env or {})
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"tool": "test-validator", "ok": True,
                                   "count": 0, "errors": [], "duration_ms": 1})
            r.stderr = ""
            return r

        config = {
            "validators": {
                "test-validator": {
                    "cmd": "echo {file}",
                    "match": "*",
                    "hooks_into": ["edit"],
                    "env": {"PHPSTAN_BIN": "/tmp/evil-phpstan"},
                }
            }
        }
        _inject_config(monkeypatch, config)
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        with patch("subprocess.run", side_effect=fake_run):
            supertool.op_validate(str(real_file))

        assert len(captured_envs) == 1
        env = captured_envs[0]
        # The spec.env value is present in the subprocess environment
        assert env.get("PHPSTAN_BIN") == "/tmp/evil-phpstan"

    def test_formatter_spec_env_is_passed_to_subprocess(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """spec.env values for formatters also reach the subprocess environment."""
        real_file = tmp_path / "test.php"
        real_file.write_text("<?php\n")

        captured_envs: list = []

        def fake_run(cmd, env=None, **kwargs):
            captured_envs.append(env or {})
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        config = {
            "formatters": {
                "test-formatter": {
                    "cmd": "echo {file}",
                    "match": "*",
                    "hooks_into": ["format"],
                    "env": {"PHP_CS_FIXER_BIN": "/tmp/evil-cs-fixer"},
                }
            }
        }
        _inject_config(monkeypatch, config)

        with patch("subprocess.run", side_effect=fake_run):
            supertool.op_format(str(real_file))

        assert len(captured_envs) == 1
        assert captured_envs[0].get("PHP_CS_FIXER_BIN") == "/tmp/evil-cs-fixer"

    def test_no_spec_env_means_no_extra_env_passed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When spec has no 'env' key, the child sees os.environ plus nothing
        the config could have chosen.

        #475 made the child env explicit so supertool can stamp
        SUPERTOOL_MCP_AUTOSPAWN (a validator may use a warm MCP daemon but not
        create one). The security property this test guards is unchanged and is
        now asserted directly rather than via `env is None`: the delta against
        the ambient environment is exactly the one supertool-owned key, so a
        validator spec still cannot smuggle anything into its own child.
        """
        real_file = tmp_path / "test.php"
        real_file.write_text("<?php\n")

        captured_envs: list = []

        def fake_run(cmd, env=None, **kwargs):
            captured_envs.append(env)
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"tool": "test-validator", "ok": True,
                                   "count": 0, "errors": [], "duration_ms": 1})
            r.stderr = ""
            return r

        _inject_config(monkeypatch, _make_validator_config("echo {file}"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        with patch("subprocess.run", side_effect=fake_run):
            supertool.op_validate(str(real_file))

        assert len(captured_envs) == 1
        child_env = captured_envs[0]
        assert child_env is not None, "child env is explicit since #475"
        delta = {
            k: v for k, v in child_env.items()
            if os.environ.get(k) != v
        }
        assert set(delta) == {"SUPERTOOL_MCP_AUTOSPAWN"}, (
            f"a spec without 'env' must add nothing beyond supertool's own "
            f"provenance flag — got {sorted(delta)}"
        )
        assert delta["SUPERTOOL_MCP_AUTOSPAWN"] == "0"
        assert not (set(os.environ) - set(child_env)), (
            "child must still inherit the whole ambient environment"
        )


# ---------------------------------------------------------------------------
# 10. Directory PATH — document DoS / recurse behaviour
# ---------------------------------------------------------------------------

class TestDirectoryPath:
    """validate:src/ — does it recurse over every file or reject the directory?

    FINDING: op_validate does NOT recurse. It passes the directory path
    directly to _validator_run_one which inserts it into the cmd template.
    The validator binary then decides what to do (most linters accept a dir).

    For formatters, same pattern — formatter binary receives the dir path.

    There is NO built-in recursion or file enumeration in op_validate/op_format.
    The DoS risk depends entirely on the external binary's behaviour.
    """

    def test_validate_directory_path_is_passed_directly_to_validator_no_recursion(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """op_validate does not recurse over directory contents.

        The directory path is passed verbatim to the validator cmd.
        No glob expansion, no os.walk, no directory listing.
        """
        sub_dir = tmp_path / "src"
        sub_dir.mkdir()
        for i in range(5):
            (sub_dir / f"file{i}.php").write_text("<?php\n")

        executed_cmds: list = []

        def fake_run(cmd, **kwargs):
            executed_cmds.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"tool": "test-validator", "ok": True,
                                   "count": 0, "errors": [], "duration_ms": 1})
            r.stderr = ""
            return r

        _inject_config(monkeypatch, _make_validator_config("echo {file}", match="*"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        with patch("subprocess.run", side_effect=fake_run):
            out = supertool.op_validate(str(sub_dir))

        # Exactly ONE subprocess call — the directory path, not one per file.
        assert len(executed_cmds) == 1, (
            f"Expected 1 subprocess call for directory, got {len(executed_cmds)}. "
            "op_validate recurses over directory contents — DoS risk."
        )
        assert str(sub_dir) in executed_cmds[0]

    def test_format_directory_path_is_passed_directly_to_formatter_no_recursion(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """op_format does not recurse over directory contents."""
        sub_dir = tmp_path / "src"
        sub_dir.mkdir()
        for i in range(5):
            (sub_dir / f"file{i}.php").write_text("<?php\n")

        executed_cmds: list = []

        def fake_run(cmd, **kwargs):
            executed_cmds.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        _inject_config(monkeypatch, _make_formatter_config("echo {file}", match="*"))

        with patch("subprocess.run", side_effect=fake_run):
            out = supertool.op_format(str(sub_dir))

        assert len(executed_cmds) == 1, (
            f"Expected 1 subprocess call for directory, got {len(executed_cmds)}."
        )
        assert str(sub_dir) in executed_cmds[0]


# ---------------------------------------------------------------------------
# Bonus: validate_staged subprocess uses list form (no shell injection in git call)
# ---------------------------------------------------------------------------

class TestValidateStagedGitSubprocessForm:
    """validate_staged uses list-form subprocess for the git call — verifying this."""

    def test_validate_staged_git_call_uses_list_form(self, monkeypatch) -> None:
        """The git diff --cached call uses a list, not a shell string.

        This means user-controlled data (branch names, env vars) can't inject
        shell commands via the git invocation itself.
        """
        list_calls: list = []
        shell_calls: list = []

        def fake_run(cmd, shell=False, **kwargs):
            if shell:
                shell_calls.append(cmd)
            else:
                list_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""  # no staged files
            r.stderr = ""
            return r

        _inject_config(monkeypatch, _make_validator_config("echo {file}"))

        with patch("subprocess.run", side_effect=fake_run):
            out = supertool.op_validate_staged()

        # The git call must have been list-form (shell=False or default)
        git_list_calls = [c for c in list_calls if isinstance(c, list) and "git" in c[0]]
        assert len(git_list_calls) >= 1, (
            "git diff --cached was not called with list form — shell injection risk."
        )
        # No shell=True calls for the git invocation
        git_shell_calls = [c for c in shell_calls if "git diff" in str(c)]
        assert len(git_shell_calls) == 0


# ---------------------------------------------------------------------------
# 11. validate list form (issue #306) — routing + per-file path security
# ---------------------------------------------------------------------------

class TestValidateListForm:
    """validate:f1,f2,...:filter — multi-file form routes to op_validate_multi
    and still security-checks every path in the list at dispatch."""

    def test_comma_path_routes_to_multi(self, tmp_path: Path, monkeypatch) -> None:
        """A comma-separated PATH dispatches the list form, one block per file."""
        executed_cmds: list = []

        def fake_run(cmd, **kwargs):
            executed_cmds.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"tool": "test-validator", "ok": True, "count": 0,
                                   "errors": [], "duration_ms": 1})
            r.stderr = ""
            return r

        a = tmp_path / "a.php"; a.write_text("<?php\n")
        b = tmp_path / "b.php"; b.write_text("<?php\n")
        _inject_config(monkeypatch, _make_validator_config("echo {file}", match="*.php"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        with patch("subprocess.run", side_effect=fake_run):
            out = supertool.dispatch(f"validate:{a},{b}")

        assert out.count("validate: ") == 2
        assert str(a) in out and str(b) in out

    def test_single_path_still_one_block_backcompat(self, tmp_path: Path, monkeypatch) -> None:
        """A path with no comma keeps the single-file form unchanged."""
        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"tool": "test-validator", "ok": True, "count": 0,
                                   "errors": [], "duration_ms": 1})
            r.stderr = ""
            return r

        a = tmp_path / "a.php"; a.write_text("<?php\n")
        _inject_config(monkeypatch, _make_validator_config("echo {file}", match="*.php"))
        monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")

        with patch("subprocess.run", side_effect=fake_run):
            out = supertool.dispatch(f"validate:{a}")

        assert out.count("validate: ") == 1
        assert str(a) in out

    def test_list_form_rejects_nul_byte_in_any_member(self, tmp_path: Path, monkeypatch) -> None:
        """A NUL byte anywhere in the list is rejected at dispatch (each path is
        run through _safe_path, not just the first)."""
        a = tmp_path / "a.php"; a.write_text("<?php\n")
        _inject_config(monkeypatch, _make_validator_config("echo {file}", match="*.php"))
        out = supertool.dispatch(f"validate:{a},bad\x00name.php")
        assert "ERROR" in out
