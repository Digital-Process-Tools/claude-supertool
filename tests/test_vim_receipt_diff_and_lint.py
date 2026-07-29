"""Tests for op_vim receipt enhancements: unified diff + post-edit lint.

Kevin re-reads files after every vim::: to verify the edit landed. These
two additions put verification in-band:
  B) Unified diff of changed regions (-old +new ±2 ctx, capped at 5 hunks)
  C) Post-edit lint per extension (php -l, json, xmllint, py_compile)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# B) Unified diff section
# ---------------------------------------------------------------------------

def test_simple_edit_shows_diff_section(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    out = supertool.op_vim(str(f), "/beta␞ccBETA")
    assert "--- diff" in out
    assert "-beta" in out
    assert "+BETA" in out


def test_noop_script_no_diff_changes(tmp_path: Path) -> None:
    """Pure cursor motion produces no diff hunks → 'no changes' marker."""
    f = tmp_path / "x.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    out = supertool.op_vim(str(f), "gg␞G")
    # Either omitted, or explicit 'no changes' note. Must NOT show -/+ lines.
    assert "--- diff: no changes ---" in out or "--- diff" not in out
    # Sanity: no spurious -/+ lines from a diff body
    assert "\n-alpha" not in out and "\n+alpha" not in out


def test_diff_hunks_capped_at_five(tmp_path: Path) -> None:
    """Many scattered edits → cap diff at 5 hunks with truncation footer."""
    # 20 lines, edit every line via consecutive cc — but that's one big
    # hunk. We want SCATTERED hunks. Use lines with blank-line separators
    # so each edit becomes its own hunk after grouping by context.
    lines = []
    for i in range(10):
        for j in range(5):
            lines.append(f"keep-a-{i}-{j}")
        lines.append(f"target-{i}")
        for j in range(5):
            lines.append(f"keep-b-{i}-{j}")
    f = tmp_path / "x.txt"
    f.write_text("\n".join(lines) + "\n")
    # Replace each 'target-N' line — 10 scattered hunks
    script_parts = []
    for i in range(10):
        script_parts.append(f"gg")
        script_parts.append(f"/target-{i}")
        script_parts.append(f"ccREPLACED-{i}")
    out = supertool.op_vim(str(f), "␞".join(script_parts))
    # We expect at most 5 hunks shown + a "more hunks" note
    hunk_count = out.count("@@ line ")
    assert hunk_count <= 5, f"got {hunk_count} hunks, want <= 5"
    assert "more hunks" in out


# ---------------------------------------------------------------------------
# C) Post-edit lint
# ---------------------------------------------------------------------------

def test_php_lint_success_when_php_available(tmp_path: Path) -> None:
    if not shutil.which("php"):
        return  # silently skip when php not installed
    f = tmp_path / "x.php"
    f.write_text("<?php\necho 'hi';\n")
    out = supertool.op_vim(str(f), "G␞oecho 'bye';")
    assert "--- lint: php -l ---" in out
    assert "FAILED" not in out


def test_php_lint_failure_makes_it_obvious(tmp_path: Path) -> None:
    if not shutil.which("php"):
        return
    f = tmp_path / "x.php"
    f.write_text("<?php\necho 'hi';\n")
    # Break syntax: append a stray '}' on a new line
    out = supertool.op_vim(str(f), "G␞o}")
    assert "POST-EDIT LINT FAILED" in out
    assert "php -l" in out


def test_json_lint_success(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text('{"a": 1}\n')
    out = supertool.op_vim(str(f), '/1␞r2')
    assert "--- lint: json ---" in out
    assert "FAILED" not in out


def test_json_lint_failure(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text('{"a": 1}\n')
    # Introduce trailing garbage breaking JSON
    out = supertool.op_vim(str(f), "G␞A,broken")
    assert "POST-EDIT LINT FAILED" in out
    assert "json" in out


def test_xml_lint(tmp_path: Path) -> None:
    f = tmp_path / "x.xml"
    f.write_text("<root><a/></root>\n")
    # Break it
    out = supertool.op_vim(str(f), "/<\\/root>␞x")
    if shutil.which("xmllint"):
        # A decline is a third state, not a lenient pass: this test still
        # demands a verdict, and says which of the two it did not get (#553).
        assert "POST-EDIT LINT TIMED OUT" not in out, (
            "xmllint declined instead of reaching a verdict — the runner blew "
            "the lint budget, not the code. Raise SUPERTOOL_LINT_TIMEOUT "
            "(conftest sets it for the suite). The decline itself is pinned by "
            "test_vim_receipt_reports_a_lint_decline_not_a_verdict, not here.\\n"
            + out
        )
        assert "POST-EDIT LINT FAILED" in out
        assert "xmllint" in out
    else:
        assert "xmllint" not in out


def test_vim_receipt_reports_a_lint_decline_not_a_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    """#553 — the third state has to survive all the way into the receipt.

    `_vim_render_lint` is already pinned at unit level (#396), but nothing
    pinned the decline where the caller actually reads it. An op_vim receipt
    whose lint section is empty reads as a file that linted clean, so a lint
    that never finished must name itself, the tool, and the knob.

    The timeout is forced here rather than waited for: pinning this by letting
    a slow runner produce one by accident is what made master red.
    """
    real_which = shutil.which
    real_run = subprocess.run

    def fake_which(name: str, *a, **kw):
        # xmllint is absent from some runners; the decline is not about that.
        return "/usr/bin/xmllint" if name == "xmllint" else real_which(name, *a, **kw)

    def timing_out_xmllint(*a, **k):
        cmd = a[0] if a else k.get("args")
        if cmd and "xmllint" in cmd[0]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=k.get("timeout", 5))
        return real_run(*a, **k)

    monkeypatch.setattr(supertool.shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", timing_out_xmllint)

    f = tmp_path / "x.xml"
    f.write_text("<root><a/></root>\\n")
    out = supertool.op_vim(str(f), "/<\\/root>␞x")

    assert "POST-EDIT LINT TIMED OUT" in out
    assert "xmllint" in out
    assert "was NOT checked" in out
    assert "SUPERTOOL_LINT_TIMEOUT" in out, "the decline must name the knob to raise"
    # Neither verdict may be implied. "FAILED" would blame the file for the
    # runner; a bare "--- lint: xmllint ---" would claim a check that never ran.
    assert "POST-EDIT LINT FAILED" not in out
    assert "--- lint: xmllint ---" not in out


def test_py_lint_failure(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("def f():\n    return 1\n")
    # Break syntax with stray colon
    out = supertool.op_vim(str(f), "G␞o:bad syntax")
    assert "POST-EDIT LINT FAILED" in out
    assert "py_compile" in out


def test_py_lint_success(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("def f():\n    return 1\n")
    out = supertool.op_vim(str(f), "G␞oprint('ok')")
    assert "--- lint: py_compile ---" in out
    assert "FAILED" not in out


def test_unknown_extension_no_lint(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    out = supertool.op_vim(str(f), "A world")
    assert "--- lint" not in out


def test_missing_binary_silently_omitted(tmp_path: Path, monkeypatch) -> None:
    """If lint binary isn't on PATH, omit the section without erroring."""
    # Force shutil.which to claim php is absent
    real_which = shutil.which

    def fake_which(name: str, *a, **kw):
        if name == "php":
            return None
        return real_which(name, *a, **kw)

    monkeypatch.setattr(supertool.shutil, "which", fake_which)
    f = tmp_path / "x.php"
    f.write_text("<?php\necho 'hi';\n")
    out = supertool.op_vim(str(f), "G␞oecho 'bye';")
    # Op should still succeed (cursor info present), lint section absent
    assert "cursor at" in out
    assert "--- lint: php -l ---" not in out
    assert "POST-EDIT LINT FAILED" not in out

def test_vim_lint_fail_receipt_warns_no_rollback(tmp_path):
    """vim's post-edit lint is informational only — no auto-rollback. The
    receipt MUST say so unambiguously so the caller knows to restore manually
    or configure a validator with rollback_on_fail.
    """
    f = tmp_path / "x.py"
    original = "def foo():\n    if True:\n        pass\n"
    f.write_text(original)
    out = supertool.op_vim(str(f), "/if True:\x1bo    broken_indent = 1\x1b")
    assert "POST-EDIT LINT FAILED" in out
    assert "file modified despite syntax fail" in out, (
        f"receipt should warn the file was modified despite lint fail, got:\n{out}"
    )
    # Documents current no-rollback behavior — file IS modified.
    assert f.read_text(encoding="utf-8") != original


def test_vim_lint_fail_on_json_warns_no_rollback(tmp_path):
    """Same warning for .json — broken JSON persists, receipt warns."""
    f = tmp_path / "x.json"
    original = '{"a": 1}\n'
    f.write_text(original)
    out = supertool.op_vim(str(f), "G$xx")
    assert "POST-EDIT LINT FAILED" in out
    assert "file modified despite syntax fail" in out
    assert f.read_text(encoding="utf-8") != original

# ---------------------------------------------------------------------------
# #559 / #560 — a decline is a third state, and it has to be readable at a
# glance, not only by whoever reads the whole section.
# ---------------------------------------------------------------------------

def _force_xmllint_to_time_out(monkeypatch) -> None:
    """Produce a lint decline deliberately (the #553/#558 pattern).

    Waiting for a slow runner to make one is what put master red; xmllint is
    also absent from some runners, and the decline is not about that.
    """
    real_which = shutil.which
    real_run = subprocess.run

    def fake_which(name: str, *a, **kw):
        return "/usr/bin/xmllint" if name == "xmllint" else real_which(name, *a, **kw)

    def timing_out_xmllint(*a, **k):
        cmd = a[0] if a else k.get("args")
        if cmd and "xmllint" in cmd[0]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=k.get("timeout", 5))
        return real_run(*a, **k)

    monkeypatch.setattr(supertool.shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", timing_out_xmllint)


def test_a_lint_decline_says_the_file_was_modified_and_not_checked(
    tmp_path: Path, monkeypatch
) -> None:
    """#560 — the warning fires on a syntax fail and nowhere else.

    Everywhere else in a receipt, the absence of "review or restore manually"
    means the edit came out clean. On a decline it means nobody checked, and
    the file was written anyway — the one state where the file is least
    verified is the one that reads most reassuring.
    """
    _force_xmllint_to_time_out(monkeypatch)
    f = tmp_path / "x.xml"
    original = "<root><a/></root>\n"
    f.write_text(original)
    out = supertool.op_vim(str(f), "/<\\/root>␞x")

    assert "POST-EDIT LINT TIMED OUT" in out
    assert f.read_text(encoding="utf-8") != original, "the file must really be modified"
    assert "NOT checked" in out
    assert "review or restore manually" in out, (
        "a scanning reader takes the absence of this line as 'clean', and here "
        f"nothing was checked at all:\n{out}"
    )
    # The decline is not a failure: nothing was found wrong with the file.
    assert "despite syntax fail" not in out
    assert "POST-EDIT LINT FAILED" not in out


def test_the_python_lint_runs_the_interpreter_that_is_running_supertool(
    tmp_path: Path, monkeypatch
) -> None:
    """#559 — `python3` is a PATH bet this repo already lost once (#529).

    On Windows the literal name resolves to the App Execution Alias stub,
    which blocks instead of erroring (PR #527's Windows/3.10 leg), or is
    absent entirely. `sys.executable` is the interpreter already running this
    process: present by construction, Python 3 by construction, and never a
    stray Python 2 or the wrong venv.
    """
    seen: dict = {}
    real_run = subprocess.run

    def capture(*a, **k):
        cmd = a[0] if a else k.get("args")
        if cmd and "py_compile" in list(cmd):
            seen["cmd"] = list(cmd)
        return real_run(*a, **k)

    monkeypatch.setattr(subprocess, "run", capture)
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.op_vim(str(f), "G␞ob = 2")

    assert "--- lint: py_compile ---" in out
    assert seen.get("cmd"), f"py_compile never spawned:\n{out}"
    assert seen["cmd"][0] == sys.executable, (
        f"py_compile spawned {seen['cmd'][0]!r}, not the running interpreter"
    )


def test_a_python_file_with_no_interpreter_declines_rather_than_falling_silent(
    tmp_path: Path, monkeypatch
) -> None:
    """#559 — silence is reserved for files no linter applies to.

    A `.py` file has one. If the interpreter cannot be sourced, the check did
    not run and the receipt has to say so: an empty lint section reads as
    "linted clean" everywhere else in the tool.
    """
    monkeypatch.setattr(supertool.sys, "executable", "")
    f = tmp_path / "x.py"
    original = "a = 1\n"
    f.write_text(original)
    out = supertool.op_vim(str(f), "G␞ob = 2")

    assert "POST-EDIT LINT DECLINED" in out, (
        f"a Python file that was never checked rendered as clean:\n{out}"
    )
    assert "py_compile" in out
    assert "NOT checked" in out
    assert "--- lint: py_compile ---" not in out, "no check ran; none may be claimed"
    assert "POST-EDIT LINT FAILED" not in out, "nothing was found wrong with the file"
    assert "review or restore manually" in out
