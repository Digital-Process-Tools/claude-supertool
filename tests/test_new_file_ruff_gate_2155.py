"""#2155 -- the local route that catches an unused import in a brand-new file
before it costs a push-and-wait round trip against the CI lint leg.

`tests/test_lint_new_files_1481.py` proved the CI leg's own scoping (the diff,
not the tree). This file proves the local gate that reuses its `EXTRA_RULES`
constant and answers the same question against `HEAD` instead of a PR base --
run against a synthetic git repo, never against this repository's own tree,
so a bug here cannot make a claim about this repo's own working tree.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from _symlink import require_symlink

REPO = Path(__file__).resolve().parents[1]
GATE_PATH = REPO / ".github" / "scripts" / "new_file_ruff_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("new_file_ruff_gate_2155",
                                                    GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()

DEAD_IMPORT = "import json" + chr(10) + chr(10) + "X = 1" + chr(10)
CLEAN = "X = 1" + chr(10)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, args + (r.stdout, r.stderr)
    return r.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A base commit with the same tree-wide ruff ignore this repo carries,
    plus one already-committed file with a dead import -- proof that the
    ignore, not this gate, is what keeps that one quiet."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "pyproject.toml").write_text(
        "[tool.ruff.lint]" + chr(10)
        + 'select = ["E9", "F", "B", "PLE"]' + chr(10)
        + 'ignore = ["F401", "F841", "F541"]' + chr(10),
        encoding="utf-8")
    (root / "old.py").write_text(DEAD_IMPORT, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _run_gate(file: Path):
    """`(exit_code_ignored, verdict_dict)` — this adapter is a subprocess
    contract (one JSON line on stdout), so drive it as one: run `main()` with
    argv patched, capture stdout, parse the JSON. Mirrors how the real
    validator harness reads every adapter (`_validator_run_one` in
    `_supertool.py`)."""
    import io
    old_argv, old_stdout = sys.argv, sys.stdout
    sys.argv = ["new_file_ruff_gate.py", str(file)]
    sys.stdout = io.StringIO()
    try:
        gate.main()
        out = sys.stdout.getvalue()
    finally:
        sys.argv, sys.stdout = old_argv, old_stdout
    line = out.strip().splitlines()[-1]
    return json.loads(line)


def _require_ruff():
    import shutil
    if not shutil.which("ruff"):
        pytest.skip("ruff not on PATH -- this test needs the real binary, "
                    "same as tests/test_lint_new_files_1481.py")


# --- the case #2155 was filed for -------------------------------------------


def test_a_brand_new_file_with_no_head_blob_is_checked_with_extra_rules(
        repo: Path) -> None:
    """This is the exact shape of #2145/#2148/#2153: a new test file, never
    committed, carrying a dead import the tree-wide ignore would otherwise
    hide from every local route."""
    _require_ruff()
    new_file = repo / "brand_new.py"
    new_file.write_text(DEAD_IMPORT, encoding="utf-8")
    # Deliberately NOT committed -- the failure mode is "before the push",
    # and this gate must fire on the working tree, not only after a commit.
    verdict = _run_gate(new_file)
    assert verdict["ok"] is False, verdict
    assert verdict["count"] >= 1, verdict
    assert any(e["code"] == "F401" for e in verdict["errors"]), verdict


def test_a_clean_new_file_is_reported_ok_not_skipped(repo: Path) -> None:
    _require_ruff()
    new_file = repo / "brand_new_clean.py"
    new_file.write_text(CLEAN, encoding="utf-8")
    verdict = _run_gate(new_file)
    assert verdict.get("ok") is True, verdict
    assert "skipped" not in verdict, verdict


# --- what this gate deliberately leaves alone -------------------------------


def test_a_file_already_committed_at_head_is_skipped_not_checked(
        repo: Path) -> None:
    """`old.py` has a commit at HEAD and a dead import the tree-wide ignore
    exists to hold back. This gate is not CI's merge-base diff and has no PR
    base ref to reproduce it with, so it declines rather than re-litigating
    debt it cannot correctly scope."""
    _require_ruff()
    verdict = _run_gate(repo / "old.py")
    assert "skipped" in verdict, verdict
    assert "ok" not in verdict and "count" not in verdict, verdict


def test_a_modified_but_previously_committed_file_is_still_skipped(
        repo: Path) -> None:
    """Editing `old.py` further (still carrying its dead import, plus a new
    one) does not make it new -- it has a commit at HEAD before this edit,
    so it is still out of scope for THIS gate specifically."""
    _require_ruff()
    (repo / "old.py").write_text(DEAD_IMPORT + "import sys" + chr(10),
                                 encoding="utf-8")
    verdict = _run_gate(repo / "old.py")
    assert "skipped" in verdict, verdict


def test_outside_a_git_repository_declines_rather_than_guessing(
        tmp_path: Path) -> None:
    """No git repo at all -- e.g. a file opened outside any checkout. Must
    not silently report `ok`: whether the tree-wide ignore even applies is
    unanswerable, and the third state exists for exactly this."""
    _require_ruff()
    lone = tmp_path / "lone.py"
    lone.write_text(CLEAN, encoding="utf-8")
    verdict = _run_gate(lone)
    assert "skipped" in verdict, verdict
    assert "ok" not in verdict, verdict


def test_ruff_absent_is_the_third_state_not_a_pass(
        repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(gate.shutil, "which", lambda *_a, **_k: None)
    new_file = repo / "brand_new2.py"
    new_file.write_text(DEAD_IMPORT, encoding="utf-8")
    verdict = _run_gate(new_file)
    # `absent()` escalates to a loud `adapter` error unless
    # SUPERTOOL_REQUIRE_VALIDATORS names this tool -- either way it must not
    # be `ok: true` about a file ruff never opened.
    assert verdict.get("ok") is not True, verdict


def test_a_committed_file_reached_through_a_symlinked_ancestor_is_not_new(
        repo: Path, tmp_path: Path) -> None:
    """Reviewer-found regression: `git rev-parse --show-toplevel` reports the
    PHYSICAL repo root (every symlink resolved); `os.path.abspath` does not
    resolve symlinks. Reached through a symlinked ancestor (macOS's own
    `/tmp` -> `/private/tmp` is exactly this shape), the two disagreed and
    `os.path.relpath` computed a path `git cat-file -e HEAD:<path>` could
    never find -- silently reporting an already-committed file as new and
    re-enabling F401/F841/F541 on code this gate has no business
    relitigating. `main()` now resolves through `os.path.realpath` before
    calling `_is_new_at_head`, which is what this test pins."""
    require_symlink()
    link = tmp_path / "via_symlink"
    link.symlink_to(repo, target_is_directory=True)
    verdict = _run_gate(link / "old.py")
    assert "skipped" in verdict, (
        "a file already committed at HEAD, reached through a symlink, was "
        f"reported as new instead of being left to the shared `ruff` "
        f"validator: {verdict!r}")


def test_extra_rules_is_imported_not_copied_from_lint_new_files(
        ) -> None:
    """The one thing this whole gate exists to keep in sync with the CI leg
    it is a local stand-in for. A hand-duplicated tuple drifts silently and
    an import cannot -- checked two ways: the source names an import (never
    a re-typed literal), and the two modules' independently-executed copies
    still agree on the value."""
    source = GATE_PATH.read_text(encoding="utf-8")
    assert "from lint_new_files import EXTRA_RULES" in source, source
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "lint_new_files_2155", REPO / ".github" / "scripts" / "lint_new_files.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert gate.EXTRA_RULES == mod.EXTRA_RULES
