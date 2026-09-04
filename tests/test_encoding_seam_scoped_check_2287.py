"""#2287 -- catch the #418 encoding-seam guard on a lane's OWN changed test
files before push, not only inside the full CI suite.

`tests/test_encoding_seam.py` enforces the encoding-seam rule tree-wide, but
only inside the full pytest suite, so a violation is caught only after a
lane has already pushed and burned a full CI leg finding out. This file
proves two artifacts that scope the SAME check to just the files a lane
changed, reusing (never re-implementing) the real scan functions:

* `.github/scripts/check_encoding_seam.py` -- a standalone, git-diff-scoped
  runner, exercised end to end against a synthetic repo that carries a real
  copy of `tests/test_encoding_seam.py` (so the scan itself is this
  project's own, not a test double).
* `validators/encoding-seam/encoding-seam.py` -- the supertool validator
  adapter wired into `.supertool.json`, so the same check also fires on
  every `paste`/`edit` of a matching file, no push required at all.
* `validators/common/encoding_seam.py`'s `scope_kinds` -- the scoping rule
  both of the above share (`tests/` read-only, `SHIPPED` read+write, the
  rest out of the read/write half but still subject to the subprocess half).

Run against synthetic git repos and tmp_path fixtures, never against this
repository's own tree, so a bug here cannot make a false claim about this
repo's own working tree.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import _adapter_budget as budget  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "check_encoding_seam.py"
ADAPTER = REPO / "validators" / "encoding-seam" / "encoding-seam.py"
SHARED = REPO / "validators" / "common" / "encoding_seam.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


shared = _load("_st_encoding_seam_shared_2287", SHARED)

BAD_CALL = """import subprocess


def f(cmd):
    return subprocess.run(cmd, text=True)
"""

GOOD_CALL = """import subprocess


def f(cmd):
    return subprocess.run(cmd, text=True, encoding="utf-8", errors="replace")
