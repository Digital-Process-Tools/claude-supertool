"""#263 — a phpstan result must never read as a verdict PHPStan never reached.

Two separate claims live here, and they were not equally true when the issue
was filed:

1. **Inheritance IS visible in single-file scope.** The issue's stated cause —
   "the op analyses the single file in isolation, so `property.extraNativeType`
   can't fire" — does not reproduce. PHPStan resolves the parent through the
   project config's `paths`/`scanDirectories` and reports the error on the child
   file alone. That is locked here so it cannot silently regress.

2. **A refusal to analyse DID read as CLEAN.** When PHPStan declines — the file
   is under `excludePaths.analyse`, the path matches nothing, the run dies — it
   writes to stderr, exits non-zero, and puts *nothing* on stdout. The adapter
   read empty stdout as `{"totals": {"file_errors": 0}}` and emitted
   `ok: true, count: 0`. A green that means "I analysed nothing" is
   indistinguishable from one that means "I analysed it and it is fine", which
   is exactly the symptom #263 reported, arrived at by a different road.

The three-state contract (validators/SCHEMA.md, "Skipped: the third state")
already has the vocabulary for (2); this adapter just wasn't using it.
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget

PHPSTAN_PY = Path(__file__).parent.parent / "validators" / "phpstan" / "phpstan.py"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX /bin/sh shim")


def _run_adapter(tmp_path: Path, *, stdout: str = "", stderr: str = "",
                 exit_code: int = 0) -> dict:
    """Drive the adapter against a fake `php` with full control of the exit.

    The real failure mode needs all three knobs at once — empty stdout, a
    message on stderr, a non-zero exit — and none of the pre-existing shims
    could express it.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake_php = bindir / "php"
    script = "#!/bin/sh\n"
    if stdout:
        script += "cat <<'OUT'\n" + stdout + "\nOUT\n"
    if stderr:
        script += "cat >&2 <<'ERR'\n" + stderr + "\nERR\n"
    script += f"exit {exit_code}\n"
    fake_php.write_text(script)
    fake_php.chmod(0o755)
    dummy_bin = bindir / "phpstan"
    dummy_bin.write_text("#!/bin/sh\n:\n")
    dummy_bin.chmod(0o755)
    target = tmp_path / "Child.php"
    target.write_text("<?php\nclass Child extends ParentC { protected string $label = ''; }\n")
    env = {**os.environ,
           "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
           "PHPSTAN_BIN": str(dummy_bin)}
    r = subprocess.run([sys.executable, str(PHPSTAN_PY), str(target)],
                       capture_output=True, text=True, timeout=adapter_budget(PHPSTAN_PY), env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


def _is_bare_clean(data: dict) -> bool:
    """A receipt that any consumer would read as 'analysed, nothing wrong'."""
    return (data.get("ok") is True
            and data.get("count") == 0
            and not data.get("errors")
            and "skipped" not in data)


# ---------------------------------------------------------------------------
# (2) Refusal must not render as a verdict
# ---------------------------------------------------------------------------

def test_excluded_path_refusal_is_skipped_not_clean(tmp_path: Path) -> None:
    """`excludePaths.analyse` → PHPStan analyses nothing. That is not a pass.

    Verbatim shape of the real run (phpstan 2.2.2, project neon excluding the
    target): stdout empty, stderr `[ERROR] No files found to analyse.`, rc=1.
    """
    data = _run_adapter(tmp_path, stderr=" [ERROR] No files found to analyse.", exit_code=1)
    assert not _is_bare_clean(data), (
        "a file PHPStan refused to analyse came back as a clean pass: " + json.dumps(data))
    assert "skipped" in data
    assert "no files found to analyse" in data["skipped"].lower()


def test_refusal_reason_is_carried_not_invented(tmp_path: Path) -> None:
    """The skip says what the tool said, so the reader can fix the config."""
    data = _run_adapter(tmp_path, stderr=" [ERROR] No files found to analyse.", exit_code=1)
    assert data["skipped"].strip().startswith("[ERROR]") or "No files found" in data["skipped"]


def test_unexplained_failure_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """An exit the adapter cannot explain stays an error.

    Swallowing an unknown failure is the same category mistake as reporting a
    refusal — pointing the other way. OOM here: empty stdout, fatal on stderr.
    """
    data = _run_adapter(
        tmp_path,
        stderr="PHP Fatal error:  Allowed memory size of 2147483648 bytes exhausted",
        exit_code=255,
    )
    assert not _is_bare_clean(data), "an OOM'd analysis came back as a clean pass"
    assert data["ok"] is False
    assert data["errors"][0]["code"] == "adapter"
    assert "255" in data["errors"][0]["msg"] or "memory" in data["errors"][0]["msg"].lower()


def test_refusal_on_stdout_is_also_skipped(tmp_path: Path) -> None:
    """Some phpstan builds put the refusal on stdout; same non-verdict."""
    data = _run_adapter(tmp_path, stdout=" [ERROR] No files found to analyse.", exit_code=1)
    assert not _is_bare_clean(data)
    assert "skipped" in data


def test_genuine_clean_is_still_clean(tmp_path: Path) -> None:
    """The guard must not turn real passes into skips."""
    data = _run_adapter(tmp_path, stdout='{"totals": {"file_errors": 0}, "files": {}}')
    assert _is_bare_clean(data)


def test_findings_still_reported_when_exit_nonzero(tmp_path: Path) -> None:
    """PHPStan exits 1 whenever it finds anything — that must stay a finding."""
    payload = json.dumps({
        "totals": {"file_errors": 1},
        "files": {"Child.php": {"messages": [
            {"line": 2, "identifier": "property.extraNativeType", "message": "boom"}]}},
    })
    data = _run_adapter(tmp_path, stdout=payload, exit_code=1)
    assert data["ok"] is False
    assert "skipped" not in data
    assert data["errors"][0]["code"] == "property.extraNativeType"


# ---------------------------------------------------------------------------
# (1) Inheritance IS visible in single-file scope — regression lock
# ---------------------------------------------------------------------------

def test_inheritance_identifier_survives_the_adapter(tmp_path: Path) -> None:
    """`property.extraNativeType` must reach the caller intact, not be filtered."""
    payload = json.dumps({
        "totals": {"file_errors": 1},
        "files": {"Child.php": {"messages": [{
            "line": 2,
            "identifier": "property.extraNativeType",
            "message": "Property Child::$label (string) overriding property "
                       "ParentC::$label should not have a native type.",
        }]}},
    })
    data = _run_adapter(tmp_path, stdout=payload, exit_code=1)
    assert data["ok"] is False
    assert data["errors"][0]["code"] == "property.extraNativeType"
    assert "should not have a native type" in data["errors"][0]["msg"]


@functools.lru_cache(maxsize=1)
def _phpstan_ready() -> bool:
    """Can this env run a real project-config phpstan and emit JSON?"""
    if not shutil.which("phpstan"):
        return False
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "probe.php"
        f.write_text("<?php\n$x = 1;\n")
        try:
            r = subprocess.run([sys.executable, str(PHPSTAN_PY), str(f)],
                               capture_output=True, text=True, timeout=adapter_budget(PHPSTAN_PY))
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        data = json.loads(r.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return False
    return data.get("tool") == "phpstan" and not any(
        e.get("code") == "adapter" for e in (data.get("errors") or []))


@pytest.mark.skipif(not _phpstan_ready(), reason="no project phpstan emitting JSON here")
def test_real_phpstan_sees_the_parent_in_single_file_scope(tmp_path: Path) -> None:
    """The claim in #263, tested against the real analyser.

    Parent untyped, child native-typed, analyse the CHILD FILE ONLY. PHPStan
    resolves the parent through the config's `paths` and reports the error.
    Single-file scope is not the blind spot the issue described.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "ParentC.php").write_text("<?php\nnamespace R;\nclass ParentC { protected $label; }\n")
    (src / "ChildC.php").write_text(
        "<?php\nnamespace R;\nclass ChildC extends ParentC { protected string $label = ''; }\n")
    (tmp_path / "phpstan.neon").write_text(
        "parameters:\n    level: 8\n    paths:\n        - src\n")
    r = subprocess.run(
        [sys.executable, str(PHPSTAN_PY), "src/ChildC.php"],
        capture_output=True, text=True, timeout=adapter_budget(PHPSTAN_PY), cwd=str(tmp_path),
    )
    data = json.loads(r.stdout.strip())
    assert data["ok"] is False, "single-file scope reported CLEAN on an inheritance error"
    assert any(e["code"] == "property.extraNativeType" for e in data["errors"]), data

def test_skip_reason_names_the_refusal_not_the_preamble(tmp_path: Path) -> None:
    """Real runs put a `Note: Using configuration file ...` line first.

    Reporting that as the skip reason tells the reader nothing about why the
    file went unanalysed. The reason must be the line that actually refused.
    """
    data = _run_adapter(
        tmp_path,
        stderr=chr(10).join([
            "Note: Using configuration file /repo/phpstan.neon.",
            "",
            " [ERROR] No files found to analyse.",
        ]),
        exit_code=1,
    )
    assert "skipped" in data
    assert "no files found to analyse" in data["skipped"].lower(), data["skipped"]
    assert "Using configuration file" not in data["skipped"]
