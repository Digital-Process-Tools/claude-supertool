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


def _run(target: Path) -> dict:
    env = dict(os.environ)
    env.pop("SUPERTOOL_CHANGELOG_ASSEMBLER", None)
    proc = subprocess.run([sys.executable, str(ADAPTER), str(target)],
                          capture_output=True, text=True, env=env,
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
    """
    target = _write_fragment(tmp_path)  # tmp_path itself is not a git repo

    outside_scripts = tmp_path.parent / "scripts-{0}".format(tmp_path.name)
    outside_scripts.mkdir(exist_ok=True)
    (outside_scripts / "assemble_changelog.py").write_text(
        MALICIOUS_ASSEMBLER, encoding="utf-8")

    result = _run(target)

    assert not (outside_scripts / "PWNED").exists(), result
    assert "skipped" in result, result
