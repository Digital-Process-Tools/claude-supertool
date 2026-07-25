"""Read/navigation friction fixes from a heavy real-world session (#363).

Four separate frictions, all in the ops hit every turn:

1. `between:` refused a symbol written the way it appears in source
   (`async function foo`, `function bar`) — the matcher wanted the bare name.
2. `grep` dumped a pathological 25KB single line (one-line PHPDoc) verbatim,
   eating a screenful for one hit.
3. `glob` is repo-root relative, so `glob:SiBrief/**/*.php` returned 0 for a
   dir nested deeper — while `grep` happily took the same mid-path segment.
4. cwd drift died instead of recovering: after a `cd` into a subdir, a
   root-relative path arg failed with "wrong CWD?" and needed a manual
   `cwd:` retry.
"""
from __future__ import annotations

import os

import pytest

import supertool
from tests.conftest import _has_any_tree_sitter

requires_ts = pytest.mark.skipif(
    not _has_any_tree_sitter(), reason="no tree-sitter package installed"
)


@pytest.fixture
def restore_cwd():
    saved = os.getcwd()
    yield
    os.chdir(saved)


@pytest.fixture(autouse=True)
def no_rtk(monkeypatch):
    """grep delegates to rtk when available — force the native walker so the
    per-line cap under test is the one that runs."""
    monkeypatch.setattr(supertool, "_rtk_enabled", lambda: False)


# ---------------------------------------------------------------------------
# 1. between: symbol written as it appears in source
# ---------------------------------------------------------------------------

@requires_ts
def test_between_accepts_async_function_prefix(tmp_path, enable_tree_sitter) -> None:
    f = tmp_path / "helpers.js"
    f.write_text(
        "async function fillAndSubmit(page) {\n"
        "  await page.click('#go');\n"
        "}\n"
    )
    out = supertool.op_between_symbol("async function fillAndSubmit", str(f))
    assert "ERROR" not in out
    assert "await page.click" in out


@requires_ts
def test_between_accepts_php_function_keyword_prefix(tmp_path, enable_tree_sitter) -> None:
    f = tmp_path / "Proxy.php"
    f.write_text(
        "<?php\n"
        "function getReorderEntityHasFileComponent(): Form\n"
        "{\n"
        "    return new Form();\n"
        "}\n"
    )
    out = supertool.op_between_symbol(
        "function getReorderEntityHasFileComponent", str(f))
    assert "ERROR" not in out
    assert "return new Form();" in out


@requires_ts
def test_between_accepts_trailing_call_parens(tmp_path, enable_tree_sitter) -> None:
    f = tmp_path / "helpers.js"
    f.write_text("function fillAndSubmit(page) {\n  return 1;\n}\n")
    out = supertool.op_between_symbol("fillAndSubmit(", str(f))
    assert "ERROR" not in out
    assert "return 1;" in out


@requires_ts
def test_between_bare_symbol_still_works(tmp_path, enable_tree_sitter) -> None:
    f = tmp_path / "helpers.js"
    f.write_text("function fillAndSubmit(page) {\n  return 1;\n}\n")
    out = supertool.op_between_symbol("fillAndSubmit", str(f))
    assert "ERROR" not in out
    assert "return 1;" in out


@requires_ts
def test_between_unknown_symbol_still_errors(tmp_path, enable_tree_sitter) -> None:
    f = tmp_path / "helpers.js"
    f.write_text("function fillAndSubmit(page) {\n  return 1;\n}\n")
    out = supertool.op_between_symbol("function nope", str(f))
    assert "ERROR" in out


def test_normalize_symbol_query_strips_modifiers() -> None:
    n = supertool._normalize_symbol_query
    assert n("async function fillAndSubmit") == "fillAndSubmit"
    assert n("public static function getFoo") == "getFoo"
    assert n("class Foo") == "Foo"
    assert n("def parse_thing") == "parse_thing"
    assert n("fillAndSubmit(page)") == "fillAndSubmit"
    assert n("plainName") == "plainName"
    # A symbol that IS a keyword must survive — don't strip it to nothing.
    assert n("function") == "function"


# ---------------------------------------------------------------------------
# 2. grep per-line cap
# ---------------------------------------------------------------------------

def test_grep_caps_pathological_long_line(tmp_path) -> None:
    f = tmp_path / "Big.php"
    f.write_text("<?php\n/** @extends " + ("Foo, " * 6000) + "NEEDLE */\n")
    out = supertool.op_grep("NEEDLE", str(f), limit=5, no_auto_read=True)
    hit = [ln for ln in out.splitlines() if "…" in ln]
    assert hit, out[:300]
    assert len(hit[0]) < 800
    assert "chars" in hit[0]  # tells the reader what was dropped


def test_grep_leaves_normal_lines_intact(tmp_path) -> None:
    f = tmp_path / "Small.php"
    f.write_text("<?php\n$needle = 'here';\n")
    out = supertool.op_grep("needle", str(f), limit=5, no_auto_read=True)
    assert "$needle = 'here';" in out
    assert "…" not in out


