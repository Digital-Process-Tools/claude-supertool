#!/usr/bin/env python3












from __future__ import annotations

import sys






DEFAULT_FILTER = "author=@me,state=opened"



DEFAULT_FEED = f"gl-mrs:{DEFAULT_FILTER},iids"

DEFAULT_SOURCE = "gitlab-mr"



















DEFAULT_ONLY = ("pipeline_failed,pipeline_succeeded,comment_added,"
                "merged,closed,conflicts_appeared,mr_unreachable")




DEFAULT_FEED_SOURCE = "gitlab-mr-feed"


DEFAULT_FEED_SCOPE = "@me"

















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
