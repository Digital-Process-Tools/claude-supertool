"""Smoke tests for formatters/ruff-format/ruff-format.py (#2085)."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import shlex
import sys
from pathlib import Path

import pytest
from _adapter_verdict import assert_declined, assert_ok

ADAPTER = Path(__file__).parent.parent / "formatters" / "ruff-format" / "ruff-format.py"


def _load_adapter_module():
    """Import ruff-format.py in-process (its filename has hyphens, so it
    cannot be a normal `import` target) so a test can patch `open` at the
    module level and observe how the adapter uses the handle it gets back.
    """
    spec = importlib.util.spec_from_file_location("ruff_format_adapter_2160", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _python_stub(tmp_path: Path, name: str, body: str) -> str:
    """Create a Python stub file and return a `python <path>` command line
    suitable for RUFF_BIN (the adapter shlex-splits the env var).
    """
    stub = tmp_path / f"{name}.py"
    stub.write_text(body)
    return f"{shlex.quote(sys.executable)} {shlex.quote(stub.as_posix())}"


def test_no_arg_returns_schema_error() -> None:
    r = subprocess.run([sys.executable, str(ADAPTER)], capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


def test_missing_binary_returns_schema_error(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x=1\n")
    env = {**os.environ, "RUFF_BIN": "ruff-that-does-not-exist-xyz"}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_declined(data)
    assert "not found" in data["errors"][0]["msg"]
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_clean_file_ok_noop_via_stub(tmp_path: Path) -> None:
    """Stub ruff that exits 0 without touching the file -> ok=True, metrics 0/0."""
    f = tmp_path / "x.py"
    content = "x = 1\n"
    f.write_text(content)

    bin_cmd = _python_stub(tmp_path, "stub_exit0", "import sys; sys.exit(0)\n")
    env = {**os.environ, "RUFF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_ok(data)
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_live_clean_file_ok(tmp_path: Path) -> None:
    """Live ruff on an already-formatted file -> ok=True."""
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_ok(data)


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_live_file_needing_format_gets_reformatted(tmp_path: Path) -> None:
    """A badly-spaced file is actually rewritten, and metrics say so."""
    f = tmp_path / "x.py"
    f.write_text("x=1\ny  =   2\n")
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_ok(data)
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0
    assert f.read_text(encoding="utf-8") != "x=1\ny  =   2\n"


def test_file_needing_format_via_stub(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x=1\n")

    body = (
        "import sys, pathlib\n"
        f"pathlib.Path(r'{f.as_posix()}').write_text('x = 1\\ny = 2\\n')\n"
        "sys.exit(0)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_add_line", body)
    env = {**os.environ, "RUFF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_ok(data)
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0


def test_syntax_error_file_reports_failure_not_ok(tmp_path: Path) -> None:
    """A file ruff cannot parse must fail loudly, never a silent ok=True no-op --
    the bar every 'would this test still pass if the code did nothing' check
    needs: ok=True with 0/0 metrics is EXACTLY what a no-op stub also returns,
    so this is the one case that tells a real failure from an adapter that
    never actually asked the tool anything.
    """
    f = tmp_path / "x.py"
    f.write_text("def f(:\n    pass\n")

    body = (
        "import sys\n"
        "sys.stderr.write('error: failed to parse\\n')\n"
        "sys.exit(2)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_parse_error", body)
    env = {**os.environ, "RUFF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_declined(data)
    assert data["count"] == 1
    assert "failed to parse" in data["errors"][0]["msg"]


def test_before_after_file_handles_are_explicitly_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test for #2160: both reads (`before` at line 85, `after` at
    line 138) used to be a bare `open(...).read()` chain with no context
    manager, so nothing in the adapter ever called `.close()` on the handle
    -- it relied entirely on CPython dropping the last reference and the
    object's own `__del__` doing the closing, timing this codebase does not
    control (not guaranteed at all on e.g. PyPy, and not immediate through
    an exception's traceback holding a frame alive).

    A black-box before/after-content assertion cannot tell the buggy version
    from the fixed one -- the observable diff output is identical either
    way. So this patches `open` at the module level with a fake handle that
    only ever gets `.close()` invoked through `__exit__`, never implicitly,
    and asserts that call happened. That is red on a bare
    `open(...).read()` chain (close_called stays False -- nothing in the
    adapter ever calls it) and green once both sites use `with open(...) as
    f: ... = f.read()`.
    """
    f = tmp_path / "x.py"
    original_text = "x = 1" + chr(10)
    f.write_text(original_text)

    mod = _load_adapter_module()

    bin_cmd = _python_stub(tmp_path, "stub_exit0", "import sys" + chr(10) + "sys.exit(0)" + chr(10))
    monkeypatch.setenv("RUFF_BIN", bin_cmd)
    monkeypatch.setattr(sys, "argv", ["ruff-format.py", str(f)])

    handles = []

    class FakeHandle:
        def __init__(self) -> None:
            self.close_called = False

        def read(self) -> str:
            return original_text

        def close(self) -> None:
            self.close_called = True

        def __enter__(self) -> "FakeHandle":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            self.close()
            return False

    def fake_open(path, *args, **kwargs):
        handle = FakeHandle()
        handles.append(handle)
        return handle

    monkeypatch.setattr(mod, "open", fake_open, raising=False)

    mod.main()

    assert len(handles) == 2, "expected exactly two open() calls (before + after reads)"
    assert all(h.close_called for h in handles), (
        "a file handle from open(...).read() was never explicitly closed -- "
        "the adapter is relying on implicit GC/refcounting timing again"
    )


