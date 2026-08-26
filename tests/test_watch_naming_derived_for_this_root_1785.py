"""#1785: `naming.project_notes`' "may be another project's fleet" warning
fires on the *healthy* path, not only the shared-accident one.

`bin/oss-workspace` (the consuming plugin) derives `SUPERTOOL_WATCH_NAME`
from a repository's own `.oss.json` slug precisely because nothing here
declares one, and exports it -- a deliberate fix for a repo that would
otherwise bind the shared default socket without knowing. That designed
path is `DECLARED_SILENT` here (a `.supertool.json` exists, no op block
names `watch_name`), and it is indistinguishable from a hand-copied name
leaking in from an unrelated project's `settings.local.json`, which is the
case the warning exists for.

The fix is the same shape the issue proposes: a second variable naming the
root the export was derived for. Present and matching, `project_notes` has
positive evidence this is not an inherited name; absent, or naming some
other root, nothing changes -- every existing case in
`tests/test_watch_transport_repo_attribution_1952.py`'s sibling suites for
this module stays exactly as it read before.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

NAMING = Path(__file__).parent.parent / "presets" / "watch" / "naming.py"
_spec = importlib.util.spec_from_file_location("watch_naming_1785", NAMING)
assert _spec is not None and _spec.loader is not None
naming = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(NAMING.parent.parent))
_spec.loader.exec_module(naming)


def _resolved(name="myrepo"):
    return naming.Resolved(name=name, sock="/tmp/x.sock", state_dir="/tmp/x",
                           notes=[], refusal="")


def _declared_silent(path="/repo/root/.supertool.json"):
    return naming.Declared(state=naming.DECLARED_SILENT, path=path, names=(),
                           declaring_ops=(),
                           silent_ops=("channel", "radar", "unwatch", "watch", "watches"))


def _fleet_warned(lines: list[str]) -> bool:
    return any("may be another project" in line for line in lines)


# ---------------------------------------------------------------------------
# the designed path: a marker naming this exact root silences the warning
# ---------------------------------------------------------------------------

def test_a_name_derived_for_this_root_is_not_reported_as_another_projects_fleet() -> None:
    env = {naming.ROOT_ENV: "/repo/root"}
    lines = naming.project_notes(_resolved(), _declared_silent(), env=env)
    assert not _fleet_warned(lines), lines
    assert lines, "silence with no reason said is the failure this file exists to close"


def test_the_derivation_marker_is_normalised_before_comparison() -> None:
    """A trailing slash or a relative-looking join must not defeat the match --
    `bin/oss-workspace` exports whatever `os.path` gave it, not necessarily in
    the exact string form this reads the config path in."""
    env = {naming.ROOT_ENV: "/repo/root/"}
    lines = naming.project_notes(_resolved(), _declared_silent("/repo/root/.supertool.json"),
                                 env=env)
    assert not _fleet_warned(lines), lines


# ---------------------------------------------------------------------------
# must-fire pairs: every case the warning exists for must keep warning
# ---------------------------------------------------------------------------

def test_no_marker_at_all_still_warns() -> None:
    """The must-fire twin: fixing #1785 must not go silent by default."""
    lines = naming.project_notes(_resolved(), _declared_silent(), env={})
    assert _fleet_warned(lines), lines


def test_a_marker_naming_a_different_root_still_warns() -> None:
    """The marker is per-directory, not a blanket opt-out -- a name derived for
    one repo and leaked into a sibling checkout must still be caught."""
    env = {naming.ROOT_ENV: "/some/other/repo"}
    lines = naming.project_notes(_resolved(), _declared_silent("/repo/root/.supertool.json"),
                                 env=env)
    assert _fleet_warned(lines), lines


def test_declared_no_config_is_unaffected() -> None:
    """No `.supertool.json` anywhere above the cwd has no root to validate the
    marker against, so this arm is deliberately untouched by #1785's fix."""
    declared = naming.Declared(state=naming.DECLARED_NO_CONFIG, path="", names=(),
                               declaring_ops=(), silent_ops=tuple(naming.WATCH_OPS))
    env = {naming.ROOT_ENV: "/anything"}
    lines = naming.project_notes(_resolved(), declared, env=env)
    assert _fleet_warned(lines), lines


def test_a_real_disagreement_still_warns_regardless_of_the_marker() -> None:
    """The marker answers "is this an inherited name", never "is this
    fleet shared" -- a project whose own config declares a *different* name
    must keep reading as not-this-project's-channel even with the marker set,
    because that branch is evidence of an actual conflict, not an absence."""
    declared = naming.Declared(state=naming.DECLARED_FOUND,
                               path="/repo/root/.supertool.json",
                               names=("theirs",), declaring_ops=("radar",),
                               silent_ops=())
    env = {naming.ROOT_ENV: "/repo/root"}
    lines = naming.project_notes(_resolved(), declared, env=env)
    assert any("not this project" in line for line in lines), lines
