#!/usr/bin/env python3




















from __future__ import annotations

import calendar
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
import transport  


def key(payload: Any) -> str:







    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


def path(prefix: str, digest: str) -> str:

    return os.path.join(transport.STATE_DIR, f"{prefix}.{digest}.snapshot.json")


def read(prefix: str, digest: str, member: str) -> dict[str, Any] | None:







    try:
        with open(path(prefix, digest), encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get(member), dict):
        return None
    return loaded


def elided_note(elided: list[str], total: int, noun: str, sigil: str,
                wider: str) -> list[str]:




















    if not elided:
        return []
    named = ", ".join(f"{sigil}{n}" for n in elided[:12])
    if len(elided) > 12:
        named += f", +{len(elided) - 12} more"
    return [f"radar: NOTE — {len(elided)} of {total} open {noun} are not on the "
            f"board: unchanged since the previous run and not a standing "
            f"problem ({named}). The footer counts all {total}, so rows plus "
            f"'unchanged not shown' is the whole population; `{wider}` prints "
            f"every row."]


def departed_note(departed: list[str], noun: str, sigil: str,
                  lookup: str, capped: bool = False) -> list[str]:

























    if not departed:
        return []









    departed = sorted(departed,
                      key=lambda n: (0, int(n), "") if n.isdecimal() else (1, 0, n))
    named = ", ".join(f"{sigil}{n}" for n in departed[:12])
    if len(departed) > 12:
        named += f", +{len(departed) - 12} more"
    plural = noun if len(departed) == 1 else f"{noun}s"
    if capped:







        return [f"radar: WARNING — {len(departed)} {plural} on the previous "
                f"snapshot are not on this one ({named}), and this board "
                f"cannot call that a departure: the live query returned a full "
                f"page, so an entry pushed past the page limit by newer ones "
                f"looks exactly like one that left. Merged, closed, no longer "
                f"matching this board's filter, or simply past the page limit "
                f"— `{lookup}` says which."]
    return [f"radar: NOTE — {len(departed)} {plural} left this board since the "
            f"previous run ({named}): merged, closed, or still open and no "
            f"longer matching this board's filter. The snapshot records "
            f"membership, not how it ended, so this board does not guess — "
            f"`{lookup}` says which."]






SINCE_KEY = "_since"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:

    return time.strftime(_TS_FMT, time.gmtime())


def facts(entry: Any) -> Any:







    if not isinstance(entry, dict):
        return entry
    return {k: v for k, v in entry.items() if k != SINCE_KEY}


def stamp(entry: dict[str, Any], previous: Any, now: str | None = None
          ) -> dict[str, Any]:















    out = dict(entry)
    prior = previous.get(SINCE_KEY) if isinstance(previous, dict) else None
    held = bool(prior) and isinstance(prior, str) and facts(previous) == facts(entry)
    out[SINCE_KEY] = prior if held else (now or now_iso())
    return out


def unchanged_minutes(entry: Any, now: str | None = None) -> float | None:













    if not isinstance(entry, dict):
        return None
    raw = entry.get(SINCE_KEY)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        then = calendar.timegm(time.strptime(raw, _TS_FMT))
        current = calendar.timegm(time.strptime(now or now_iso(), _TS_FMT))
    except ValueError:
        return None
    return max(0.0, (current - then) / 60.0)


def unchanged_label(minutes: float, state: str) -> str:













    unit = f"{int(minutes // 60)}h" if minutes >= 120 else f"{int(minutes)}m"
    return f"{state or 'in progress'} {unit} unchanged"


def write(prefix: str, digest: str, entries: dict[str, Any], member: str) -> None:



















    transport.write_json_contained(path(prefix, digest), {member: entries})
