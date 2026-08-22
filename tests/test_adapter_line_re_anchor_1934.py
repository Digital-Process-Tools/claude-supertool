"""A path-embedded ":N:M: " supplies the reported line/col (#1934).

The actionlint adapter parsed its own path:line:col: message output with a
non-greedy .*? standing in for the path -- so the regex bound to the
*earliest* :digit:digit: in the line rather than the one the tool actually
wrote. A workflow file named to contain its own ":1:1: " sequence supplied the
reported line, column and message chosen by the filename author, discarding
actionlint's real diagnostic. Reachable end to end: _supertool.py
shlex.quote()s the file before substituting it into the adapter's argv, so a
space-bearing filename survives shlex.split into sys.argv[1] intact.

The same non-greedy path-discarding shape was pre-existing in four sibling
adapters that all parse a path:line[:col]: message line the same way:
xmllint, ruby-check, hadolint, gofmt-check.

Fixed by anchoring each adapter's line regex on re.escape(file) -- the path
the adapter actually invoked -- instead of a .*?/.+? that matches the
earliest colon-digit run anywhere earlier in the line. Only the path that was
passed in can now start a match; a filename cannot forge one.

The control pair per adapter: a file whose name embeds ":1:1: " must report
the tool's real line/col, and an ordinary filename must keep reporting
exactly what it reports today.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget

VALIDATORS = Path(__file__).parent.parent / "validators"

_IN_PROCESS = {
    name: VALIDATORS / name / f"{name}.py"
    for name in ("xmllint", "ruby-check", "gofmt-check", "actionlint")
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name.replace(chr(45), chr(95))}_1934", _IN_PROCESS[name])
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


xmllint = _load("xmllint")
ruby_check = _load("ruby-check")
gofmt_check = _load("gofmt-check")
actionlint = _load("actionlint")


# ---------------------------------------------------------------------------
# xmllint, ruby-check, gofmt-check -- parse_diagnostics(out, file) is already
# extracted, so the class is driven in process on every platform.
# ---------------------------------------------------------------------------

def test_xmllint_anchors_on_the_invoked_path_not_the_earliest_colon() -> None:
    evil = "x:1: parser error : fake .xml"
    line = evil + ":7: real parser error: mismatched tag"
    found = xmllint.parse_diagnostics(line, evil)
    assert len(found) == 1, found
    assert found[0]["line"] == 7, found


def test_xmllint_ordinary_filename_is_unaffected() -> None:
    line = "subject.xml:2: parser error : Opening and ending tag mismatch"
    found = xmllint.parse_diagnostics(line, "subject.xml")
    assert len(found) == 1, found
    assert found[0]["line"] == 2, found


def test_ruby_check_anchors_on_the_invoked_path_not_the_earliest_colon() -> None:
    evil = "x:1: syntax error, fake .rb"
    line = evil + ":9: syntax error, unexpected end-of-input"
    found = ruby_check.parse_diagnostics(line, evil)
    assert len(found) == 1, found
    assert found[0]["line"] == 9, found


def test_ruby_check_ordinary_filename_is_unaffected() -> None:
    line = "subject.rb:2: syntax error, unexpected end-of-input"
    found = ruby_check.parse_diagnostics(line, "subject.rb")
    assert len(found) == 1, found
    assert found[0]["line"] == 2, found


def test_gofmt_check_anchors_on_the_invoked_path_not_the_earliest_colon() -> None:
    evil = "x:1:1: expected fake .go"
    line = evil + ":7:15: expected close paren, found brace"
    found = gofmt_check.parse_diagnostics(line, evil)
    assert len(found) == 1, found
    assert found[0]["line"] == 7 and found[0]["col"] == 15, found


def test_gofmt_check_ordinary_filename_is_unaffected() -> None:
    line = "subject.go:3:12: expected close paren, found brace"
    found = gofmt_check.parse_diagnostics(line, "subject.go")
    assert len(found) == 1, found
    assert found[0]["line"] == 3 and found[0]["col"] == 12, found


# ---------------------------------------------------------------------------
# actionlint, hadolint -- end to end via a fake binary on PATH, matching the
# fixture in tests/test_adapter_tool_vs_file_753.py. POSIX only: on Windows
# CreateProcess ignores PATHEXT for an extensionless program name, so a .bat
# shim on PATH cannot intercept these adapters' spawns.
# ---------------------------------------------------------------------------

_SPAWNED = {
    "actionlint": VALIDATORS / "actionlint" / "actionlint.py",
    "hadolint": VALIDATORS / "hadolint" / "hadolint.py",
}

posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="fake-binary fixture cannot intercept extensionless spawns on Windows (see #753)",
)


def _fake_tool(tmp_path: Path, name: str, stdout: str) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / f"fake_{name}.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    launcher = bindir / name
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    return bindir


def _run_with_fake(tmp_path: Path, adapter: str, binary: str,
                    target: Path, stdout: str) -> dict:
    import json
    bindir = _fake_tool(tmp_path, binary, stdout)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        [sys.executable, str(_SPAWNED[adapter]), str(target)],
        capture_output=True, text=True, env=env,
        timeout=adapter_budget(_SPAWNED[adapter]), encoding="utf-8", errors="replace",
    )
    return json.loads(result.stdout)


@posix_only
def test_actionlint_anchors_on_the_invoked_path_not_the_earliest_colon(tmp_path: Path) -> None:
    # actionlint reports the path it was invoked with *relative to its own
    # CWD*, which this adapter inherits (verified against real actionlint
    # 1.7.12, see actionlint.py's `_line_re`) — so the fake tool must mimic
    # that relativisation, not echo the absolute argv path back verbatim.
    target = tmp_path / "x:1:1: workflow is valid, 0 problems .yml"
    target.write_text("on: push\n")
    reported = os.path.relpath(target)
    stdout = reported + ":7:15: specifying action \"bogus\" is not allowed [action]\n"
    out = _run_with_fake(tmp_path, "actionlint", "actionlint", target, stdout)
    assert out["ok"] is False, out
    assert out["count"] == 1, out
    err = out["errors"][0]
    assert err["line"] == 7 and err["col"] == 15, err
    assert "bogus" in err["msg"], err


@posix_only
def test_actionlint_ordinary_filename_is_unaffected(tmp_path: Path) -> None:
    target = tmp_path / "workflow.yml"
    target.write_text("on: push\n")
    reported = os.path.relpath(target)
    stdout = reported + ":3:5: unexpected key \"foo\" [syntax-check]\n"
    out = _run_with_fake(tmp_path, "actionlint", "actionlint", target, stdout)
    assert out["ok"] is False, out
    err = out["errors"][0]
    assert err["line"] == 3 and err["col"] == 5, err


@posix_only
def test_hadolint_anchors_on_the_invoked_path_not_the_earliest_colon(tmp_path: Path) -> None:
    target = tmp_path / "x:1 DL9999 error: fake .Dockerfile"
    target.write_text("FROM ubuntu\n")
    stdout = str(target) + ":7 DL3006 warning: real hadolint finding\n"
    out = _run_with_fake(tmp_path, "hadolint", "hadolint", target, stdout)
    assert out["ok"] is False, out
    assert out["count"] == 1, out
    err = out["errors"][0]
    assert err["line"] == 7, err
    assert err["code"] == "DL3006", err


@posix_only
def test_hadolint_ordinary_filename_is_unaffected(tmp_path: Path) -> None:
    target = tmp_path / "Dockerfile"
    target.write_text("FROM ubuntu\n")
    stdout = str(target) + ":5 DL3007 warning: using latest is prone to errors\n"
    out = _run_with_fake(tmp_path, "hadolint", "hadolint", target, stdout)
    assert out["ok"] is False, out
    err = out["errors"][0]
    assert err["line"] == 5 and err["code"] == "DL3007", err


# ---------------------------------------------------------------------------
# actionlint's Windows cross-drive fallback (`except ValueError: reported =
# file` in actionlint.py's `_line_re`) has no Windows machine with actionlint
# installed to verify against, so it is reasoned rather than observed (see
# the comment above `_line_re`). This is not that verification -- it cannot
# be, absent that machine -- but it does pin the fallback's own logic at the
# unit level: forcing `os.path.relpath` to raise confirms the adapter falls
# back to anchoring on the literal `file` string rather than raising, or
# silently matching nothing.
# ---------------------------------------------------------------------------

def test_actionlint_relpath_failure_falls_back_to_the_literal_path(monkeypatch) -> None:
    def _raise(*_a, **_kw):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(actionlint.os.path, "relpath", _raise)
    file = "D:\\workflows\\deploy.yml"
    line = file + ":7:15: specifying action \"bogus\" is not allowed [action]"
    found = actionlint.parse_diagnostics(line, file)
    assert len(found) == 1, found
    assert found[0]["line"] == 7 and found[0]["col"] == 15, found
