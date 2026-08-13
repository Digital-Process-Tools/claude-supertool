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

# The population every "watch everything of mine" flow covers, in gl-mrs
# filter vocabulary. Radar resolves its board, its fleet and its feed from
# this when given no argument, so editing it here moves all of them together
# — the drift this module exists to prevent, in the one field a caller is
# most likely to change.
DEFAULT_FILTER = "author=@me,state=opened"

# Every open MR, not just the failing ones: a watcher can only report an MR
# going red if it was already watching while the MR was green.
DEFAULT_FEED = f"gl-mrs:{DEFAULT_FILTER},iids"

DEFAULT_SOURCE = "gitlab-mr"

# pipeline_succeeded closes the red -> fix -> push -> ? loop. pipeline_running
# is excluded: the user just pushed, so it carries no information.
#
# comment_added is IN as of #519. It was held out on the belief that
# `user_notes_count` counts system notes, which would have double-fired it on
# every pipeline transition. That belief was wrong — GitLab scopes the count
# over `where(system: false)`, verified across twelve MRs on the live instance
# — so the one stated reason for the exclusion did not survive checking. A
# comment on your MR is actionable and otherwise silent, which is the same
# argument that puts conflicts_appeared here.
# mr_unreachable is IN as of #541. It is the one event that reports the watcher
# itself rather than the MR, and leaving it out of the default filter would keep
# the defect exactly where it hurts: every "watch everything of mine" flow
# spawns with DEFAULT_ONLY, so a radar board full of live-looking rows observing
# nothing is the default configuration. It is edge-triggered — once per outage,
# not once per poll — so the cost is one line when your token expires, and the
# argument is the same one that carries conflicts_appeared and comment_added:
# actionable, and otherwise entirely silent.
DEFAULT_ONLY = ("pipeline_failed,pipeline_succeeded,comment_added,"
                "merged,closed,conflicts_appeared,mr_unreachable")

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
# mrs_unreachable is IN as of #1602, for the same argument that put
# mr_unreachable in DEFAULT_ONLY above and one degree stronger. Every radar run
# spawns the feed with this filter, so leaving it out would mean an outage
# reaches the operator only if they went and configured a non-default `only=` —
# i.e. never, in the default configuration, which is the one the defect lives
# in. A feed reports discoveries, so its silence is its healthy state and there
# is no second signal to notice its absence by. Edge-triggered: one line when
# your token expires, nothing while it works.
DEFAULT_FEED_ONLY = "mr_opened,mr_merged,mr_closed,mr_left_feed,mrs_unreachable"

VALUES = {
    "feed": DEFAULT_FEED,
    "filter": DEFAULT_FILTER,
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
