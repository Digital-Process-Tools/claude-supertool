"""`.oss.json` set no `curate_route_threshold` (#2561), so the curate route
in the oss plugin's `workspace_routes.py` was never evaluated -- absent key
means the route is silently skipped rather than reported `not-due` for a
reason. `trap.d/` had accumulated 48 fragments with nothing able to route a
curate pass to drain them.

The maintainer's decision is to set the threshold low (2, matching
`triage_route_threshold` from #2557/#2558) rather than draining the backlog
by hand first: `commands/run/curate.md` states a curate pass takes its whole
waiting backlog uncapped in one run ("One pass takes the whole backlog,
uncapped"), so a low threshold does not cause a repeated partial-drain -- the
very next curate pass drains all 49 fragments in `trap.d/` in one pull
request, and the threshold then stays low so the directory never
re-accumulates.

This is a presence guard, not a behavioural one -- the routing logic itself
(`oss_config.py`, `workspace_routes.py`, `curate_trigger.py`) lives in the
separate `claude-oss` repo, which this repo's own config is read by but does
not own. What this test can pin from inside claude-supertool is that the key
stays present and holds a sane value, so the gap this issue found cannot
silently reopen by a later edit to `.oss.json` that drops it back out.
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _config() -> dict:
    return json.loads((REPO / ".oss.json").read_text(encoding="utf-8"))


def test_curate_route_threshold_is_configured() -> None:
    config = _config()
    assert "curate_route_threshold" in config, (
        "`.oss.json` has no `curate_route_threshold` -- the curate route "
        "can never evaluate as `over`, so a curate pass can never become "
        "due through it (#2561)")
    threshold = config["curate_route_threshold"]
    assert isinstance(threshold, int) and not isinstance(threshold, bool), (
        "curate_route_threshold must be a plain int, got %r" % (threshold,))
    assert threshold >= 0, (
        "curate_route_threshold must be non-negative, got %r" % (threshold,))