def test_reread_oserror_reports_verify_failed_not_a_silent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2162: an `OSError` on the post-format re-read used to fall back to
    `after = before`, publishing `ok: True` with `metrics: {0, 0}` --
    identical to the receipt for "ruff genuinely made no changes". A caller
    reading this payload cannot tell the two apart, and the file may well
    have changed: only the re-read failed, not the format.
    """
    f = tmp_path / "x.py"
    original_text = "x = 1" + chr(10)
    f.write_text(original_text)

    mod = _load_adapter_module()

    bin_cmd = _python_stub(tmp_path, "stub_exit0",
                            "import sys" + chr(10) + "sys.exit(0)" + chr(10))
    monkeypatch.setenv("RUFF_BIN", bin_cmd)
    monkeypatch.setattr(sys, "argv", ["ruff-format.py", str(f)])

    real_open = open
    calls = []

    def flaky_open(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 2:
            raise OSError(2, "No such file or directory", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(mod, "open", flaky_open, raising=False)

    captured = []
    monkeypatch.setattr(mod, "emit", lambda obj: captured.append(obj))

    mod.main()

    assert len(captured) == 1, captured
    data = captured[0]
    assert_ok(data)
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0
    assert "verify_failed" in data, (
        "an ok=True payload with 0/0 metrics after a re-read failure is "
        "indistinguishable from a genuine no-op without this field: "
        + repr(data))
    assert "No such file" in data["verify_failed"]


def test_ruff_bin_program_files_style_path_is_not_split_at_the_space(
    tmp_path: Path, monkeypatch,
) -> None:
    """#2176 -- a Windows-Program-Files-shaped RUFF_BIN, actually installed
    there (a real file with the execute bit set), must be used AS ONE PATH.
    Before the fix, POSIX-mode `shlex.split` split the unquoted path at the
    space in "Program Files", and the adapter ran `["<tmp>/Program",
    "format", ...]` -- a binary that does not exist, reported as RUFF_BIN
    not found, even though the real one was sitting right there.
    """
    bin_dir = tmp_path / "Program Files" / "ruff"
    bin_dir.mkdir(parents=True)
    real_ruff = bin_dir / "ruff.exe"
    real_ruff.write_text("")
    real_ruff.chmod(0o755)

    f = tmp_path / "x.py"
    f.write_text("x = 1\n")

    monkeypatch.setenv("RUFF_BIN", real_ruff.as_posix())
    monkeypatch.setattr(sys, "argv", ["ruff-format.py", str(f)])

    mod = _load_adapter_module()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        import subprocess as _sp
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    captured_emit = []
    monkeypatch.setattr(mod, "emit", lambda obj: captured_emit.append(obj))

    mod.main()

    assert captured["cmd"][0] == real_ruff.as_posix(), captured["cmd"]
    assert_ok(captured_emit[0])
