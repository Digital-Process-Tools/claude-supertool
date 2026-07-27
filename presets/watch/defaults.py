#!/usr/bin/env python3
"""Shared defaults for the watch preset's "watch everything of mine" flows.

`watch-mine.sh` (shell supervisor) and `radar.py` (reconcile op) spawn
watchers over the same population with the same event filter. Two literals in
two languages drift silently, and the symptom of drift is a watcher that
exists but never notifies — so both read the values from here.

Printed one-per-invocation so shell can read them:

    python3 defaults.py feed
    python3 defaults.py only
"""
from __future__ import annotations

import sys

# Every open MR, not just the failing ones: a watcher can only report an MR
# going red if it was already watching while the MR was green.
DEFAULT_FEED = "gl-mrs:author=@me,state=opened,iids"

DEFAULT_SOURCE = "gitlab-mr"

# pipeline_succeeded closes the red -> fix -> push -> ? loop. pipeline_running
# is excluded (the user just pushed; it carries no information) and
# comment_added is excluded because user_notes_count counts system notes.
DEFAULT_ONLY = "pipeline_failed,pipeline_succeeded,merged,closed,conflicts_appeared"

# The discovery tier. DEFAULT_FEED above is a one-shot query a caller runs and
# pipes; this is the poller that runs the same query forever, so a session
# started before an MR existed still learns about it.
DEFAULT_FEED_SOURCE = "gitlab-mr-feed"

# Scope, not an id: an alias the feed source resolves to a gl-mrs filter.
DEFAULT_FEED_SCOPE = "@me"

# mr_opened is on by default because it is the event that closes the discovery
# gap, and a board that gains a row without saying so is the failure being
# fixed. It is deliberately NOT added to DEFAULT_ONLY: that filter belongs to
# the gitlab-mr source, which cannot emit mr_opened, and listing an event
# beside a source that never emits it is a claim that is simply untrue.
# mr_merged/mr_closed are on so a merge is still reported when no per-MR
# watcher was alive to report it; the feed emits them without a desktop
# notification so the normal path does not ping twice.
DEFAULT_FEED_ONLY = "mr_opened,mr_merged,mr_closed,mr_left_feed"

VALUES = {
    "feed": DEFAULT_FEED,
    "source": DEFAULT_SOURCE,
    "only": DEFAULT_ONLY,
    "feed-source": DEFAULT_FEED_SOURCE,
    "feed-scope": DEFAULT_FEED_SCOPE,
    "feed-only": DEFAULT_FEED_ONLY,
}


def main(argv: list[str]) -> int:
    key = argv[1] if len(argv) > 1 else ""
    if key not in VALUES:
        print(f"ERROR: unknown default {key!r}. Known: {', '.join(sorted(VALUES))}",
              file=sys.stderr)
        return 1
    print(VALUES[key])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
