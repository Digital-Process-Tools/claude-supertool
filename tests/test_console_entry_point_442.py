"""Console-script entry point (#442) — pip-installed `supertool` must actually run.

`[project.scripts]` points at `supertool:_cli`. Before this fix `_cli` did not
exist, so `pip install .` succeeded but every invocation of the installed
binary died with ImportError — the module-path route (`supertool.py ...`),
the maintainer's local symlink, and the old CI copy all bypassed the console
script, so nothing caught it.

This test builds a throwaway venv, installs the package non-editable (the
real end-user path), and runs the actual installed `supertool` binary as a
subprocess — no PYTHONPATH, no module-path shortcut. It asserts both halves
of the contract `_cli() -> int` must satisfy: a cheap successful op exits 0,
and a deliberately bad op exits non-zero (pins the exit-code plumbing, not
just import-ability — a `_cli` that always returns 0 would pass every other
assertion here and still be broken, cf. #445/#454).
"""
from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


@pytest.mark.slow
def test_installed_console_script_runs_and_propagates_exit_code(tmp_path: Path) -> None:
    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)

    bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    venv_python = bin_dir / ("python.exe" if sys.platform == "win32" else "python")
    installed_supertool = bin_dir / ("supertool.exe" if sys.platform == "win32" else "supertool")

    subprocess.run(
        [str(venv_python), "-m", "pip", "install", str(REPO_ROOT)],
        check=True,
        capture_output=True,
        encoding="utf-8", errors="replace",
    )

    assert installed_supertool.exists(), (
        f"pip install did not produce a console script at {installed_supertool}"
    )

    ok = subprocess.run(
        [str(installed_supertool), "version"],
        capture_output=True,
        encoding="utf-8", errors="replace",
    )
    assert ok.returncode == 0, (
        f"installed `supertool version` should exit 0\n"
        f"stdout: {ok.stdout}\nstderr: {ok.stderr}"
    )
    assert "supertool" in ok.stdout.lower()

    bad = subprocess.run(
        [str(installed_supertool), "not-a-real-op:whatever"],
        capture_output=True,
        encoding="utf-8", errors="replace",
    )
    assert bad.returncode != 0, (
        f"installed supertool should propagate a non-zero exit code for a "
        f"failing op instead of always exiting 0\n"
        f"stdout: {bad.stdout}\nstderr: {bad.stderr}"
    )
