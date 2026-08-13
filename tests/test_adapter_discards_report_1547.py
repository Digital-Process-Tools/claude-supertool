"""#1547 — an adapter discards or fabricates a verdict on a path where the tool did produce something.

Two of the three filed instances. The third (recovering a JSON object out of
polluted stdout in `validators/phpstan/phpstan.py`) is deliberately not fixed:
the current arm is already a loud `adapter` non-verdict carrying the unreadable
bytes, and recovering an object from noise means choosing a brace and therefore
a `count`, which feeds `_validator_regressed`. A guessed count trades a loud
non-verdict for a quiet wrong number.

Instance 1 — `phpstan` exiting 0 with empty stdout.

    Measured, phpstan 2.1.55, `analyse --no-progress --error-format=json Ok.php`
    on a clean file:

        {"totals":{"errors":0,"file_errors":0},"files":{},"errors":[]}  rc=0

    Under that formatter a clean run always writes the object. So rc 0 with
    nothing on stdout is not a clean file, it is a run that produced no report —
    and the adapter used to synthesise `{"totals": {"file_errors": 0}}` for it,
    which is `ok: true` about a file nothing opened.

Instance 3 — `phpmd-mcp` returning early on `structuredContent["error"]`.

    The branch discarded `structuredContent["output"]` unread. Whether the
    daemon can set both is not establishable from this repository (the server is
    `mcp-phpmd-warm`, external, and not installed on the machine this was written
    on), so the adapter is made not to depend on the answer: a report that
    arrived is never dropped in favour of a message about it. Same rule #1527
    applied to the cold phpstan adapter — a declination and a report are mutually
    exclusive claims, and the report is the one with evidence in it.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PHPSTAN_PY = REPO / "validators" / "phpstan" / "phpstan.py"
PHPMD_MCP_PY = REPO / "validators" / "phpmd-mcp" / "phpmd-mcp.py"

CLEAN_JSON = json.dumps({"totals": {"errors": 0, "file_errors": 0},
                         "files": {}, "errors": []})

ALLOWLIST_MSG = "analyse: path is outside the configured --paths allowlist."

REPORT = json.dumps({"files": [{"file": "/x/A.php", "violations": [
    {"beginLine": 12, "rule": "UnusedLocalVariable",
     "description": "Avoid unused local variables such as $tmp."},
    {"beginLine": 30, "rule": "CyclomaticComplexity",
     "description": "The method run() has a Cyclomatic Complexity of 14."},
]}]})


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive_phpstan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *,
                   stdout: str = "", stderr: str = "", rc: int = 0) -> dict:
    """Run the real cold adapter against a canned phpstan exit, in process.

    In process rather than through a PATH shim so it runs on Windows too.
    """
    mod = _load(PHPSTAN_PY, "phpstan_1547")
    target = tmp_path / "A.php"
    target.write_text("<?php" + chr(10), encoding="utf-8")
    monkeypatch.setattr(mod.shutil, "which", lambda _b: "/usr/bin/php")
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(stdout=stdout, stderr=stderr,
                                              returncode=rc))
    monkeypatch.setattr(mod.sys, "argv", ["phpstan.py", str(target)])
    seen: list = []
    monkeypatch.setattr(mod, "emit", seen.append)
    mod.main()
    assert len(seen) == 1, seen
    return seen[0]


def _phpmd(**structured) -> dict:
    mod = _load(PHPMD_MCP_PY, "phpmd_mcp_1547")
    return mod.format_response(
        "src/A.php",
        {"jsonrpc": "2.0", "id": 2,
         "result": {"structuredContent": structured}},
        11)


# ---------------------------------------------------------------------------
# Instance 1 — rc 0 and no output is not a clean file
# ---------------------------------------------------------------------------

def test_exit_zero_with_no_output_is_skipped_not_clean(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = _drive_phpstan(monkeypatch, tmp_path, stdout="", stderr="", rc=0)
    assert "skipped" in out, f"expected the third state, got {out!r}"
    assert out.get("ok") is not True
    # #515: a skip omits the verdict keys rather than padding them.
    assert "ok" not in out and "count" not in out and "errors" not in out, out
    assert "no output" in out["skipped"].lower(), out["skipped"]


def test_a_real_clean_run_still_reports_clean(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The guard against buying instance 1 with a validator that never passes."""
    out = _drive_phpstan(monkeypatch, tmp_path, stdout=CLEAN_JSON, rc=0)
    assert "skipped" not in out, out
    assert out["ok"] is True and out["count"] == 0