def test_grep_line_cap_is_configurable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_GREP_MAX_LINE_CHARS", "20")
    f = tmp_path / "Small.php"
    f.write_text("<?php\n$needle = '" + "x" * 200 + "';\n")
    out = supertool.op_grep("needle", str(f), limit=5, no_auto_read=True)
    assert "…" in out


def test_grep_context_mode_also_capped(tmp_path) -> None:
    f = tmp_path / "Big.php"
    f.write_text("<?php\n/** " + ("Foo, " * 6000) + "NEEDLE */\n$x = 1;\n")
    out = supertool.op_grep("NEEDLE", str(f), limit=5, context=2,
                            no_auto_read=True)
    assert "…" in out
    assert max(len(ln) for ln in out.splitlines()) < 800


# ---------------------------------------------------------------------------
# 3. glob mid-path segment
# ---------------------------------------------------------------------------

def test_glob_retries_pattern_as_mid_path_segment(tmp_path, restore_cwd) -> None:
    target = tmp_path / "Dvsi" / "src2" / "SiBrief" / "Components"
    target.mkdir(parents=True)
    (target / "BriefForm.php").write_text("<?php\n")
    os.chdir(tmp_path)

    out = supertool.op_glob("SiBrief/**/*.php", no_auto_read=True)

    assert "BriefForm.php" in out
    assert "mid-path" in out  # the retry is announced, not silent


def test_glob_direct_match_does_not_announce_retry(tmp_path, restore_cwd) -> None:
    (tmp_path / "SiBrief").mkdir()
    (tmp_path / "SiBrief" / "BriefForm.php").write_text("<?php\n")
    os.chdir(tmp_path)

    out = supertool.op_glob("SiBrief/**/*.php", no_auto_read=True)

    assert "BriefForm.php" in out
    assert "mid-path" not in out


def test_glob_genuine_zero_stays_zero(tmp_path, restore_cwd) -> None:
    os.chdir(tmp_path)
    out = supertool.op_glob("Nope/**/*.php", no_auto_read=True)
    assert "(0 files)" in out


# ---------------------------------------------------------------------------
# 4. cwd drift auto-recovery
# ---------------------------------------------------------------------------

def _project(tmp_path):
    (tmp_path / ".supertool.json").write_text("{}\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x = 1\n")
    sub = tmp_path / "tests" / "e2e"
    sub.mkdir(parents=True)
    return sub


def test_main_auto_resolves_to_project_root(tmp_path, monkeypatch, capsys,
                                            restore_cwd) -> None:
    sub = _project(tmp_path)
    os.chdir(sub)
    seen: list[str] = []
    monkeypatch.setattr(supertool, "dispatch",
                        lambda a: (seen.append(os.getcwd()), "")[-1])
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    supertool.main(["read:src/foo.py"])

    assert os.path.realpath(seen[0]) == os.path.realpath(str(tmp_path))
    assert "auto-resolved" in capsys.readouterr().out


def test_main_no_chdir_when_path_resolves_locally(tmp_path, monkeypatch,
                                                  restore_cwd) -> None:
    sub = _project(tmp_path)
    (sub / "foo.py").write_text("y = 2\n")
    os.chdir(sub)
    seen: list[str] = []
    monkeypatch.setattr(supertool, "dispatch",
                        lambda a: (seen.append(os.getcwd()), "")[-1])
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    supertool.main(["read:foo.py"])

    assert os.path.realpath(seen[0]) == os.path.realpath(str(sub))


def test_main_explicit_cwd_op_disables_auto_resolve(tmp_path, monkeypatch,
                                                    restore_cwd) -> None:
    sub = _project(tmp_path)
    os.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(supertool, "dispatch",
                        lambda a: (seen.append(os.getcwd()), "")[-1])
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    supertool.main([f"cwd:{sub}", "read:src/foo.py"])

    assert os.path.realpath(seen[0]) == os.path.realpath(str(sub))


def test_main_no_chdir_without_project_marker(tmp_path, monkeypatch,
                                              restore_cwd) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x = 1\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    os.chdir(sub)
    seen: list[str] = []
    monkeypatch.setattr(supertool, "dispatch",
                        lambda a: (seen.append(os.getcwd()), "")[-1])
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    supertool.main(["read:src/foo.py"])

    assert os.path.realpath(seen[0]) == os.path.realpath(str(sub))


def test_auto_cwd_root_ignores_at_payloads_and_flags(tmp_path, restore_cwd) -> None:
    sub = _project(tmp_path)
    os.chdir(sub)
    # No op arg names an existing root-relative path -> nothing to recover from.
    assert supertool._auto_cwd_root(["git-status", "grep:foo:.:20"]) is None
    assert supertool._auto_cwd_root(["edit:@-"]) is None
    assert supertool._auto_cwd_root(["read:src/foo.py"]) == os.path.realpath(
        str(tmp_path))
