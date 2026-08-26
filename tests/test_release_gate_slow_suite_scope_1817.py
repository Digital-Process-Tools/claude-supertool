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

Returns `(excluded, note)`, not a bare list: a directory that could not be
listed at all must not render the same as one with zero excluded workflows,
the same `count_fragments`-style distinction `changelog.d` already gets a few
functions above it in this same module -- caught in self-review (#1817) after
the first cut of this function returned `[]` for both. `note` also names any
individual `.yml` file that could not be opened even when the directory
listing succeeded, so a partial read is not silently reported as a complete
one either.

`pull_request*` (not just the exact string `pull_request`) is read as
"already checked per PR" -- `pull_request_target`, `pull_request_review` and
`pull_request_review_comment` all fire before merge exactly like
`pull_request` does. Caught in the same self-review round: the first cut's
exact-string match would have told a maintainer a `pull_request_target`
workflow was "unmeasured, caught only by its own schedule", which is false --
it has no schedule and is checked on every PR.
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
    excluded, note = gate.not_gated_by_push_workflows(str(tmp_path))
    assert [w["name"] for w in excluded] == ["slow tests"]
    assert note == ""


def test_a_pull_request_only_workflow_is_named_too(tmp_path):
    _write(tmp_path, "changelog.yml",
           "name: changelog\non:\n  pull_request:\n    types: [opened]\n")
    excluded, note = gate.not_gated_by_push_workflows(str(tmp_path))
    assert [w["name"] for w in excluded] == ["changelog"]
    assert note == ""


def test_a_pull_request_target_workflow_is_named_as_pull_request_family_too():
    """Argued down in self-review: an exact `== "pull_request"` match would
    have EXCLUDED `pull_request_target` from this per-PR family and left it
    reported as 'unmeasured, own-schedule-only' below (see `assess`'s test),
    which is false -- `pull_request_target` also fires per PR, before merge,
    with no schedule of its own."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d), "privileged.yml",
               "name: privileged\non:\n  pull_request_target:\n")
        excluded, note = gate.not_gated_by_push_workflows(d)
    assert [w["name"] for w in excluded] == ["privileged"]
    assert note == ""


def test_a_push_triggered_workflow_is_not_named():
    """The 'must fire' half of the same fixture: a workflow a push DOES reach
    must never land in this list, or the release gate would claim `tests`
    itself is unmeasured."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d), "tests.yml", "name: tests\non:\n  push:\n"
               "    branches: [master]\n  pull_request:\n")
        excluded, note = gate.not_gated_by_push_workflows(d)
    assert excluded == []
    assert note == ""


def test_an_unparsable_trigger_block_is_not_claimed_as_excluded(tmp_path):
    """`None` (no `on:` block this parser can find) must not be read as
    'provably unreachable by a push' -- the same 'I could not tell' vs 'this
    cannot happen' distinction `_declared_workflows.is_push_triggered`
    already draws, and which `parse_triggers` returns `None` for."""
    _write(tmp_path, "odd.yml", "name: odd\n")
    excluded, note = gate.not_gated_by_push_workflows(str(tmp_path))
    assert excluded == []
    assert note == ""


def test_a_directory_that_cannot_be_listed_is_not_read_not_zero():
    """The 'must not fire the wrong way' half of the (excluded, note)
    contract: `None` (could not read) must never collapse to `[]` (read, and
    nothing was excluded) -- the same distinction `count_fragments` already
    draws for `changelog.d`. Caught in self-review; the first cut of this
    function returned a bare `[]` for both."""
    excluded, note = gate.not_gated_by_push_workflows("/no/such/directory/at/all")
    assert excluded is None
    assert note


def test_an_unopenable_workflow_file_is_named_in_the_note_not_dropped_silently(tmp_path):
    """A file `not_gated_by_push_workflows` cannot open is neither provably
    excluded nor provably included -- dropping it without a word would
    understate the set exactly as silently as claiming a directory read
    succeeded when it did not.

    Root (and some CI containers) ignore `0o000` and can read the file
    anyway -- this asserts the note only when the permission actually held,
    rather than skip outright, so the fixture still exercises the ordinary
    path (`slow tests` alone excluded) on every runner."""
    import os as _os
    _write(tmp_path, "slow-tests.yml",
           "name: slow tests\non:\n  schedule:\n    - cron: '0 6 * * *'\n")
    bad = tmp_path / "locked.yml"
    bad.write_text("name: locked\non:\n  schedule:\n    - cron: '0 0 * * *'\n",
                   encoding="utf-8")
    _os.chmod(bad, 0o000)
    permission_held = not _os.access(str(bad), _os.R_OK)
    try:
        excluded, note = gate.not_gated_by_push_workflows(str(tmp_path))
    finally:
        _os.chmod(bad, 0o644)

    names = sorted(w["name"] for w in excluded)
    if permission_held:
        assert "locked.yml" in note
        assert names == ["slow tests"]
    else:
        assert names == ["locked", "slow tests"]
        assert note == ""


def test_the_real_repo_workflows_name_changelog_and_slow_tests_only():
    """Run against this repo's own .github/workflows -- the fixture that
    matters, since #1817 was filed off this exact tree."""
    real_dir = Path(__file__).parent.parent / ".github" / "workflows"
    excluded, note = gate.not_gated_by_push_workflows(str(real_dir))
    names = sorted(w["name"] for w in excluded)
    assert names == ["changelog", "slow tests"], names
    assert note == ""


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


def test_assess_says_not_read_rather_than_a_measured_zero_when_the_dir_is_gone(tmp_path):
    """The wiring for the (excluded, note) contract: a workflows directory
    that could not be listed must render as NOT READ, not as the silence a
    zero-exclusions render also produces."""
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
        workflows_dir=str(tmp_path / "no-such-workflows-dir"))
    out = "\n".join(lines)
    assert "release scope: NOT READ" in out


def test_assess_reads_pull_request_target_as_already_checked_per_pr(tmp_path):
    """The `assess`-level half of the pull_request-family fix: a
    `pull_request_target` workflow must get the 'already checked' reading,
    never the 'UNMEASURED ... own schedule' one an exact `pull_request`
    match would give it."""
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    _write(workflows_dir, "privileged.yml",
           "name: privileged\non:\n  pull_request_target:\n")
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
    assert "already checked every PR" in out
    assert "UNMEASURED" not in out
