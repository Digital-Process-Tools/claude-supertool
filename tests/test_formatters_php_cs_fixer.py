"""Smoke tests for formatters/php-cs-fixer/php-cs-fixer.py."""
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

ADAPTER = Path(__file__).parent.parent / "formatters" / "php-cs-fixer" / "php-cs-fixer.py"


def _python_stub(tmp_path: Path, name: str, body: str) -> str:
    """Create a Python stub file and return a `python <path>` command line
    suitable for PHPCSFIXER_BIN (the adapter shlex-splits the env var).
    """
    stub = tmp_path / f"{name}.py"
    stub.write_text(body)
    return f"{shlex.quote(sys.executable)} {shlex.quote(stub.as_posix())}"


def test_no_arg_returns_schema_error() -> None:
    r = subprocess.run([sys.executable, str(ADAPTER)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "php-cs-fixer"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


def test_missing_binary_returns_schema_error(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x=1;\n")
    env = {**os.environ, "PHPCSFIXER_BIN": "php-cs-fixer-that-does-not-exist-xyz"}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "php-cs-fixer"
    assert_declined(data)
    assert "not found" in data["errors"][0]["msg"]
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_exit0_noop_via_stub(tmp_path: Path) -> None:
    """php-cs-fixer exit 0 = no changes → ok=True, metrics 0/0."""
    f = tmp_path / "clean.php"
    f.write_text("<?php\n$x = 1;\n")
    bin_cmd = _python_stub(tmp_path, "stub_exit0", "import sys; sys.exit(0)\n")
    env = {**os.environ, "PHPCSFIXER_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_ok(data)
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_exit1_fixes_applied_via_stub(tmp_path: Path) -> None:
    """php-cs-fixer exit 1 = fixes applied → ok=True, metrics > 0."""
    f = tmp_path / "dirty.php"
    f.write_text("<?php\n$x=1;\n")
    body = (
        "import sys, pathlib\n"
        f"pathlib.Path(r'{f.as_posix()}').write_text('<?php\\n$x = 1;\\n$y = 2;\\n')\n"
        "sys.exit(1)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_exit1", body)
    env = {**os.environ, "PHPCSFIXER_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "php-cs-fixer"
    assert_ok(data)
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0


def test_exit16_error_via_stub(tmp_path: Path) -> None:
    """php-cs-fixer exit >=16 = error → ok=False."""
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x = 1;\n")
    body = (
        "import sys\n"
        "sys.stderr.write('fatal: config error\\n')\n"
        "sys.exit(16)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_exit16", body)
    env = {**os.environ, "PHPCSFIXER_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_declined(data)


@pytest.mark.skipif(not shutil.which("php-cs-fixer"), reason="php-cs-fixer not installed")
def test_live_clean_php(tmp_path: Path) -> None:
    f = tmp_path / "ok.php"
    f.write_text("<?php\nfunction add(int $a, int $b): int\n{\n    return $a + $b;\n}\n")
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert_ok(data)