"""


# ---------------------------------------------------------------------------
# scope_kinds -- no subprocess, no git, just the scoping rule itself.
# ---------------------------------------------------------------------------

def test_scope_kinds_is_read_only_under_tests() -> None:
    assert shared.scope_kinds("tests/test_something.py", ("supertool.py",)) == ("read",)


def test_scope_kinds_is_read_and_write_under_shipped() -> None:
    assert shared.scope_kinds("presets/git/push.py", ("presets",)) == ("read", "write")
    assert shared.scope_kinds("supertool.py", ("supertool.py",)) == ("read", "write")


def test_scope_kinds_is_none_outside_both_scopes() -> None:
    assert shared.scope_kinds("docs/contributing.md", ("supertool.py", "presets")) is None
    assert shared.scope_kinds("scripts/whatever.py", ("supertool.py", "presets")) is None


# ---------------------------------------------------------------------------
# The validator adapter -- one file in, one SCHEMA.md JSON record out.
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                        timeout=30, encoding="utf-8", errors="replace")
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout


def _run_adapter(file_path: Path) -> dict:
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(file_path)],
        capture_output=True, timeout=budget.adapter_budget(ADAPTER),
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, (r.stdout, r.stderr)
    return json.loads(r.stdout)


def test_adapter_flags_an_unguarded_subprocess_run(tmp_path: Path) -> None:
    fake_root = tmp_path / "repo"
    (fake_root / "tests").mkdir(parents=True)
    _git(fake_root, "init", "-q")
    shutil.copy(REPO / "tests" / "test_encoding_seam.py",
                fake_root / "tests" / "test_encoding_seam.py")
    target = fake_root / "tests" / "test_bad_2287.py"
    target.write_text(BAD_CALL, encoding="utf-8")

    result = _run_adapter(target)
    assert result["tool"] == "encoding-seam"
    assert result["ok"] is False
    assert result["count"] == 1
    assert "encoding" in result["errors"][0]["msg"]


def test_adapter_spares_a_correctly_encoded_call(tmp_path: Path) -> None:
    fake_root = tmp_path / "repo"
    (fake_root / "tests").mkdir(parents=True)
    _git(fake_root, "init", "-q")
    shutil.copy(REPO / "tests" / "test_encoding_seam.py",
                fake_root / "tests" / "test_encoding_seam.py")
    target = fake_root / "tests" / "test_good_2287.py"
    target.write_text(GOOD_CALL, encoding="utf-8")

    result = _run_adapter(target)
    assert result["ok"] is True
    assert result["count"] == 0
    assert result["errors"] == []


def test_adapter_declines_when_config_dir_sits_above_a_directory_of_clones(
    tmp_path: Path,
) -> None:
    """#2228/#2236's own shape, reproduced for THIS adapter: a maintainer's
    `.supertool.json` sitting above a directory of clones must not
    authorize importing an arbitrary clone's own test_encoding_seam.py --
    this adapter imports and EXECUTES what it finds, same as
    new-file-lint.py and changelog-fragment.py before it.
    """
    clones_dir = tmp_path / "clones"
    fake_repo = clones_dir / "some-clone"
    (fake_repo / "tests").mkdir(parents=True)
    _git(fake_repo, "init", "-q")
    shutil.copy(REPO / "tests" / "test_encoding_seam.py",
                fake_repo / "tests" / "test_encoding_seam.py")
    target = fake_repo / "tests" / "test_bad_2287.py"
    target.write_text(BAD_CALL, encoding="utf-8")

    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(target)],
        capture_output=True, timeout=budget.adapter_budget(ADAPTER),
        encoding="utf-8", errors="replace",
        env={**os.environ, "SUPERTOOL_CONFIG_DIR": str(clones_dir)},
    )
    assert r.returncode == 0, (r.stdout, r.stderr)
    result = json.loads(r.stdout)
    assert "skipped" in result
    assert "ok" not in result


def test_adapter_declines_when_the_project_has_no_test_module(tmp_path: Path) -> None:
    fake_root = tmp_path / "no_guard_repo"
    fake_root.mkdir()
    _git(fake_root, "init", "-q")
    target = fake_root / "whatever.py"
    target.write_text(BAD_CALL, encoding="utf-8")

    result = _run_adapter(target)
    assert "skipped" in result
    assert "ok" not in result
    assert "count" not in result


# ---------------------------------------------------------------------------
# The standalone, git-diff-scoped script -- end to end, against a synthetic
# repo carrying a REAL copy of tests/test_encoding_seam.py, so the scan
# itself is this project's own logic rather than a test double.
# ---------------------------------------------------------------------------

def _run_script(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=str(repo),
        capture_output=True, timeout=30, encoding="utf-8", errors="replace",
    )


@pytest.fixture()
def synthetic_repo(tmp_path: Path) -> "tuple[Path, str]":
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "encoding-seam@example.com")
    _git(root, "config", "user.name", "encoding-seam test")
    shutil.copy(REPO / "tests" / "test_encoding_seam.py",
                root / "tests" / "test_encoding_seam.py")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD").strip()
    return root, base_sha


def test_script_catches_a_new_unguarded_subprocess_run(
    synthetic_repo: "tuple[Path, str]",
) -> None:
    root, base_sha = synthetic_repo
    (root / "tests" / "test_new_thing_2287.py").write_text(BAD_CALL, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add unguarded call")

    r = _run_script(root, "--base", base_sha)

    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "test_new_thing_2287.py" in r.stdout
    assert "encoding" in r.stdout


def test_script_spares_a_correctly_encoded_call(
    synthetic_repo: "tuple[Path, str]",
) -> None:
    root, base_sha = synthetic_repo
    (root / "tests" / "test_new_thing_ok_2287.py").write_text(GOOD_CALL, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add correctly encoded call")

    r = _run_script(root, "--base", base_sha)

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "clean" in r.stdout


def test_script_narrows_to_explicit_files_with_no_git_diff_at_all(
    synthetic_repo: "tuple[Path, str]",
) -> None:
    root, _base_sha = synthetic_repo
    target = root / "tests" / "test_explicit_2287.py"
    target.write_text(BAD_CALL, encoding="utf-8")

    r = _run_script(root, "tests/test_explicit_2287.py")

    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "test_explicit_2287.py" in r.stdout


def test_script_distinguishes_could_not_check_from_clean(
    tmp_path: Path,
) -> None:
    """#2287 review, auditor finding: a script that never runs (no
    `tests/test_encoding_seam.py` here) must not return the same exit code
    as a run that scanned files and found nothing -- a caller gating on
    exit status alone cannot otherwise tell "clean" from "did not check".
    """
    no_guard_root = tmp_path / "no_guard_repo"
    no_guard_root.mkdir()
    _git(no_guard_root, "init", "-q")
    _git(no_guard_root, "config", "user.email", "encoding-seam@example.com")
    _git(no_guard_root, "config", "user.name", "encoding-seam test")
    (no_guard_root / "x.py").write_text("X = 1\n", encoding="utf-8")
    _git(no_guard_root, "add", "-A")
    _git(no_guard_root, "commit", "-q", "-m", "base")

    r = _run_script(no_guard_root, "x.py")

    assert r.returncode == 2, (r.stdout, r.stderr)


def test_script_catches_a_non_ascii_filename_git_quotes_by_default(
    synthetic_repo: "tuple[Path, str]",
) -> None:
    """#2287 review finding: `git diff --name-only` (no `-z`) C-quotes and
    octal-escapes a path holding a non-ASCII byte under the default
    `core.quotePath=true`, so `.splitlines()` over that hands the quoted
    literal string to `(root / f).is_file()`, which is never true -- the
    file is silently dropped from the scan. The fix reads `-z` output
    instead, which is never quoted.
    """
    root, base_sha = synthetic_repo
    non_ascii_name = "test_" + chr(0xE9) + "_2287.py"  # e-acute
    (root / "tests" / non_ascii_name).write_text(BAD_CALL, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add non-ascii filename")

    r = _run_script(root, "--base", base_sha)

    assert r.returncode == 1, (r.stdout, r.stderr)
    assert non_ascii_name in r.stdout


def test_script_resolves_explicit_files_against_cwd_not_repo_root(
    synthetic_repo: "tuple[Path, str]",
) -> None:
    """#2287 review finding: an explicit file argument is typed relative to
    wherever the operator is standing, not necessarily to the repo root --
    `cd tests && ../.github/scripts/check_encoding_seam.py test_foo.py`
    must resolve the same file the equivalent root-relative call does.
    """
    root, _base_sha = synthetic_repo
    target = root / "tests" / "test_explicit_cwd_2287.py"
    target.write_text(BAD_CALL, encoding="utf-8")

    r = subprocess.run(
        [sys.executable, str(SCRIPT), "test_explicit_cwd_2287.py"],
        cwd=str(root / "tests"),
        capture_output=True, timeout=30, encoding="utf-8", errors="replace",
    )

    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "test_explicit_cwd_2287.py" in r.stdout


def test_scan_one_skips_subprocess_half_outside_both_scopes(tmp_path: Path) -> None:
    """#2287 review finding: `tests/test_encoding_seam.py`'s own tree-wide
    guard never enumerates subprocess calls outside `tests/`/`SHIPPED`
    either, so `scan_one(..., kinds=None)` scanning them anyway was a false
    positive relative to the guard this local check claims to mirror, not
    just a broader net. A file with a genuine unguarded `subprocess.run`,
    classified `kinds=None` (out of both scopes), must come back with no
    findings at all -- not just no read/write findings.
    """
    real_module = _load("_st_encoding_seam_real_2287",
                         REPO / "tests" / "test_encoding_seam.py")
    out_of_scope = tmp_path / "outside.py"
    out_of_scope.write_text(BAD_CALL, encoding="utf-8")

    assert shared.scan_one(real_module, out_of_scope, None) == []
    # Sanity: the SAME file, in scope, DOES report the violation --
    # otherwise an empty result here would prove nothing about scoping.
    assert shared.scan_one(real_module, out_of_scope, ("read",)) != []
