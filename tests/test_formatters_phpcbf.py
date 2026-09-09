"""Smoke tests for formatters/phpcbf/phpcbf.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import shlex
import sys
from pathlib import Path

import pytest
from _adapter_verdict import assert_declined, assert_ok

ADAPTER = Path(__file__).parent.parent / "formatters" / "phpcbf" / "phpcbf.py"


def _python_stub(tmp_path: Path, name: str, body: str) -> str:
    """Create a Python stub file and return a `python <path>` command line
    suitable for PHPCBF_BIN (the adapter shlex-splits the env var).

    POSIX-only bash stubs (#!/usr/bin/env bash) don't run on Windows runners;
    Python is universally available and the adapter contract now allows the
    BIN env var to be a multi-token command.
    """
    stub = tmp_path / f"{name}.py"
    stub.write_text(body)
    return f"{shlex.quote(sys.executable)} {shlex.quote(stub.as_posix())}"


def test_no_arg_returns_schema_error() -> None:
    r = subprocess.run([sys.executable, str(ADAPTER)], capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpcbf"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


def test_missing_binary_returns_schema_error(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x=1;\n")
    env = {**os.environ, "PHPCBF_BIN": "phpcbf-that-does-not-exist-xyz"}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpcbf"
    assert_declined(data)
    assert "not found" in data["errors"][0]["msg"]
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_exit0_noop_via_stub(tmp_path: Path) -> None:
    """phpcbf exit 0 = nothing to fix → ok=True, metrics 0/0."""
    f = tmp_path / "clean.php"
    f.write_text("<?php\n$x = 1;\n")
    bin_cmd = _python_stub(tmp_path, "stub_exit0", "import sys; sys.exit(0)\n")
    env = {**os.environ, "PHPCBF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_ok(data)
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_exit1_fixes_applied_via_stub(tmp_path: Path) -> None:
    """phpcbf exit 1 = fixes applied → ok=True, metrics > 0."""
    f = tmp_path / "dirty.php"
    f.write_text("<?php\n$x=1;\n")
    body = (
        "import sys, pathlib\n"
        f"pathlib.Path(r'{f.as_posix()}').write_text('<?php\\n$x = 1;\\n$y = 2;\\n')\n"
        "sys.exit(1)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_exit1", body)
    env = {**os.environ, "PHPCBF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpcbf"
    assert_ok(data)
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0


def test_exit2_unfixable_remaining_is_not_formatter_failure(tmp_path: Path) -> None:
    """phpcbf exit 2 = errors phpcbf cannot fix (phpcs concern). Formatter
    treats this as ok=True — it did its job; remaining errors should surface
    via the phpcs validator, not as a formatter failure."""
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x = 1;\n")
    body = (
        "import sys\n"
        "sys.stdout.write('No fixable errors were found\\n')\n"
        "sys.exit(2)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_exit2", body)
    env = {**os.environ, "PHPCBF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["ok"] is True, "exit 2 is not a formatter failure"


def test_exit3_internal_error_is_failure(tmp_path: Path) -> None:
    """phpcbf exit 3 = real internal failure → ok=False."""
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x = 1;\n")
    body = (
        "import sys\n"
        "sys.stderr.write('fatal error\\n')\n"
        "sys.exit(3)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_exit3", body)
    env = {**os.environ, "PHPCBF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_declined(data)
    assert "fatal error" in data["errors"][0]["msg"]


@pytest.mark.skipif(not shutil.which("phpcbf"), reason="phpcbf not installed")
def test_live_clean_php(tmp_path: Path) -> None:
    f = tmp_path / "ok.php"
    f.write_text("<?php\nfunction add(int $a, int $b): int\n{\n    return $a + $b;\n}\n")
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_ok(data)


def _load_adapter_module():
    """Import phpcbf.py in-process (its filename cannot be a normal
    `import` target) so a test can monkeypatch `subprocess.run` and inspect
    the argv it was actually called with.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("phpcbf_adapter_2191", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_phpcbf_bin_program_files_style_path_is_not_split_at_the_space(
    tmp_path: Path, monkeypatch,
) -> None:
    """#2191 -- a Windows-Program-Files-shaped PHPCBF_BIN, actually
    installed there (a real file with the execute bit set), must be used AS
    ONE PATH. Before the fix, POSIX-mode `shlex.split` split the unquoted
    path at the space in "Program Files", and the adapter ran
    `["<tmp>/Program", ...]` -- a binary that does not exist, reported as
    PHPCBF_BIN not found, even though the real one was sitting right there.
    """
    bin_dir = tmp_path / "Program Files" / "phpcbf"
    bin_dir.mkdir(parents=True)
    real_bin = bin_dir / "phpcbf.exe"
    real_bin.write_text("")
    real_bin.chmod(0o755)

    f = tmp_path / "x.php"
    f.write_text("<?php\n$x=1;\n")

    monkeypatch.setenv("PHPCBF_BIN", real_bin.as_posix())
    monkeypatch.setattr(sys, "argv", ["phpcbf.py", str(f)])

    mod = _load_adapter_module()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    captured_emit = []
    monkeypatch.setattr(mod, "emit", lambda obj: captured_emit.append(obj))

    mod.main()

    assert captured["cmd"][0] == real_bin.as_posix(), captured["cmd"]
    assert_ok(captured_emit[0])


def test_phpcbf_bin_shlex_quoted_stub_still_works(tmp_path: Path) -> None:
    """The pre-existing multi-token test-stub convention (PHPCBF_BIN set to
    a shlex-quoted `python /path/stub.py` command line) must still resolve
    after the migration to resolve_bin_cmd() -- that is the fallback path
    resolve_bin_cmd() takes when the raw value does not itself resolve to a
    single executable file.
    """
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x=1;\n")
    stub = _python_stub(tmp_path, "phpcbf_stub", (
        "import sys\n"
        "sys.exit(0)\n"
    ))
    env = {**os.environ, "PHPCBF_BIN": stub}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_ok(data)


def test_reformat_touches_unrelated_lines_reports_range_via_stub(tmp_path: Path) -> None:
    """#2458: phpcbf shares ruff-format's pre-#2405 gap -- a stub that
    rewrites the whole file (re-indenting a block) must report the
    before-file span it actually touched, the same as ruff-format does.
    """
    f = tmp_path / "x.php"
    before_text = (
        "<?php\n"
        "function f($x) {\n"
        "  if ($x > 0) {\n"
        "      return $x;\n"
        "  }\n"
        "  return null;\n"
        "}\n"
    )
    after_text = (
        "<?php\n"
        "function f($x)\n"
        "{\n"
        "    if ($x > 0) {\n"
        "        return $x;\n"
        "    }\n"
        "    return null;\n"
        "}\n"
    )
    f.write_text(before_text)

    body = (
        "import sys, pathlib\n"
        f"pathlib.Path(r'{f.as_posix()}').write_text({after_text!r})\n"
        "sys.exit(1)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_reindent", body)
    env = {**os.environ, "PHPCBF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_ok(data)
    metrics = data["metrics"]
    assert metrics["lines_added"] > 0 and metrics["lines_removed"] > 0
    # Before-file lines 2-6 are all re-indented/rewritten.
    assert metrics["first_changed_line"] == 2, metrics
    assert metrics["last_changed_line"] == 6, metrics


def test_noop_reports_no_touched_line_range(tmp_path: Path) -> None:
    """MUST FIRE control for the test above: when nothing changed, the new
    fields must be absent/None, never a stale or fabricated range.
    """
    f = tmp_path / "clean.php"
    f.write_text("<?php\n$x = 1;\n")
    bin_cmd = _python_stub(tmp_path, "stub_exit0", "import sys; sys.exit(0)\n")
    env = {**os.environ, "PHPCBF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_ok(data)
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0
    # Bracket access, not .get(): a .get() default of None cannot tell
    # "computed None" from "the key was never added" -- the exact gap that
    # would make this control pass unchanged against the pre-#2458 code,
    # which never populated these keys at all.
    assert data["metrics"]["first_changed_line"] is None
    assert data["metrics"]["last_changed_line"] is None