def test_exit_zero_no_output_but_stderr_says_why(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A recognised refusal keeps its own reason — this arm is above the new one."""
    out = _drive_phpstan(monkeypatch, tmp_path, stdout="",
                         stderr="Note: Using configuration file phpstan.neon."
                                + chr(10) + ALLOWLIST_MSG, rc=0)
    assert "skipped" in out, out
    assert "allowlist" in out["skipped"], out["skipped"]


# ---------------------------------------------------------------------------
# Instance 3 — a report that arrived is never dropped for a message about it
# ---------------------------------------------------------------------------

def test_runtime_error_beside_a_report_keeps_the_violations() -> None:
    out = _phpmd(error="SecurityError: could not write the result cache",
                 output=REPORT)
    assert "skipped" not in out, out
    codes = [e["code"] for e in out["errors"]]
    assert "UnusedLocalVariable" in codes and "CyclomaticComplexity" in codes, out
    # The error is not swallowed either: it is one more entry, not a replacement.
    assert any("SecurityError" in str(e["msg"]) for e in out["errors"]), out
    assert out["count"] == 3 and out["ok"] is False, out


def test_refusal_beside_a_report_publishes_the_report_not_a_skip() -> None:
    """A declination and a report are mutually exclusive; the report has evidence."""
    out = _phpmd(error=ALLOWLIST_MSG, output=REPORT)
    assert "skipped" not in out, f"a report was thrown away for a refusal: {out!r}"
    assert out["count"] == 2 and out["ok"] is False, out
    assert all("allowlist" not in str(e["msg"]) for e in out["errors"]), out


def test_refusal_beside_a_report_of_only_phpmd_errors_is_not_a_skip() -> None:
    """A PHPMD report has two bodies, and `errors` is the one nobody looks at.

    `files[].violations` is the obvious half; `report["errors"]` carries the
    processing failures (an unparseable PHP file, a broken ruleset) and is
    rendered by the same function three lines further down. A predicate that
    reads only the first half throws the second half away for the refusal —
    exactly the discard this issue is about, one key over.
    """
    out = _phpmd(error=ALLOWLIST_MSG,
                 output=json.dumps({"files": [], "errors": [
                     {"message": "Unable to parse file /x/A.php"}]}))
    assert "skipped" not in out, f"a report was thrown away for a refusal: {out!r}"
    assert out["count"] == 1 and out["ok"] is False, out
    assert "Unable to parse" in str(out["errors"][0]["msg"]), out


# --- guards: the single-key arms are unchanged -----------------------------

def test_error_alone_is_still_one_error() -> None:
    out = _phpmd(error="SecurityError: boom")
    assert "skipped" not in out and out["count"] == 1, out


def test_refusal_alone_is_still_skipped() -> None:
    out = _phpmd(error=ALLOWLIST_MSG)
    assert out.get("skipped"), out


def test_report_alone_is_unchanged() -> None:
    out = _phpmd(output=REPORT)
    assert out["count"] == 2 and out["ok"] is False, out

# --- guards: reordering the parse ahead of the error key changes nothing ----

def test_unparseable_output_beside_an_error_still_names_the_error() -> None:
    out = _phpmd(error="SecurityError: boom", output="<!-- not json -->")
    assert "skipped" not in out and out["count"] == 1, out
    assert "SecurityError" in str(out["errors"][0]["msg"]), out


def test_unparseable_output_beside_a_refusal_is_still_skipped() -> None:
    out = _phpmd(error=ALLOWLIST_MSG, output="<!-- not json -->")
    assert out.get("skipped"), out


def test_unparseable_output_alone_is_still_a_parse_error() -> None:
    out = _phpmd(output="<!-- not json -->")
    assert out["count"] == 1 and out["errors"][0]["code"] == "phpmd.parse", out

def test_a_clean_report_beside_an_error_still_reports_the_error() -> None:
    """No content in the report is not the same as no report.

    Green before this change too — it is here as the boundary of the arm above,
    not as a test of it.
    """
    out = _phpmd(error="SecurityError: boom",
                 output=json.dumps({"files": []}))
    assert "skipped" not in out, out
    assert out["count"] == 1 and out["ok"] is False, out
    assert "SecurityError" in str(out["errors"][0]["msg"]), out
