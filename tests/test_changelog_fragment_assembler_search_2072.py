"""The changelog-fragment adapter searches known assembler locations (#2072).

`_find_assembler` walked parents looking for exactly one relative path,
`.github/scripts/assemble_changelog.py`. `/oss:scaffold` writes the assembler
to `.oss/assemble_changelog.py` (or `scripts/assemble_changelog.py`), so every
repository that plugin has ever set up hit the `skipped` branch by
construction — and the message on that branch claimed a fact about the
*project* ("this project does not declare changelog fragment rules") that the
adapter never checked. It checked one path, not the project.

Two fixes: search a short list of known locations, in order, before giving up
(`ASSEMBLER_LOCATIONS`, mirroring `claude-oss`'s own `oss_rules.py:45`), and
say what was actually done — the locations tried — rather than asserting the
project declares nothing.

Would this pass if the code did nothing? No: with only the old hardcoded
`.github/scripts/...` path, `.oss/assemble_changelog.py` never resolves and
the message asserted below is not the one currently emitted.
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
ASSEMBLER = REPO / ".github" / "scripts" / "assemble_changelog.py"

#: A hermetic fixture's own fragment name, deliberately NOT this PR's own
#: issue number (#1293's own remedy): this PR's own pending changelog fragment
#: is a real, currently-tracked file that the tag shipping this change
#: deletes -- a fixture spelling that same name would go from a correct
#: reference to a stale one on the very release this adapter change ships in.
#: 999999 names no issue this repo has ever filed.
FIXTURE_ISSUE = "999999"
WELL_FORMED = (
    "- **A fragment** ([#" + FIXTURE_ISSUE + "](https://github.com/"
    "Digital-Process-Tools/claude-supertool/issues/" + FIXTURE_ISSUE + ")). "
    "Body text.\n"
)


def _run(target: Path) -> dict:
    env = dict(os.environ)
    env.pop("SUPERTOOL_REQUIRE_VALIDATORS", None)
    env.pop("SUPERTOOL_CHANGELOG_ASSEMBLER", None)
    proc = subprocess.run([sys.executable, str(ADAPTER), str(target)],
                          capture_output=True, text=True, env=env,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _write(project: Path, name: str, body: str) -> Path:
    target = project / "changelog.d" / name
    target.write_text(body, encoding="utf-8")
    return target


def test_assembler_at_oss_location_is_found(tmp_path):
    """`.oss/assemble_changelog.py` is what /oss:scaffold actually writes."""
    project = tmp_path
    (project / "changelog.d").mkdir(parents=True)
    oss = project / ".oss"
    oss.mkdir(parents=True)
    shutil.copy2(ASSEMBLER, oss / "assemble_changelog.py")

    result = _run(_write(project, FIXTURE_ISSUE + ".fixed.md", WELL_FORMED))
    assert "skipped" not in result, result
    assert result["ok"] is True, result


def test_assembler_at_scripts_location_is_found(tmp_path):
    """The other location `claude-oss`'s `ASSEMBLER_LOCATIONS` declares."""
    project = tmp_path
    (project / "changelog.d").mkdir(parents=True)
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ASSEMBLER, scripts / "assemble_changelog.py")

    result = _run(_write(project, FIXTURE_ISSUE + ".fixed.md", WELL_FORMED))
    assert "skipped" not in result, result
    assert result["ok"] is True, result


def test_no_assembler_anywhere_names_every_location_tried(tmp_path):
    """The miss must not read as a fact about the project (#2072)."""
    project = tmp_path
    (project / "changelog.d").mkdir(parents=True)

    result = _run(_write(project, FIXTURE_ISSUE + ".fixed.md", WELL_FORMED))
    assert "skipped" in result
    msg = result["skipped"]
    assert "does not declare" not in msg, msg
    assert ".github" in msg and ".oss" in msg and "scripts" in msg, msg
