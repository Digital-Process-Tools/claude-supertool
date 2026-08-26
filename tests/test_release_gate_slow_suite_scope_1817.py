"""The release gate now says a `slow`-marked regression is unmeasured (#1817).

`gh-branch` already discloses which declared workflows produced no run on a
commit -- `undispatched_lines` / `scope_for` in `presets/github/branch.py` --
but that disclosure lives on a different op, read by a human deciding whether
to tag, not by the thing that actually gates a release. `slow tests` runs on
`schedule`/`workflow_dispatch` only (#891 -- see `.github/workflows/slow-
tests.yml`), so by construction it never produces a run on a release
candidate's own commit, and nothing in `presets/github/_release_gate.py`
(the module `gh-prs:merged-since=TAG` calls to build the release gate's own
report) said so.

`not_gated_by_push_workflows` answers the question locally, off the workflow
files on disk, rather than by querying a commit's run history the way
`_declared_workflows.declared_at` does: whether a `.github/workflows/*.yml`
file's `on:` block can ever be reached by a push is a property of the file,
not of one commit, and the release gate already reads `changelog.d` off the
same working tree without a network round trip. `None` (an unreadable `on:`
block) is excluded on purpose -- same reasoning as
`_declared_workflows.is_push_triggered`: "I could not tell" must not read as
"this is provably unreachable by a push".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

PRESET_PATH = (Path(__file__).parent.parent / "presets" / "github"
               / "_release_gate.py")
_spec = importlib.util.spec_from_file_location("release_gate_1817", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _write(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")


def test_a_schedule_only_workflow_is_named_as_not_reachable_by_a_push(tmp_path):
    _write(tmp_path, "slow-tests.yml",
           "name: slow tests\non:\n  schedule:\n    - cron: '0 6 * * *'\n"
           "  workflow_dispatch:\n")
    excluded = gate.not_gated_by_push_workflows(str(tmp_path))
    assert [w["name"] for w in excluded] == ["slow tests"]


def test_a_pull_request_only_workflow_is_named_too(tmp_path):
    _write(tmp_path, "changelog.yml",
           "name: changelog\non:\n  pull_request:\n    types: [opened]\n")
    excluded = gate.not_gated_by_push_workflows(str(tmp_path))
    assert [w["name"] for w in excluded] == ["changelog"]


def test_a_push_triggered_workflow_is_not_named():
    """The 'must fire' half of the same fixture: a workflow a push DOES reach
    must never land in this list, or the release gate would claim `tests`
    itself is unmeasured."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d), "tests.yml", "name: tests\non:\n  push:\n"
               "    branches: [master]\n  pull_request:\n")
        excluded = gate.not_gated_by_push_workflows(d)
    assert excluded == []


def test_an_unparsable_trigger_block_is_not_claimed_as_excluded(tmp_path):
    """`None` (no `on:` block this parser can find) must not be read as
    'provably unreachable by a push' -- the same 'I could not tell' vs 'this
    cannot happen' distinction `_declared_workflows.is_push_triggered`
    already draws, and which `parse_triggers` returns `None` for."""
    _write(tmp_path, "odd.yml", "name: odd\n")
    excluded = gate.not_gated_by_push_workflows(str(tmp_path))
    assert excluded == []


def test_the_real_repo_workflows_name_changelog_and_slow_tests_only():
    """Run against this repo's own .github/workflows -- the fixture that
    matters, since #1817 was filed off this exact tree."""
    real_dir = Path(__file__).parent.parent / ".github" / "workflows"
    excluded = gate.not_gated_by_push_workflows(str(real_dir))
    names = sorted(w["name"] for w in excluded)
    assert names == ["changelog", "slow tests"], names


def test_assess_reports_the_release_is_not_gated_by_the_excluded_workflows(tmp_path):
    """The wiring: `assess()` must actually include the note in what it
    renders, not just make the function available and unused."""
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    _write(workflows_dir, "slow-tests.yml",
           "name: slow tests\non:\n  schedule:\n    - cron: '0 6 * * *'\n")
    changelog_dir = tmp_path / "changelog.d"
    changelog_dir.mkdir()

    boundary = gate.Boundary(
        state=gate.BOUNDARY_RESOLVED,
        tag={"name": "v1.0.0", "sha": "deadbee", "commit_date": "2026-01-01T00:00:00Z"},
        sha="deadbee", stamp="", instant=None, branch_ref="master",
        sources=[], notes=[], refusal="")
    _kept, lines, _code = gate.assess(
        rows=[], boundary=boundary, per_page=100, fetched=0,
        changelog_dir=str(changelog_dir),
        workflows_dir=str(workflows_dir))
    out = "\n".join(lines)
    assert "slow tests" in out
    assert "not gated" in out.lower() or "NOT gated" in out


def test_assess_says_nothing_extra_when_no_workflow_is_excluded(tmp_path):
    """The 'must not fire' half: an empty result must not print a false claim
    that N workflows are excluded when N is 0."""
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    _write(workflows_dir, "tests.yml",
           "name: tests\non:\n  push:\n    branches: [master]\n")
    changelog_dir = tmp_path / "changelog.d"
    changelog_dir.mkdir()

    boundary = gate.Boundary(
        state=gate.BOUNDARY_RESOLVED,
        tag={"name": "v1.0.0", "sha": "deadbee", "commit_date": "2026-01-01T00:00:00Z"},
        sha="deadbee", stamp="", instant=None, branch_ref="master",
        sources=[], notes=[], refusal="")
    _kept, lines, _code = gate.assess(
        rows=[], boundary=boundary, per_page=100, fetched=0,
        changelog_dir=str(changelog_dir),
        workflows_dir=str(workflows_dir))
    out = "\n".join(lines)
    assert "release scope" not in out.lower()
