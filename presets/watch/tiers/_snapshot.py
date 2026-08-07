#!/usr/bin/env python3
"""One filter-keyed snapshot store, shared by every radar tier.

A tier's delta is only as honest as its key. Two populations sharing one
snapshot file report every member of the first as new and every member of the
second as gone, which is a delta column that lies — worse than no delta at all
(`gl_mrs.read_snapshot`, #486). That reasoning is not GitLab's; it is the
reasoning of anything that keeps a previous board, so it lives once here rather
than being retyped per tier. `gh_prs` re-deriving it would have been the second
copy, and a second copy is how a fixed defect comes back.

What is deliberately *not* here: what a member is, what counts as moved, and
what the key is made of. Those are the tier's semantics — a GitLab MR is keyed
by pipeline id and a GitHub PR by head SHA, and forcing one shape on both is the
bend this module exists to avoid.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
import transport  # noqa: E402


def key(payload: Any) -> str:
    """Stable short hash of whatever identifies a population.

    Order-insensitive by construction: the caller normalises (sorted, deduped)
    and `sort_keys` does the rest, so `author=a,author=b` and the reverse are
    one population and one file — the same reason `gl_mrs.canonical_filter_string`
    exists (#476).
    """
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


def path(prefix: str, digest: str) -> str:
    """Where the snapshot for one population lives. Read by `radar-state`."""
    return os.path.join(transport.STATE_DIR, f"{prefix}.{digest}.snapshot.json")


def read(prefix: str, digest: str, member: str) -> dict[str, Any] | None:
    """The previous board, or `None` on cold start.

    `None` and `{"<member>": {}}` are different answers and stay different: an
    absent file is "nobody has looked before", an empty one is "the population
    was empty last time". A tier that collapsed them would print a cold-start
    full board forever, or never.
    """
    try:
        with open(path(prefix, digest), encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get(member), dict):
        return None
    return loaded


def write(prefix: str, digest: str, entries: dict[str, Any], member: str) -> None:
    """Replace the snapshot atomically, or leave the old one in place.

    A half-written snapshot is a board that reports rows as changed which did
    not change, so the failure mode of an unwritable state dir is "no delta
    this run", never "a wrong delta next run".
    """
    target = path(prefix, digest)
    tmp = f"{target}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({member: entries}, f, indent=2)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
