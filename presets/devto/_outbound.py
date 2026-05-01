"""Local outbound-comment tracking.

Dev.to has no public API to list comments authored by the user, so we
record each successful comment locally and use that ledger to scan
target articles for replies during status_since.

Format: JSON-lines at ~/.config/devto/my_outbound_comments. One record
per comment posted via this tool. Records never expire automatically;
prune by hand if needed.
"""
import json
import os
from pathlib import Path
from typing import Iterable

TRACK_FILE = Path(os.path.expanduser("~/.config/devto/my_outbound_comments"))


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


def unique_article_ids(records: Iterable[dict]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for r in records:
        aid = r.get("article_id")
        if isinstance(aid, int) and aid not in seen:
            seen.add(aid)
            result.append(aid)
    return result


def my_comment_ids(records: Iterable[dict]) -> set[str]:
    return {str(r["comment_id"]) for r in records if r.get("comment_id")}
