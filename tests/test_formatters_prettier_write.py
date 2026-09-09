"""Smoke tests for formatters/prettier-write/prettier-write.py."""
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

ADAPTER = Path(__file__).parent.parent / "formatters" / "prettier-write" / "prettier-write.py"


def _python_stub(tmp_path: Path, name: str, body: str) -> str:
    """Create a Python stub file and return a `python <path>` command line
    suitable for PRETTIER_BIN (the adapter shlex-splits the env var).
    """
    stub = tmp_path / f"{name}.py"
    stub.write_text(body)
    return f"{shlex.quote(sys.executable)} {shlex.quote(stub.as_posix())}"


def test_no_arg_returns_schema_error() -> None:
    r = subprocess.run([sys.executable, str(ADAPTER)], capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


def test_missing_binary_returns_schema_error(tmp_path: Path) -> None:
    f = tmp_path / "x.js"
    f.write_text("const x=1\n")
    env = {**os.environ, "PRETTIER_BIN": "prettier-that-does-not-exist-xyz"}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert_declined(data)
    assert "not found" in data["errors"][0]["msg"]
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_clean_file_ok_noop_via_stub(tmp_path: Path) -> None:
    """Stub prettier that exits 0 without touching the file → ok=True, metrics 0/0."""
    f = tmp_path / "x.json"
    content = '{"a": 1}\n'
    f.write_text(content)

    bin_cmd = _python_stub(tmp_path, "stub_exit0", "import sys; sys.exit(0)\n")
    env = {**os.environ, "PRETTIER_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert_ok(data)
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


@pytest.mark.skipif(not shutil.which("prettier"), reason="prettier not installed")
def test_live_clean_file_ok(tmp_path: Path) -> None:
    """Live prettier on an already-formatted file → ok=True."""
    f = tmp_path / "x.json"
    # Write content that prettier will not change (already formatted)
    f.write_text('{\n  "a": 1\n}\n')
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert_ok(data)


def test_file_needing_format_via_stub(tmp_path: Path) -> None:
    f = tmp_path / "x.js"
    f.write_text("const x=1\n")

    body = (
        "import sys, pathlib\n"
        f"pathlib.Path(r'{f.as_posix()}').write_text('const x = 1\\nconst y = 2\\n')\n"
        "sys.exit(0)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_add_line", body)
    env = {**os.environ, "PRETTIER_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert_ok(data)
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0


def _load_adapter_module():
    """Import prettier-write.py in-process (its filename cannot be a
    normal `import` target) so a test can monkeypatch `subprocess.run` and
    inspect the argv it was actually called with.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("prettier_write_adapter_2191", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_prettier_bin_program_files_style_path_is_not_split_at_the_space(
    tmp_path: Path, monkeypatch,
) -> None:
    """#2191 -- a Windows-Program-Files-shaped PRETTIER_BIN, actually
    installed there (a real file with the execute bit set), must be used AS
    ONE PATH. Before the fix, POSIX-mode `shlex.split` split the unquoted
    path at the space in "Program Files", and the adapter ran
    `["<tmp>/Program", ...]` -- a binary that does not exist, reported as
    PRETTIER_BIN not found, even though the real one was sitting right
    there.
    """
    bin_dir = tmp_path / "Program Files" / "prettier"
    bin_dir.mkdir(parents=True)
    real_bin = bin_dir / "prettier.exe"
    real_bin.write_text("")
    real_bin.chmod(0o755)

    f = tmp_path / "x.js"
    f.write_text("const x=1\n")

    monkeypatch.setenv("PRETTIER_BIN", real_bin.as_posix())
    monkeypatch.setattr(sys, "argv", ["prettier-write.py", str(f)])

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


def test_prettier_bin_shlex_quoted_stub_still_works(tmp_path: Path) -> None:
    """The pre-existing multi-token test-stub convention (PRETTIER_BIN set
    to a shlex-quoted `python /path/stub.py` command line) must still
    resolve after the migration to resolve_bin_cmd() -- that is the
    fallback path resolve_bin_cmd() takes when the raw value does not
    itself resolve to a single executable file.
    """
    f = tmp_path / "x.js"
    f.write_text("const x=1\n")
    stub = _python_stub(tmp_path, "prettier_stub", (
        "import sys\n"
        "sys.exit(0)\n"
    ))
    env = {**os.environ, "PRETTIER_BIN": stub}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_ok(data)


def test_reformat_touches_unrelated_lines_reports_range_via_stub(tmp_path: Path) -> None:
    """#2458: prettier-write shares ruff-format's pre-#2405 gap -- a stub that
    rewrites the whole file (collapsing a multi-line condition) must report
    the before-file span it actually touched, the same as ruff-format does.
    """
    f = tmp_path / "x.js"
    before_text = (
        "function f(x) {\n"
        "  if (\n"
        "    typeof x === 'number' &&\n"
        "    x > 0\n"
        "  ) {\n"
        "    return x;\n"
        "  }\n"
        "  return null;\n"
        "}\n"
    )
    after_text = (
        "function f(x) {\n"
        "  if (typeof x === \"number\" && x > 0) {\n"
        "    return x;\n"
        "  }\n"
        "  return null;\n"
        "}\n"
    )
    f.write_text(before_text)

    body = (
        "import sys, pathlib\n"
        f"pathlib.Path(r'{f.as_posix()}').write_text({after_text!r})\n"
        "sys.exit(0)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_collapse_condition", body)
    env = {**os.environ, "PRETTIER_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_ok(data)
    metrics = data["metrics"]
    assert metrics["lines_added"] > 0 and metrics["lines_removed"] > 0
    # Before-file lines 2-5 are the "if (...)" block collapsed to one line.
    assert metrics["first_changed_line"] == 2, metrics
    assert metrics["last_changed_line"] == 5, metrics


def test_noop_reports_no_touched_line_range(tmp_path: Path) -> None:
    """MUST FIRE control for the test above: when nothing changed, the new
    fields must be absent/None, never a stale or fabricated range.
    """
    f = tmp_path / "clean.js"
    f.write_text("const x = 1;\n")
    bin_cmd = _python_stub(tmp_path, "stub_exit0", "import sys; sys.exit(0)\n")
    env = {**os.environ, "PRETTIER_BIN": bin_cmd}
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
