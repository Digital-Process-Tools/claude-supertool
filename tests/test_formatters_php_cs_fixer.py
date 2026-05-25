"""Smoke tests for formatters/php-cs-fixer/php-cs-fixer.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import sys

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Adapter tests use bash shebang stubs (#!/usr/bin/env bash) — not available on Windows runners",
)

ADAPTER = Path(__file__).parent.parent / "formatters" / "php-cs-fixer" / "php-cs-fixer.py"


def test_no_arg_returns_schema_error() -> None:
    r = subprocess.run(["python3", str(ADAPTER)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "php-cs-fixer"
    assert data["ok"] is False
    assert "no file arg" in data["errors"][0]["msg"]


def test_missing_binary_returns_schema_error(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x=1;\n")
    env = {**os.environ, "PHPCSFIXER_BIN": "php-cs-fixer-that-does-not-exist-xyz"}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "php-cs-fixer"
    assert data["ok"] is False
    assert "not found" in data["errors"][0]["msg"]
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_exit0_noop_via_stub(tmp_path: Path) -> None:
    """php-cs-fixer exit 0 = no changes → ok=True, metrics 0/0."""
    f = tmp_path / "clean.php"
    f.write_text("<?php\n$x = 1;\n")
    stub = tmp_path / "php-cs-fixer"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(0o755)
    env = {**os.environ, "PHPCSFIXER_BIN": str(stub)}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["ok"] is True
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_exit1_fixes_applied_via_stub(tmp_path: Path) -> None:
    """php-cs-fixer exit 1 = fixes applied → ok=True, metrics > 0."""
    f = tmp_path / "dirty.php"
    f.write_text("<?php\n$x=1;\n")

    stub = tmp_path / "php-cs-fixer"
    stub.write_text(
        f"#!/usr/bin/env bash\nprintf '<?php\\n$x = 1;\\n$y = 2;\\n' > {f}\nexit 1\n"
    )
    stub.chmod(0o755)
    env = {**os.environ, "PHPCSFIXER_BIN": str(stub)}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "php-cs-fixer"
    assert data["ok"] is True
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0


def test_exit16_error_via_stub(tmp_path: Path) -> None:
    """php-cs-fixer exit >=16 = error → ok=False."""
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x = 1;\n")
    stub = tmp_path / "php-cs-fixer"
    stub.write_text("#!/usr/bin/env bash\necho 'fatal: config error' >&2\nexit 16\n")
    stub.chmod(0o755)
    env = {**os.environ, "PHPCSFIXER_BIN": str(stub)}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["ok"] is False


@pytest.mark.skipif(not shutil.which("php-cs-fixer"), reason="php-cs-fixer not installed")
def test_live_clean_php(tmp_path: Path) -> None:
    f = tmp_path / "ok.php"
    f.write_text("<?php\nfunction add(int $a, int $b): int\n{\n    return $a + $b;\n}\n")
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["ok"] is True
