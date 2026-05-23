"""Smoke tests for formatters/phpcbf/phpcbf.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "formatters" / "phpcbf" / "phpcbf.py"


def test_no_arg_returns_schema_error() -> None:
    r = subprocess.run(["python3", str(ADAPTER)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpcbf"
    assert data["ok"] is False
    assert "no file arg" in data["errors"][0]["msg"]


def test_missing_binary_returns_schema_error(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x=1;\n")
    env = {**os.environ, "PHPCBF_BIN": "phpcbf-that-does-not-exist-xyz"}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpcbf"
    assert data["ok"] is False
    assert "not found" in data["errors"][0]["msg"]
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_exit0_noop_via_stub(tmp_path: Path) -> None:
    """phpcbf exit 0 = nothing to fix → ok=True, metrics 0/0."""
    f = tmp_path / "clean.php"
    f.write_text("<?php\n$x = 1;\n")
    stub = tmp_path / "phpcbf"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(0o755)
    env = {**os.environ, "PHPCBF_BIN": str(stub)}
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
    """phpcbf exit 1 = fixes applied → ok=True, metrics > 0."""
    f = tmp_path / "dirty.php"
    f.write_text("<?php\n$x=1;\n")

    stub = tmp_path / "phpcbf"
    stub.write_text(
        f"#!/usr/bin/env bash\nprintf '<?php\\n$x = 1;\\n$y = 2;\\n' > {f}\nexit 1\n"
    )
    stub.chmod(0o755)
    env = {**os.environ, "PHPCBF_BIN": str(stub)}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpcbf"
    assert data["ok"] is True
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0


def test_exit2_unfixable_remaining_is_not_formatter_failure(tmp_path: Path) -> None:
    """phpcbf exit 2 = errors phpcbf cannot fix (phpcs concern). Formatter
    treats this as ok=True — it did its job; remaining errors should surface
    via the phpcs validator, not as a formatter failure."""
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x = 1;\n")
    stub = tmp_path / "phpcbf"
    stub.write_text(
        "#!/usr/bin/env bash\necho 'No fixable errors were found'\nexit 2\n"
    )
    stub.chmod(0o755)
    env = {**os.environ, "PHPCBF_BIN": str(stub)}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["ok"] is True, "exit 2 is not a formatter failure"


def test_exit3_internal_error_is_failure(tmp_path: Path) -> None:
    """phpcbf exit 3 = real internal failure → ok=False."""
    f = tmp_path / "x.php"
    f.write_text("<?php\n$x = 1;\n")
    stub = tmp_path / "phpcbf"
    stub.write_text("#!/usr/bin/env bash\necho 'fatal error' >&2\nexit 3\n")
    stub.chmod(0o755)
    env = {**os.environ, "PHPCBF_BIN": str(stub)}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["ok"] is False
    assert "fatal error" in data["errors"][0]["msg"]


@pytest.mark.skipif(not shutil.which("phpcbf"), reason="phpcbf not installed")
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
