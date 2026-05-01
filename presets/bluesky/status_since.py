#!/usr/bin/env python3
"""Bluesky status_since: bluesky_status_since[:ISO_TIMESTAMP]

Native notifications endpoint (unlike Forem) — no outbound ledger needed.
Surfaces likes, replies, mentions, follows since the timestamp.
No arg = uses ~/.config/bluesky/last_check (auto-tracked).
"""
import datetime as _dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _atproto import get_session, xrpc
from _auth import get_app_password, get_handle

STATE_FILE = Path(os.path.expanduser("~/.config/bluesky/last_check"))
DEFAULT_LOOKBACK_HOURS = 24


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_state() -> str | None:
    try:
        return STATE_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def _write_state(value: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(value + "\n")


def resolve_since(arg: str) -> str:
    if arg:
        return arg
    stored = _read_state()
    if stored:
        return stored
    fallback = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    return fallback.strftime("%Y-%m-%dT%H:%M:%SZ")


def render(notifications: list[dict], since: str, now: str) -> str:
    fresh = [n for n in notifications if (n.get("indexedAt") or "") > since]
    out = [f"=== Bluesky since {since} (now {now}) ==="]
    if not fresh:
        out.append("NEW NOTIFICATIONS: (none)")
        out.append("--- NEXT ---")
        out.append("  bluesky_search:QUERY     — search for mentions / topics")
        out.append("  bluesky_list:5           — see your recent posts")
        return "\n".join(out)
    by_kind: dict[str, list[dict]] = {}
    for n in fresh:
        by_kind.setdefault(n.get("reason", "?"), []).append(n)
    for kind in ("mention", "reply", "quote", "like", "repost", "follow"):
        items = by_kind.get(kind, [])
        if not items:
            continue
        out.append(f"{kind.upper()}S ({len(items)}):")
        for n in items:
            author = n.get("author") or {}
            date = (n.get("indexedAt") or "").split("T")[0]
            uri = n.get("uri", "?")
            rec = n.get("record") or {}
            text = (rec.get("text") or "").replace("\n", " ")[:160]
            out.append(f"  [{uri}] {date} @{author.get('handle','?')}: {text}")
            if kind in ("mention", "reply", "quote"):
                out.append(f"    NEXT: bluesky_publish:\"MSG\"|{uri}  — reply")
            elif kind == "follow":
                out.append(f"    NEXT: bluesky_follow:{author.get('handle','HANDLE')}  — follow back")
    out.append("--- NEXT ---")
    out.append("  bluesky_search:QUERY     — search for mentions / topics")
    out.append("  bluesky_list:5           — see your recent posts")
    return "\n".join(out)


def main(arg: str) -> None:
    since = resolve_since(arg)
    now = _now_iso()
    handle = get_handle()
    session = get_session(handle, get_app_password())
    limit = int(os.environ.get("SUPERTOOL_STATUS_LIMIT", "50"))
    data = xrpc("app.bsky.notification.listNotifications", session, params={"limit": limit})
    print(render(data.get("notifications") or [], since, now))
    _write_state(now)


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
