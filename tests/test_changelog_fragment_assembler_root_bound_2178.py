"""`_find_assembler`'s walk is bounded at the git repo root (#2178).

`_find_assembler` walked `[target.parent, *target.parent.parents]` with no
repo-root bound looking for `assemble_changelog.py` (or one of the other
`ASSEMBLER_LOCATIONS` names), then `_load` imports whatever it finds through
`importlib.util.spec_from_file_location` -- which *executes* the module on
import. A validator pointed at a fragment inside an untrusted checkout could
therefore import and run a script from anywhere at or above that fragment's
directory, including a sibling of the repo or its parent, since
`target.parent.parents` climbs all the way to filesystem root with nothing
to stop it.

This was partially exercised already: pointing
`SUPERTOOL_CHANGELOG_ASSEMBLER` at a relative path that climbed past the
repository root returned `skipped: "no assembler found ..."` once the walk
reached filesystem root without finding anything nameable -- confirming the
walk itself is unbounded, but not demonstrating a positive
malicious-script-actually-executed case.

This suite does: a script matching `ASSEMBLER_LOCATIONS` sits just *outside*
a git repo (a sibling directory of the repo root), and a fragment sits inside
the repo. Before the fix, the walk climbs out of the repo, finds the sibling
script, and `_load` executes it -- observable here because the planted
script writes a marker file as an unmistakable side effect of having run.
After the fix, the walk stops at the repo root and the marker is never
written; the validator reports the same `skipped` verdict it already gives
for "no assembler anywhere".

The companion case is the legitimate one: an assembler correctly placed
*inside* the repo, at or above the fragment, must still resolve exactly as
it did before -- closing the escape must not regress the working path.

Would these pass if the code did nothing? No: before the fix,
`test_a_script_outside_the_repo_is_never_imported_or_executed` observes the
marker file created by the planted script and fails; after the fix the walk
never reaches it. `test_an_assembler_inside_the_repo_still_resolves` pins the
accepting case so a walk that starts refusing everything (not just the
out-of-repo escape) cannot pass by never trying at all.
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

FRAGMENT_BODY = (
    "- **A thing** ([#1](https://github.com/"
    "Digital-Process-Tools/claude-supertool/issues/1)). Prose.\n"
)

#: A script that, if imported, proves it ran -- rather than asserting on
#: `_load`'s own exception shape, which is an implementation detail of how
#: the escape happens to fail today and not of whether it happened.
MALICIOUS_ASSEMBLER = (
    "import pathlib\n"
    "pathlib.Path(__file__).with_name('PWNED').write_text('pwned')\n"
)


def _run(target: Path, env: dict | None = None) -> dict:
    full_env = dict(os.environ)
    full_env.pop("SUPERTOOL_CHANGELOG_ASSEMBLER", None)
    full_env.update(env or {})
    proc = subprocess.run([sys.executable, str(ADAPTER), str(target)],
                          capture_output=True, text=True, env=full_env,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _write_fragment(repo: Path) -> Path:
    frag_dir = repo / "changelog.d"
    frag_dir.mkdir(parents=True, exist_ok=True)
    target = frag_dir / "1.fixed.md"
    target.write_text(FRAGMENT_BODY, encoding="utf-8")
    return target


def test_a_script_outside_the_repo_is_never_imported_or_executed(tmp_path):
    """The escape (#2178): an assembler-shaped script sitting just above the
    repo root must not be found, imported, or executed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = _write_fragment(repo)

    # Sibling of the repo, matching one of ASSEMBLER_LOCATIONS's names --
    # `target.parent.parents` reaches it (and reaches past it) on the old,
    # unbounded walk.
    outside_scripts = tmp_path / "scripts"
    outside_scripts.mkdir()
    outside_assembler = outside_scripts / "assemble_changelog.py"
    outside_assembler.write_text(MALICIOUS_ASSEMBLER, encoding="utf-8")

    result = _run(target)

    assert not (outside_scripts / "PWNED").exists(), (
        "the out-of-repo script ran -- the walk was not bounded at the "
        "repo root: " + json.dumps(result))
    assert "skipped" in result, result
    assert "ok" not in result, result


def test_an_assembler_inside_the_repo_still_resolves(tmp_path):
    """The working case (control): must not regress while closing the escape."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = _write_fragment(repo)

    real_assembler = REPO / ".github" / "scripts" / "assemble_changelog.py"
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copy2(real_assembler, scripts / "assemble_changelog.py")

    result = _run(target)

    assert "skipped" not in result, result
    assert result["ok"] is True, result


def test_not_inside_a_git_repository_refuses_rather_than_walking_unbounded(tmp_path):
    """No repo root to bound the walk to means no assembler, full stop.

    A fragment validated outside any git repository has no boundary to stop
    an unbounded walk at, so `_find_assembler` refuses outright instead of
    falling back to the old behaviour -- the strict choice the issue itself
    flags as likely correct.

    The planted script sits at literally `scripts/assemble_changelog.py` --
    one of `ASSEMBLER_LOCATIONS`'s own relative paths -- one level above the
    ungit-initialized directory the fragment lives in, both under this
    test's own unique `tmp_path` (never a shared base directory another test
    could also be writing under). That is exactly where the pre-#2178
    unbounded walk (or a regression that fell back to it whenever
    `_repo_root` returns `None`) would have found and executed it, so this
    pins the "no git repo -> refuse before even walking" branch rather than
    a script the walk could never have reached regardless of the fix.
    """
    not_a_repo = tmp_path / "not_a_repo"
    target = _write_fragment(not_a_repo)  # not_a_repo has no `.git` at all

    outside_scripts = tmp_path / "scripts"
    outside_scripts.mkdir()
    (outside_scripts / "assemble_changelog.py").write_text(
        MALICIOUS_ASSEMBLER, encoding="utf-8")

    result = _run(target)

    assert not (outside_scripts / "PWNED").exists(), result
    assert "skipped" in result, result


def test_git_unavailable_does_not_claim_locations_were_tried(tmp_path):
    """A distinct #2178 finding, from the self-review's own auditor spawn.

    With `git` unreachable, `_find_assembler` never gets far enough to try
    any of `ASSEMBLER_LOCATIONS` -- `_repo_root` cannot even determine
    whether the fragment is inside a repo. Before this test's fix, the
    `skipped` message nonetheless read "tried .github/scripts/..., .oss/...,
    scripts/..." -- untrue, and indistinguishable from the genuine
    "this project has no fragment tooling" case. An in-repo, in-place,
    working assembler is planted here specifically so a false "tried and
    found nothing" claim cannot be confused with a project that really has
    none.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = _write_fragment(repo)

    real_assembler = REPO / ".github" / "scripts" / "assemble_changelog.py"
    scripts = repo / ".github" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(real_assembler, scripts / "assemble_changelog.py")

    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()

    result = _run(target, {"PATH": str(empty_bin)})

    assert "skipped" in result, result
    assert "ok" not in result, result
    msg = result["skipped"]
    assert "tried .github" not in msg, (
        "claims specific locations were tried when none could be, with git "
        "unreachable: " + msg)
    assert "no assembler location was tried at all" in msg, msg
    assert "git" in msg.lower(), msg
