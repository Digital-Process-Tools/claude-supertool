"""`gh-since-tag` is retired — the gate is `gh-prs:merged-since=TAG` (#1405).

A tombstone rather than a deletion. An op that vanishes from the registry
becomes an unknown token, and "unknown op" is the wrong sentence for a name
that worked yesterday and whose capability is still here: the reader is left to
guess whether it was renamed, removed, or never existed. This prints where it
went and exits non-zero.

The judgement did not go anywhere — it lives in `_release_gate.py`, which is
this file's own history under a new name (`git log --follow`). What changed is
the boundary's spelling. `merged-since=` shipped in #1411 taking an ISO date or
instant, and it could not carry the instant: supertool splits an op argument on
':', so a full timestamp is three segments and the value is gone before any
filter is parsed. That left a bare date, which is midnight UTC — against
v0.35.0's real boundary, `merged:>2026-08-11T00:00:00Z` returns 75 PRs where
`merged:>2026-08-11T18:57:19Z` returns 20.

A tag name has no ':' in it, which is what makes the fold expressible at all.
"""
from __future__ import annotations

import sys

MESSAGE = """ERROR: gh-since-tag is retired. It is a filter on gh-prs now (#1405).

  gh-since-tag              ->  gh-prs:merged-since=<newest tag>,state=merged
  gh-since-tag:v0.34.0      ->  gh-prs:merged-since=v0.34.0,state=merged
  gh-since-tag:v0.34.0:per=200
                            ->  gh-prs:merged-since=v0.34.0,state=merged,per=200

  The tag must be named. The retired op defaulted to the newest version-shaped
  tag reachable from the default branch; a filter value cannot, because
  resolving that default has three outcomes and one of them is 'two boundaries
  are defensible' -- which a listing has no way to carry. Run `git tag` if you
  need to see the candidates.

  Everything else survives the move: the boundary's three states, the
  local-history cross-check, the changelog.d count and the CONTRADICTION render
  are all in the footer of that board."""


def main() -> int:
    print(MESSAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
