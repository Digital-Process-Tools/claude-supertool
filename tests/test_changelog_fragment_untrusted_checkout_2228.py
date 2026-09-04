"""#2228 -- changelog-fragment must not trust a convention-based assembler
found inside a repo whose .supertool.json lives ABOVE it, in a directory
holding other clones.

`_find_assembler`'s walk was already bounded at the fragment's own git
root (#2178), which stops an escape ABOVE that repo -- but it never asked a
different question: is the repo whose assembler it is about to import the
SAME project that wired this validator's `.supertool.json` in the first
place? See `tests/test_new_file_lint_untrusted_checkout_2228.py` for the
full shape; this is the identical primitive one file over, closed the same
way -- `SUPERTOOL_CONFIG_DIR`, set by supertool's own validator runner to
the directory holding the `.supertool.json` that wired this run.

Would these pass if the code did nothing? No:
`test_a_directory_of_clones_scope_is_not_trusted` observes the marker file
a malicious `assemble_changelog.py` writes as its own side effect of
having been imported, and fails before the fix; after the fix the walk
refuses before ever finding it. `test_a_self_configured_repo_is_still_trusted`
pins the accepting case so a fix that starts refusing everything cannot
pass by refusing indiscriminately.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADAPTER = REPO / "validators" / "changelog-fragment" / "changelog-fragment.py"
REAL_ASSEMBLER = REPO / ".github" / "scripts" / "assemble_changelog.py"

FRAGMENT_BODY = (
    "- **A thing** ([#1](https://github.com/"
    "Digital-Process-Tools/claude-supertool/issues/1)). Prose.\n"
)

#: Proves import/execution by an unmistakable side effect, rather than
#: asserting on whatever exception shape a refused import happens to raise
#: today.
MALICIOUS_ASSEMBLER = (
    "import pathlib\n"
    "pathlib.Path(__file__).with_name('PWNED').write_text('pwned')\n"
)


def _write_fragment(repo: Path) -> Path:
    frag_dir = repo / "changelog.d"
    frag_dir.mkdir(parents=True, exist_ok=True)
    target = frag_dir / "1.fixed.md"
    target.write_text(FRAGMENT_BODY, encoding="utf-8")
    return target


def _run(target: Path, env: dict) -> dict:
    full_env = dict(os.environ)
    full_env.pop("SUPERTOOL_CHANGELOG_ASSEMBLER", None)
    full_env.pop("SUPERTOOL_CONFIG_DIR", None)
    full_env.update(env)
    proc = subprocess.run([sys.executable, str(ADAPTER), str(target)],
                          capture_output=True, text=True, env=full_env,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_directory_of_clones_scope_is_not_trusted(tmp_path):
    """The escape (#2228): SUPERTOOL_CONFIG_DIR names a directory ABOVE the
    clone being edited -- the maintainer's own .supertool.json sitting over
    a directory of clones -- and the clone's own conventionally-named
    assembler must not be imported or executed."""
    clones = tmp_path / "clones"
    clone = clones / "untrusted-clone"
    clone.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    target = _write_fragment(clone)

    scripts = clone / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "assemble_changelog.py").write_text(MALICIOUS_ASSEMBLER,
                                                    encoding="utf-8")

    result = _run(target, {"SUPERTOOL_CONFIG_DIR": str(clones)})

    assert not (scripts / "PWNED").exists(), (
        "the untrusted clone's own assembler ran -- the convention-based "
        "location was trusted across a .supertool.json scope boundary: "
        + json.dumps(result))
    assert "skipped" in result, result
    assert "ok" not in result, result


def test_a_self_configured_repo_is_still_trusted(tmp_path):
    """The working case (control): a project whose OWN .supertool.json
    wires this validator against itself must still resolve its own
    conventionally-placed assembler, exactly as before #2228."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = _write_fragment(repo)

    scripts = repo / ".github" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REAL_ASSEMBLER, scripts / "assemble_changelog.py")

    result = _run(target, {"SUPERTOOL_CONFIG_DIR": str(repo)})

    assert "skipped" not in result, result
    assert result["ok"] is True, result


def test_config_dir_env_absent_preserves_existing_direct_invocation_behavior(
        tmp_path):
    """No SUPERTOOL_CONFIG_DIR at all means this adapter was invoked
    directly, outside supertool's own validator wiring. No scope claim is
    being made either way, so the pre-#2228 repo-bound walk still applies
    -- the same shape every test in
    test_changelog_fragment_assembler_root_bound_2178.py already relies
    on, pinned here under this issue's own name."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = _write_fragment(repo)

    scripts = repo / ".github" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REAL_ASSEMBLER, scripts / "assemble_changelog.py")

    result = _run(target, {})

    assert "skipped" not in result, result
    assert result["ok"] is True, result


def test_a_disjoint_unrelated_project_is_still_trusted(tmp_path):
    """Self-review finding (#2228): the fix's first cut refused ANY
    SUPERTOOL_CONFIG_DIR that was not `root` itself or an ancestor of it --
    which also refused a config directory that shares no ancestry with
    `root` AT ALL, an entirely ordinary shape when supertool is invoked
    with an explicit `path=` argument naming a file outside the
    config-owning project (`tests/test_changelog_fragment_write_receipt_1132.py`'s
    own end-to-end CLI test does exactly this: cwd inside THIS repo, target
    inside a sibling tmp_path project). That regressed #1132's pre-existing
    write-time guarantee for every such call. Only a config directory
    sitting STRICTLY ABOVE `root` -- the directory-of-clones shape -- may
    be refused; a disjoint sibling tree must still be trusted."""
    unrelated_config_owner = tmp_path / "some-other-project-entirely"
    unrelated_config_owner.mkdir()

    repo = tmp_path / "sibling-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = _write_fragment(repo)

    scripts = repo / ".github" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REAL_ASSEMBLER, scripts / "assemble_changelog.py")

    result = _run(target, {"SUPERTOOL_CONFIG_DIR": str(unrelated_config_owner)})

    assert "skipped" not in result, result
    assert result["ok"] is True, result


def test_explicit_override_bypasses_the_scope_check(tmp_path):
    """SUPERTOOL_CHANGELOG_ASSEMBLER names one exact path -- an operator's
    own explicit trust, not an inherited one -- and must still be honoured
    even when SUPERTOOL_CONFIG_DIR names a directory above the repo."""
    clones = tmp_path / "clones"
    clone = clones / "some-clone"
    clone.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    target = _write_fragment(clone)

    pinned = clone / "tools" / "my_assembler.py"
    pinned.parent.mkdir(parents=True)
    shutil.copy2(REAL_ASSEMBLER, pinned)

    result = _run(target, {
        "SUPERTOOL_CONFIG_DIR": str(clones),
        "SUPERTOOL_CHANGELOG_ASSEMBLER": str(Path("tools") / "my_assembler.py"),
    })

    assert "skipped" not in result, result
    assert result["ok"] is True, result
