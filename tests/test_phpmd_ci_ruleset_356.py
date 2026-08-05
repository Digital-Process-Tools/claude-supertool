"""Issue #356: phpmd validator must auto-detect the project's CI ruleset.

The DVSI CI `md_all2` job enforces phpmd with the project's own rulesets at
`gitlab-ci/md/*.xml` (cleancode.xml, design.xml, naming.xml) plus the built-in
categories the project does not override. The local validator historically ran
phpmd's full default ruleset, so it flagged rules CI never enforces
(false alarms) — eroding trust in the green/red signal.

Fix (issue request #1, "highest-value"): when `gitlab-ci/md/*.xml` exists in an
ancestor of the file being validated, use it as the ruleset so local output
matches CI, and surface which ruleset source is active. When absent, fall back
to the current default behavior unchanged.

These tests stub the phpmd binary with a bash script that echoes the ruleset
argument it received, so we can assert exactly what the adapter passed without
needing a real phpmd install.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget

PHPMD_PY = Path(__file__).parent.parent / "validators" / "phpmd" / "phpmd.py"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Stub uses bash shebang — not executable on Windows without WSL",
)


def _echo_ruleset_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A phpmd stub that writes the ruleset arg (argv position 3) to a file.

    phpmd invocation is: phpmd <file> <format> <ruleset> --suffixes ...
    so $3 is the ruleset. The adapter captures the stub's stdout/stderr, so we
    record the ruleset to a sentinel file the test can read afterwards. Exit 0
    with no findings so the adapter parses a clean result.
    """
    sentinel = tmp_path / "ruleset.seen"
    stub = tmp_path / "phpmd"
    stub.write_text(f'#!/usr/bin/env bash\nprintf %s "$3" > "{sentinel}"\nexit 0\n')
    stub.chmod(0o755)
    return stub, sentinel


def _run(php_file: Path, stub: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PHPMD_BIN": str(stub)}
    # Ensure no inherited PHPMD_RULESETS override from the environment.
    env.pop("PHPMD_RULESETS", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(PHPMD_PY), str(php_file)],
        capture_output=True, text=True, timeout=adapter_budget(PHPMD_PY), env=env, encoding="utf-8", errors="replace",
    )


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    """Create a DVSI-like project: gitlab-ci/md/*.xml sibling to src2/.

    Returns (project_root, php_file_deep_inside_src2).
    """
    md = tmp_path / "Dvsi" / "dvsi-private" / "gitlab-ci" / "md"
    md.mkdir(parents=True)
    for name in ("cleancode.xml", "design.xml", "naming.xml"):
        (md / name).write_text(
            '<?xml version="1.0"?>\n<ruleset name="x"></ruleset>\n'
        )
    src = tmp_path / "Dvsi" / "dvsi-private" / "src2" / "SiFoo" / "BusinessEntities"
    src.mkdir(parents=True)
    php = src / "Foo.php"
    php.write_text("<?php\n$x = 1;\n")
    return tmp_path, php


def test_autodetect_uses_project_rulesets(tmp_path: Path) -> None:
    """When gitlab-ci/md/*.xml exists in an ancestor, the adapter passes the
    project XML paths as (part of) the ruleset instead of the plain defaults."""
    _root, php = _make_project(tmp_path)
    stub, sentinel = _echo_ruleset_stub(tmp_path)
    r = _run(php, stub)
    assert r.returncode == 0, r.stderr
    ruleset = sentinel.read_text(encoding="utf-8")
    # Project XML files must be referenced by absolute path.
    for name in ("cleancode.xml", "design.xml", "naming.xml"):
        assert name in ruleset, f"{name} missing from ruleset: {ruleset}"
    # And the built-in categories the project does NOT override must remain,
    # matching the CI mix (codesize, controversial, unusedcode).
    for builtin in ("codesize", "controversial", "unusedcode"):
        assert builtin in ruleset, f"builtin {builtin} missing: {ruleset}"


def test_autodetect_surfaces_ruleset_source_in_output(tmp_path: Path) -> None:
    """The emitted JSON must make the active ruleset source visible so the
    user can tell CI-enforced runs from local-default runs."""
    _root, php = _make_project(tmp_path)
    stub, _sentinel = _echo_ruleset_stub(tmp_path)
    r = _run(php, stub)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpmd"
    assert "ruleset_source" in data, data
    assert data["ruleset_source"] == "project"


def test_no_project_rulesets_falls_back_to_default(tmp_path: Path) -> None:
    """Repos WITHOUT gitlab-ci/md/ must be unaffected: default ruleset used,
    ruleset_source == 'default'."""
    php = tmp_path / "lonely.php"
    php.write_text("<?php\n$x = 1;\n")
    stub, sentinel = _echo_ruleset_stub(tmp_path)
    r = _run(php, stub)
    assert r.returncode == 0, r.stderr
    ruleset = sentinel.read_text(encoding="utf-8")
    # Default behavior: plain built-in category names, no .xml paths.
    assert ".xml" not in ruleset
    assert ruleset == "cleancode,codesize,controversial,design,naming,unusedcode"
    data = json.loads(r.stdout.strip())
    assert data["ruleset_source"] == "default"


def test_explicit_env_ruleset_disables_autodetect(tmp_path: Path) -> None:
    """An explicit PHPMD_RULESETS must win over auto-detection (escape hatch)."""
    _root, php = _make_project(tmp_path)
    stub, sentinel = _echo_ruleset_stub(tmp_path)
    r = _run(php, stub, extra_env={"PHPMD_RULESETS": "codesize"})
    assert r.returncode == 0, r.stderr
    ruleset = sentinel.read_text(encoding="utf-8")
    assert ruleset == "codesize"
    data = json.loads(r.stdout.strip())
    assert data["ruleset_source"] == "env"
