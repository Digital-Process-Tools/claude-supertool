"""#2228 -- new-file-lint must not trust a convention-based script found
inside a repo whose .supertool.json lives ABOVE it, in a directory holding
other clones.

`_find_lint_script` bounds its walk at the git root of the file being
edited -- correct against escaping *above* that repo (mirrors
changelog-fragment.py's #2178 fix), but it never asked a different
question: is the repo whose script it is about to import the SAME project
that wired this validator's `.supertool.json` in the first place? A
maintainer whose own `.supertool.json` sits above a directory of clones,
editing any `.py` file inside one of them, previously had that clone's own
conventionally-named `.github/scripts/lint_new_files.py` imported -- and
`_load` executes it -- with the maintainer's own privileges, before
`_is_new_at_head` even asks whether the edited file is new.

Supertool's own validator runner now sets `SUPERTOOL_CONFIG_DIR` (the
directory holding the `.supertool.json` that wired this run) in the
adapter's environment. When that directory is a strict ancestor of the
edited file's own git root -- the directory-of-clones shape -- the
convention-based locations are not trusted; `SUPERTOOL_NEW_FILE_LINT_SCRIPT`
(an operator naming one exact path) still is, because that is explicit
trust rather than an inherited one.

When the env var is absent altogether, this adapter was invoked directly
(a test harness, an operator running it by hand) rather than through
supertool's wiring -- no scope claim is being made either way, so the old,
already-tested repo-bound behaviour is unchanged; every other test in
`test_new_file_lint_validator_2155.py` exercises exactly that path and
must keep passing unmodified.

Would these pass if the code did nothing? No:
`test_a_directory_of_clones_scope_is_not_trusted` observes the marker file
a malicious `lint_new_files.py` writes as its own side effect of having
been imported, and fails before the fix; after the fix the walk refuses
before ever finding it. `test_a_self_configured_repo_is_still_trusted`
pins the accepting case -- the ordinary, safe shape -- so a fix that starts
refusing everything cannot pass by refusing indiscriminately.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ADAPTER = REPO / "validators" / "new-file-lint" / "new-file-lint.py"

DEAD_IMPORT = "import json" + chr(10) + chr(10) + "X = 1" + chr(10)

REAL_LINT_SCRIPT_BODY = 'EXTRA_RULES = ("F401", "F841", "F541")' + chr(10)

#: Proves import/execution by an unmistakable side effect, rather than
#: asserting on whatever exception shape a refused import happens to raise
#: today -- that is an implementation detail of how the escape fails, not
#: of whether it happened. Also declares EXTRA_RULES so that, IF this ever
#: executed, the run would proceed exactly like a legitimate one instead of
#: being caught by an unrelated "no EXTRA_RULES" skip.
MALICIOUS_LINT_SCRIPT = (
    "import pathlib" + chr(10)
    + "pathlib.Path(__file__).with_name('PWNED').write_text('pwned')"
    + chr(10)
    + 'EXTRA_RULES = ("F401",)' + chr(10)
)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, args + (r.stdout, r.stderr)
    return r.stdout


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "pyproject.toml").write_text(
        "[tool.ruff.lint]" + chr(10)
        + 'select = ["E9", "F", "B", "PLE"]' + chr(10)
        + 'ignore = ["F401", "F841", "F541"]' + chr(10),
        encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")


def _run(target: Path, env: dict) -> dict:
    full_env = dict(os.environ)
    full_env.pop("SUPERTOOL_NEW_FILE_LINT_SCRIPT", None)
    full_env.pop("SUPERTOOL_CONFIG_DIR", None)
    full_env.update(env)
    proc = subprocess.run([sys.executable, str(ADAPTER), str(target)],
                          capture_output=True, text=True, env=full_env,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _require_ruff():
    import shutil
    if not shutil.which("ruff"):
        pytest.skip("ruff not on PATH -- this test needs the real binary")


def test_a_directory_of_clones_scope_is_not_trusted(tmp_path):
    """The escape (#2228): SUPERTOOL_CONFIG_DIR names a directory ABOVE the
    clone being edited -- the maintainer's own .supertool.json sitting over
    a directory of clones -- and the clone's own conventionally-named
    script must not be imported or executed."""
    clones = tmp_path / "clones"
    clone = clones / "untrusted-clone"
    _init_repo(clone)
    scripts = clone / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "lint_new_files.py").write_text(MALICIOUS_LINT_SCRIPT,
                                                encoding="utf-8")
    new_file = clone / "brand_new.py"
    new_file.write_text(DEAD_IMPORT, encoding="utf-8")

    result = _run(new_file, {"SUPERTOOL_CONFIG_DIR": str(clones)})

    assert not (scripts / "PWNED").exists(), (
        "the untrusted clone's own script ran -- the convention-based "
        "location was trusted across a .supertool.json scope boundary: "
        + json.dumps(result))
    assert "skipped" in result, result
    assert "ok" not in result, result


def test_a_self_configured_repo_is_still_trusted(tmp_path):
    """The working case (control): a project whose OWN .supertool.json
    wires this validator against itself must still resolve its own
    conventionally-placed script, exactly as before #2228."""
    _require_ruff()
    repo = tmp_path / "repo"
    _init_repo(repo)
    scripts = repo / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "lint_new_files.py").write_text(REAL_LINT_SCRIPT_BODY,
                                                encoding="utf-8")
    new_file = repo / "brand_new.py"
    new_file.write_text(DEAD_IMPORT, encoding="utf-8")

    result = _run(new_file, {"SUPERTOOL_CONFIG_DIR": str(repo)})

    assert result["ok"] is False, result
    assert any(e["code"] == "F401" for e in result["errors"]), result


