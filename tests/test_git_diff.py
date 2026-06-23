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
        capture_output=True, text=True, cwd=repo, env=env,
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


def test_not_a_git_repo(tmp_path: Path) -> None:
    res = subprocess.run(
        [sys.executable, str(DIFF), "staged"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert res.returncode == 1
    assert "not inside a git repository" in res.stdout
