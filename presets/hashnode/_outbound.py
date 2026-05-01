"""Local outbound-comment tracking for Hashnode.

Hashnode's GraphQL has no `me.comments` field, so we record each
comment/reply locally and use the ledger during status_since to
detect replies on posts we've engaged with.

Format: JSON-lines at ~/.config/hashnode/my_outbound_comments.
"""
import json
import os
from pathlib import Path
from typing import Iterable

TRACK_FILE = Path(os.path.expanduser("~/.config/hashnode/my_outbound_comments"))


def append(record: dict) -> None:
    TRACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TRACK_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def read() -> list[dict]:
    if not TRACK_FILE.is_file():
        return []
    out: list[dict] = []
    for line in TRACK_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def unique_post_ids(records: Iterable[dict]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for r in records:
        pid = r.get("post_id")
        if isinstance(pid, str) and pid and pid not in seen:
            seen.add(pid)
            result.append(pid)
    return result


def my_comment_ids(records: Iterable[dict]) -> set[str]:
    return {str(r["comment_id"]) for r in records if r.get("comment_id")}