def test_config_dir_env_absent_preserves_existing_direct_invocation_behavior(
        tmp_path):
    """No SUPERTOOL_CONFIG_DIR at all means this adapter was invoked
    directly, outside supertool's own validator wiring -- e.g. a test
    harness, or an operator running the script by hand. No scope claim is
    being made either way, so the pre-#2228 repo-bound walk still applies;
    this is the same shape every test in test_new_file_lint_validator_2155.py
    already relies on, pinned here under this issue's own name."""
    _require_ruff()
    repo = tmp_path / "repo"
    _init_repo(repo)
    scripts = repo / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "lint_new_files.py").write_text(REAL_LINT_SCRIPT_BODY,
                                                encoding="utf-8")
    new_file = repo / "brand_new.py"
    new_file.write_text(DEAD_IMPORT, encoding="utf-8")

    result = _run(new_file, {})

    assert result["ok"] is False, result
    assert any(e["code"] == "F401" for e in result["errors"]), result


def test_a_disjoint_unrelated_project_is_still_trusted(tmp_path):
    """Self-review finding (#2228): the fix's first cut refused ANY
    SUPERTOOL_CONFIG_DIR that was not `root` itself or an ancestor of it --
    which also refused a config directory sharing no ancestry with `root`
    AT ALL, an entirely ordinary shape when supertool is invoked with an
    explicit `path=` argument naming a file outside the config-owning
    project (see `tests/test_changelog_fragment_write_receipt_1132.py`'s
    own end-to-end CLI test for the sibling adapter's identical case).
    Only a config directory sitting STRICTLY ABOVE `root` -- the
    directory-of-clones shape -- may be refused; a disjoint sibling tree
    must still be trusted."""
    _require_ruff()
    unrelated_config_owner = tmp_path / "some-other-project-entirely"
    unrelated_config_owner.mkdir()

    repo = tmp_path / "sibling-repo"
    _init_repo(repo)
    scripts = repo / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "lint_new_files.py").write_text(REAL_LINT_SCRIPT_BODY,
                                                encoding="utf-8")
    new_file = repo / "brand_new.py"
    new_file.write_text(DEAD_IMPORT, encoding="utf-8")

    result = _run(new_file, {"SUPERTOOL_CONFIG_DIR": str(unrelated_config_owner)})

    assert result["ok"] is False, result
    assert any(e["code"] == "F401" for e in result["errors"]), result


def test_explicit_override_bypasses_the_scope_check(tmp_path):
    """SUPERTOOL_NEW_FILE_LINT_SCRIPT names one exact path -- an operator's
    own explicit trust, not an inherited one -- and must still be honoured
    even when SUPERTOOL_CONFIG_DIR names a directory above the repo."""
    _require_ruff()
    clones = tmp_path / "clones"
    clone = clones / "some-clone"
    _init_repo(clone)
    pinned = clone / "tools" / "my_lint_policy.py"
    pinned.parent.mkdir(parents=True)
    pinned.write_text('EXTRA_RULES = ("F401",)' + chr(10), encoding="utf-8")
    new_file = clone / "brand_new.py"
    new_file.write_text(DEAD_IMPORT, encoding="utf-8")

    result = _run(new_file, {
        "SUPERTOOL_CONFIG_DIR": str(clones),
        "SUPERTOOL_NEW_FILE_LINT_SCRIPT": str(Path("tools") / "my_lint_policy.py"),
    })

    assert result["ok"] is False, result
    assert any(e["code"] == "F401" for e in result["errors"]), result
