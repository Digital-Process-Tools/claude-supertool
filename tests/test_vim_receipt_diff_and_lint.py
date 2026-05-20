"""Tests for op_vim receipt enhancements: unified diff + post-edit lint.

Kevin re-reads files after every vim::: to verify the edit landed. These
two additions put verification in-band:
  B) Unified diff of changed regions (-old +new ±2 ctx, capped at 5 hunks)
  C) Post-edit lint per extension (php -l, json, xmllint, py_compile)
"""
from __future__ import annotations

import shutil
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
        assert "POST-EDIT LINT FAILED" in out
        assert "xmllint" in out
    else:
        assert "xmllint" not in out


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
    assert f.read_text() != original


def test_vim_lint_fail_on_json_warns_no_rollback(tmp_path):
    """Same warning for .json — broken JSON persists, receipt warns."""
    f = tmp_path / "x.json"
    original = '{"a": 1}\n'
    f.write_text(original)
    out = supertool.op_vim(str(f), "G$xx")
    assert "POST-EDIT LINT FAILED" in out
    assert "file modified despite syntax fail" in out
    assert f.read_text() != original
