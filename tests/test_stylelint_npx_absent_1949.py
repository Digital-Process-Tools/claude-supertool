"""stylelint has the same two-route resolution as eslint and no npx-absence
guard at all (#1949): an uninstalled stylelint, reached through `npx
--no-install`, reports `tool_fault` -- "this is a stylelint failure" -- rather
than the third state eslint already gets right.

Fixture shape mirrors tests/test_validators_eslint_667.py's `_fake_npx`: PATH
is exactly one directory holding a fake `npx`, so `shutil.which("stylelint")`
genuinely fails and the adapter takes its documented npx fallback.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget
from _adapter_verdict import assert_declined, describe, verdict
import subprocess

from _winenv import empty_path_env

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "stylelint" / "stylelint.py"

posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason=("a fake binary on PATH cannot intercept an extensionless list "
            "spawn on Windows: CreateProcess appends .exe and ignores PATHEXT"),
)

#: npm 10.9.4, reproduced on this machine (#1949's own repro).
NPX_CANCELED_STDERR = (
    'npm error npx canceled due to missing packages and no YES option: '
    '["stylelint@17.14.1"]\n'
)


def _fake_npx(tmp_path: Path, stderr: str, rc: int = 1) -> dict:
    """`npx` on PATH and no `stylelint` -- the common laptop with node."""
    bindir = tmp_path / "npxbin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "fake_npx.py"
    script.write_text(
        "import sys\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({rc})\n", encoding="utf-8")
    launcher = bindir / "npx"
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    env = empty_path_env()
    env["PATH"] = str(bindir)
    return env


def _css(tmp_path: Path, body: str = "a { color: red; }\n") -> Path:
    p = tmp_path / "x.css"
    p.write_text(body, encoding="utf-8")
    return p


def _run(path: Path, env: dict) -> dict:
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(path)],
        capture_output=True, text=True, env=env,
        timeout=adapter_budget(ADAPTER), encoding="utf-8", errors="replace",
    )
    return verdict(r, adapter=ADAPTER.name)


@posix_only
def test_npx_without_stylelint_is_an_absent_stylelint_not_a_failed_one(
        tmp_path: Path) -> None:
    """The #1949 repro: npx refuses under --no-install and it must read as
    absent, not as `tool_fault` naming "this is a stylelint failure"."""
    out = _run(_css(tmp_path), _fake_npx(tmp_path, NPX_CANCELED_STDERR))
    assert "skipped" in out, describe(out)
    assert "npm install" in out["skipped"], out["skipped"]
    for key in ("ok", "count", "errors"):
        assert key not in out, f"a skip must not carry {key!r}: {out}"


@posix_only
def test_npx_without_stylelint_is_loud_when_required(tmp_path: Path) -> None:
    env = _fake_npx(tmp_path, NPX_CANCELED_STDERR)
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = "stylelint"
    out = _run(_css(tmp_path), env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="a required stylelint that npx cannot resolve")
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in out["errors"][0]["msg"]
    assert "npm install" in out["errors"][0]["msg"], out["errors"][0]["msg"]


@posix_only
def test_a_real_npx_failure_is_still_a_failure(tmp_path: Path) -> None:
    """The narrow half, paired in the same fixture as the must-fire case
    above: an npx failure that is NOT a missing stylelint must stay loud, or
    this guard is silently swallowing failures nobody can now see."""
    env = _fake_npx(tmp_path, "npm error code EACCES\nnpm error syscall open\n")
    out = _run(_css(tmp_path), env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="an npx failure that is not a missing stylelint")
    assert out["errors"][0]["code"] == "adapter", describe(out)
