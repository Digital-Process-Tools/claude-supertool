"""Tests for presets/git/diff.py — the review-aware git-diff op.

Runs diff.py as a subprocess against throwaway repos in tmp_path, with project
policy supplied via SUPERTOOL_* env vars (the same way the dispatcher feeds it).
Covers: mode dispatch, classification, red-flag scan, forbidden-path guard,
test-pairing, clean-silence, plus regression guards for the two bugs found in
review (the `+++ ` added-line guard and the migration substring misclassify).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DIFF = Path(__file__).parent.parent / "presets" / "git" / "diff.py"


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], check=True, cwd=path)
    subprocess.run(["git", "config", "user.name", "T"], check=True, cwd=path)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], check=True, cwd=path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], check=True, cwd=path)


def _write(path: Path, rel: str, content: str) -> None:
    f = path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)


def _run(repo: Path, *args: str, env_extra: dict | None = None) -> str:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    res = subprocess.run(
        [sys.executable, str(DIFF), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=repo, env=env,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


DVSI_PAIRING = json.dumps([
    {"src": r"src2/(?P<rest>.+)\.class\.php$", "test": "tests/unit/{rest}Test.php"}
])
DVSI_FORBIDDEN = json.dumps([
    {"pattern": "/Generated/", "reason": "generated — edit the source class"}
])


def test_staged_red_flag_forbidden_and_missing_test(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src2/SiFoo/Foo.class.php",
           "<?php\nclass Foo {\n    function go() { var_dump($this); }\n}\n")
    _write(tmp_path, "src2/Generated/Bar.class.php", "<?php\nclass Bar {}\n")
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged",
               env_extra={"SUPERTOOL_FORBIDDEN_PATHS": DVSI_FORBIDDEN,
                          "SUPERTOOL_TEST_PAIRING": DVSI_PAIRING})

    assert "git-diff (staged)" in out
    assert "var_dump" in out and "Foo.class.php:3" in out
    assert "Forbidden paths" in out and "Generated/Bar.class.php" in out
    # Foo is a new src class with no test -> flagged; Generated file is exempt.
    assert "tests/unit/SiFoo/FooTest.php" in out


def test_clean_diff_is_silent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src2/SiFoo/Foo.class.php", "<?php\nclass Foo {}\n")
    _write(tmp_path, "tests/unit/SiFoo/FooTest.php", "<?php\nclass FooTest {}\n")
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged", env_extra={"SUPERTOOL_TEST_PAIRING": DVSI_PAIRING})

    assert "No red flags" in out
    assert "⚠" not in out


def test_branch_mode_scope(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], check=True, cwd=tmp_path)
    _write(tmp_path, "src2/SiFoo/Foo.class.php", "<?php\nclass Foo {}\n")
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "feat"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "branch", "main")

    assert "git-diff (branch)" in out
    assert "merge-base(main)..HEAD" in out
    assert "Foo.class.php" in out


def _run_raw(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Like _run but without the returncode==0 assertion (guard paths return 1)."""
    return subprocess.run(
        [sys.executable, str(DIFF), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=repo, env=dict(os.environ),
    )


def test_path_mode_missing_path_warns_not_silent(tmp_path: Path) -> None:
    """A path that doesn't exist must NOT read as 'No changes.' — it warns + exits 1.

    Regression for the wrong-CWD trap: git-diff:PATH on an absent file silently
    printed 'No changes.', indistinguishable from a clean tracked file.
    """
    _init_repo(tmp_path)
    res = _run_raw(tmp_path, "does-not-exist.json")
    assert res.returncode == 1
    assert "No changes." not in res.stdout
    assert "not found" in res.stdout
    assert "wrong CWD" in res.stdout
    assert "Repo:" in res.stdout


def test_path_mode_untracked_path_warns(tmp_path: Path) -> None:
    """An on-disk but untracked file warns 'untracked' rather than 'No changes.'"""
    _init_repo(tmp_path)
    _write(tmp_path, "scratch.txt", "hi\\n")
    res = _run_raw(tmp_path, "scratch.txt")
    assert res.returncode == 0
    assert "No changes." not in res.stdout
    assert "untracked" in res.stdout


def test_path_mode_tracked_clean_still_says_no_changes(tmp_path: Path) -> None:
    """Regression guard: a tracked, unmodified file is genuinely clean → 'No changes.'"""
    _init_repo(tmp_path)
    res = _run(tmp_path, "seed.txt")
    assert "No changes." in res


def test_path_mode_tracked_modified_still_diffs(tmp_path: Path) -> None:
    """The guard must NOT swallow a legit diff: a modified tracked file scoped by
    PATH still renders its diff (proves ls-files lets tracked paths through)."""
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\\nmore\\n")
    res = _run(tmp_path, "seed.txt")
    assert "No changes." not in res
    assert "seed.txt" in res


def test_header_shows_repo_root(tmp_path: Path) -> None:
    """Every mode stamps the resolved repo root so a wrong-CWD run is visible."""
    _init_repo(tmp_path)
    out = _run(tmp_path, "staged")
    assert "Repo:" in out


def test_migration_path_not_flagged_but_module_is(tmp_path: Path) -> None:
    """Bug 2 regression: 'migration' substring must not exempt real source classes."""
    _init_repo(tmp_path)
    # A real migration under a migrations/ path -> classified migration, exempt.
    _write(tmp_path, "src2/Migrations/V1/V1.class.php", "<?php\nclass V1 {}\n")
    # A module whose NAME contains 'migration' -> still a source class, must be flagged.
    _write(tmp_path, "src2/SiMigration/SiMigrationModule.class.php",
           "<?php\nclass SiMigrationModule {}\n")
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged", env_extra={"SUPERTOOL_TEST_PAIRING": DVSI_PAIRING})

    assert "SiMigrationModuleTest.php" in out  # module flagged
    assert "V1Test.php" not in out             # real migration exempt


def test_added_line_starting_with_plusplus_is_scanned(tmp_path: Path) -> None:
    """Bug 1 regression: an added line whose content starts with '++' renders as
    '+++...' in the diff and must NOT be mistaken for a '+++ ' header."""
    _init_repo(tmp_path)
    # Content line begins at column 0 with '++', so the diff line is '+++x...'.
    _write(tmp_path, "src2/SiFoo/Foo.class.php", "<?php\n++var_dump($x);\n")
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged")

    assert "var_dump" in out


def test_ext_filter_on_red_flags_extra(tmp_path: Path) -> None:
    """A red_flags_extra entry with an `ext` filter fires only on that extension."""
    _init_repo(tmp_path)
    _write(tmp_path, "app.js", "const x = 1;\nbreakpoint();\n")
    _write(tmp_path, "app.py", "x = 1\nbreakpoint()\n")
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    flags = json.dumps([{"pattern": r"\bbreakpoint\s*\(", "ext": ".py", "label": "bp"}])
    out = _run(tmp_path, "staged", env_extra={"SUPERTOOL_RED_FLAGS_EXTRA": flags})

    # Red flags print "path:line"; the file list prints "status  path" (no colon).
    assert "app.py:2" in out   # .py matches the ext filter -> flagged
    assert "app.js:" not in out  # .js excluded by the ext filter


def test_conflict_marker_only_at_line_start(tmp_path: Path) -> None:
    """A real conflict marker (column 0) flags; a mid-line mention does not."""
    _init_repo(tmp_path)
    _write(tmp_path, "note.txt", "ok\n# discusses <<<<<<< in prose\n")  # mention
    _write(tmp_path, "conflict.txt", "ok\n<<<<<<< HEAD\n")              # real marker
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged")

    assert "conflict.txt:2" in out  # real marker at BOL -> flagged
    assert "note.txt:" not in out   # mid-line mention -> not flagged


def test_plain_mode_replaces_glyphs_with_ascii(tmp_path: Path) -> None:
    """SUPERTOOL_PLAIN=1 → warning sections use [WARN], no ⚠ glyph (issue #308)."""
    _init_repo(tmp_path)
    _write(tmp_path, "src2/SiFoo/Foo.class.php",
           "<?php\nclass Foo {\n    function go() { var_dump($this); }\n}\n")
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged", env_extra={"SUPERTOOL_PLAIN": "1"})

    assert "[WARN]" in out
    assert "⚠" not in out
    # Stable ASCII section key survives — machine consumers grep this, not glyphs.
    assert "Red flags in added lines" in out
    # Entire output is ASCII-encodable (the whole point of plain mode).
    out.encode("ascii")


def test_plain_mode_clean_diff_uses_ok_marker(tmp_path: Path) -> None:
    """Clean diff in plain mode → [OK], no ✓ glyph."""
    _init_repo(tmp_path)
    _write(tmp_path, "src2/SiFoo/Foo.class.php", "<?php\nclass Foo {}\n")
    _write(tmp_path, "tests/unit/SiFoo/FooTest.php", "<?php\nclass FooTest {}\n")
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged",
               env_extra={"SUPERTOOL_PLAIN": "1", "SUPERTOOL_TEST_PAIRING": DVSI_PAIRING})

    assert "[OK]" in out
    assert "✓" not in out
    assert "No red flags" in out


def test_default_mode_keeps_glyphs(tmp_path: Path) -> None:
    """Without SUPERTOOL_PLAIN, rich glyphs are unchanged (no regression)."""
    _init_repo(tmp_path)
    _write(tmp_path, "src2/SiFoo/Foo.class.php",
           "<?php\nclass Foo {\n    function go() { var_dump($this); }\n}\n")
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged", env_extra={"SUPERTOOL_PLAIN": "0"})

    assert "⚠" in out
    assert "[WARN]" not in out


def test_not_a_git_repo(tmp_path: Path) -> None:
    res = subprocess.run(
        [sys.executable, str(DIFF), "staged"],
        capture_output=True, text=True, encoding="utf-8", cwd=tmp_path,
    )
    assert res.returncode == 1
    assert "not inside a git repository" in res.stdout
