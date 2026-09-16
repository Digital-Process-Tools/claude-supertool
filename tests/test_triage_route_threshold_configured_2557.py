"""`.oss.json` sets no `triage_route_threshold` (#2557), so the label-coverage
triage route in the oss plugin's `workspace_routes.py` is never evaluated --
absent key means the route is silently skipped rather than reported `not-due`
for a reason. `next_action.py`'s post-release trigger
(`release.triggers.triage_after_release`) is the other half of the same hole:
both left unset means nothing on this board can ever make a triage sweep due.

This is a presence guard, not a behavioural one -- the routing logic itself
(`oss_config.py`, `workspace_routes.py`, `triage_trigger.py`) lives in the
separate `claude-oss` repo, which this repo's own config is read by but does
not own. What this test can pin from inside claude-supertool is that the two
keys stay present and hold a sane value, so the gap this issue found cannot
silently reopen by a later edit to `.oss.json` that drops one back out.
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _config() -> dict:
    return json.loads((REPO / ".oss.json").read_text(encoding="utf-8"))


def test_triage_route_threshold_is_configured() -> None:
    config = _config()
    assert "triage_route_threshold" in config, (
        "`.oss.json` has no `triage_route_threshold` -- the label-coverage "
        "triage route can never evaluate as `over`, so a triage sweep can "
        "never become due through it (#2557)")
    threshold = config["triage_route_threshold"]
    assert isinstance(threshold, int) and not isinstance(threshold, bool), (
        "triage_route_threshold must be a plain int, got %r" % (threshold,))
    assert threshold >= 0, (
        "triage_route_threshold must be non-negative, got %r" % (threshold,))


def test_triage_after_release_trigger_is_enabled() -> None:
    config = _config()
    triggers = config.get("release", {}).get("triggers", {})
    assert triggers.get("triage_after_release") is True, (
        "`.oss.json`'s release.triggers.triage_after_release is not enabled "
        "-- the post-release trigger is the other half of the hole #2557 "
        "found, and next_action.py checks it before falling through to the "
        "label-coverage route")
