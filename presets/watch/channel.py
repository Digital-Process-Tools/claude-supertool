"""`channel:health` — is the watch -> Claude-session bridge actually delivering?

The question #554 asks, and the reason it needs its own op: `pgrep -fl channel.ts`,
`lsof /tmp/supertool-watch.sock` and writing to that socket are all green whether
events reach the session or not. Three probes, one answer, no information.

**Delivery into a Claude session is not observable from outside it, and this op
does not pretend otherwise.** `channel.ts` reaches the session through
`mcp.notification()` — a JSON-RPC notification, so no id, no response and
nothing to await — and it never writes back to the producer connection either.
No ack exists to read. That is a finding, not a gap in this implementation, and
it is why the answer here has three states rather than two:

    NOT DELIVERING   a definite negative. Nothing is listening on the socket, so
                     every event a poller emits right now is lost at the source.
    FORWARDING       a consumer is bound, alive, and its own counters say it has
                     handed N events to the MCP transport. The strongest positive
                     fact available anywhere outside the session.
    CANNOT DETERMINE something took the bytes and nothing here can see what it
                     did with them.

`CANNOT DETERMINE` is the point of the op rather than its failure mode. It is
the state today's tooling reports as green, and reporting it as green produced a
confidently wrong diagnosis in both directions on 2026-07-29 (#554's own
account) — first "transport is fine" off `sent ok`, then "the radar is dead" off
a drop line, while it had already recovered.

Exit codes are the three states, on purpose: 0 forwarding, 1 not delivering, 3
cannot determine. A single non-zero for the last two would put the two answers
this op exists to separate back into one bucket.

**Measured caveat, so nobody builds on a code that is not there.** The supertool
wrapper reports any non-zero op as `FAIL` and exits 1, so 3 survives only when
this file is run directly (`python3 presets/watch/channel.py health`). Through
`supertool 'channel:health'` the three states are carried by the *first line* of
the report — `channel: FORWARDING` / `NOT DELIVERING` / `CANNOT DETERMINE` —
which is what the tests key on and what a caller should key on too.
"""
from __future__ import annotations

import calendar
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _proc

import _proc  # noqa: E402  (the one liveness probe, shared with gl-mrs / gh-prs)

SOCK_PATH = os.environ.get("SUPERTOOL_WATCH_SOCK") or "/tmp/supertool-watch.sock"
STATE_DIR = os.environ.get("SUPERTOOL_WATCH_STATE_DIR") or "/tmp"

#: Where a consumer publishes its own counters. Derived from the socket path
#: rather than fixed, so two sessions running on separate `SUPERTOOL_WATCH_SOCK`
#: paths (the documented multi-session arrangement) get separate health files
#: instead of overwriting each other's — which would be this issue's defect
#: rebuilt inside its own fix.
HEALTH_SUFFIX = ".health.json"

#: How long a consumer's counters may go unrefreshed before they stop counting
#: as evidence. `channel.ts` rewrites the file on a heartbeat every 10s whether
#: or not events are flowing, precisely so that "idle" and "wedged" are
#: distinguishable.
#:
#: 45s is four missed beats plus half of a fifth. The half is deliberate: at a
#: flat multiple, a beat that lands a few hundred milliseconds late on a loaded
#: machine puts a perfectly healthy consumer over the line, and a spurious
#: CANNOT DETERMINE trains a reader to ignore the one state this op exists to
#: make legible.
STALE_AFTER_SECS = 45

#: The connect probe's budget. A healthy `net.createServer` accepts immediately.
CONNECT_TIMEOUT = 0.5

RC_FORWARDING = 0
RC_NOT_DELIVERING = 1
RC_UNKNOWN = 3

#: Printed in every report, including the healthy one. The ceiling belongs on
#: the surface that would otherwise be read as proof: a reader who only ever
#: saw it in the failure cases would learn that FORWARDING means delivered.
CEILING = (
    "`forwarded` counts events handed to the MCP transport by the consumer.\n"
    "Whether they appeared in a Claude session is not observable from here, or\n"
    "from any process except that session: the bridge sends a JSON-RPC\n"
    "notification, which has no response to wait on."
)


def _parse_iso(value: Any) -> float | None:
    """Epoch seconds for one of our own `%Y-%m-%dT%H:%M:%SZ` stamps, or None.

    A fractional-seconds part is tolerated rather than refused. The health file
    is written by a *separate program* on its own release cadence, so a stamp
    shaped `...:00.123Z` is a version skew — and rejecting it here would report
    CANNOT DETERMINE for a consumer that is forwarding perfectly, which is this
    issue's defect with the sign flipped.

    `calendar.timegm`, not `time.mktime`: the stamp is UTC, and mktime reads it
    as local time. The usual correction (`- time.timezone`) is wrong by an hour
    across a DST boundary, which would age a fresh stamp past the stale
    threshold twice a year.
    """
    if not isinstance(value, str):
        return None
    text = value
    if "." in text and text.endswith("Z"):
        text = text[:text.index(".")] + "Z"
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        return None


def probe_socket(path: str) -> tuple[str, str]:
    """Whether anything is listening. Same three states as `transport.Emit`.

    Deliberately a real `connect()` rather than an `lsof`/`pgrep` check: those
    are the two probes #554 names as indistinguishable from working, because a
    crashed consumer leaves the path behind and a live process can hold a socket
    it is no longer reading.
    """
    # Absence first, and deliberately before the capability check below: if the
    # path is not there, nobody is listening, and that is true whether or not
    # this interpreter could have opened a socket to find out.
    if not os.path.exists(path):
        return "no-listener", f"no socket at {path}"
    if not hasattr(socket, "AF_UNIX"):
        # Not a definite negative and not a crash. This interpreter cannot open
        # the kind of socket the bridge uses, so it has no evidence either way —
        # and an op whose whole subject is "declining beats guessing" must not
        # answer its own question with a traceback.
        return "unknown", "this platform has no AF_UNIX socket, so nothing here can probe the path"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(CONNECT_TIMEOUT)
        s.connect(path)
        return "accepted", f"{path} accepted a connection"
    except (ConnectionRefusedError, FileNotFoundError) as err:
        return "no-listener", f"{path} exists but refused the connection ({type(err).__name__})"
    except OSError as err:
        return "unknown", f"{type(err).__name__} connecting to {path}"
    finally:
        try:
            s.close()
        except OSError:
            pass


def read_health(path: str) -> tuple[dict | None, str]:
    """The consumer's published counters, or None and why not."""
    health_path = path + HEALTH_SUFFIX
    if not os.path.exists(health_path):
        return None, (
            "the bound consumer publishes no counters — it predates this field, or it is "
            "not claude-channel"
        )
    try:
        with open(health_path, "r", encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, json.JSONDecodeError) as err:
        return None, f"{health_path} could not be read ({type(err).__name__})"
    if not isinstance(record, dict):
        return None, f"{health_path} is not a JSON object"
    return record, ""


def _health_objection(record: dict) -> str:
    """Why these counters are not evidence, or "" when they are.

    The arms below are the same mistake one step apart: believing a number
    because it is present. A file left behind by a consumer that died is a
    frozen `forwarded` count that never decreases and reads as health forever.
    """
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return "its health file names no process"
    if not _proc.pid_alive(pid):
        return (
            f"its health file was written by pid {pid}, which is gone — something else "
            "is bound to this socket"
        )
    updated = _parse_iso(record.get("updated"))
    if updated is None:
        return "its health file carries no readable `updated` stamp"
    age = time.time() - updated
    if age > STALE_AFTER_SECS:
        return (
            f"pid {pid} is alive but has not refreshed its counters in {int(age)}s "
            f"(heartbeat is every 10s, stale after {STALE_AFTER_SECS}s) — it may be wedged"
        )
    return ""


def _int(record: dict, key: str) -> int:
    value = record.get(key)
    return value if isinstance(value, int) else 0


def stranded_watchers(path: str) -> list[tuple[str, str, dict]]:
    """Watchers whose last emit to `path` found nobody listening.

    The blast radius of a definite negative. "Nothing is listening" is a fact
    about the socket; "and these six pollers have been writing into it since
    09:14" is the one an operator can act on.
    """
    rows: list[tuple[str, str, dict]] = []
    try:
        names = sorted(os.listdir(STATE_DIR))
    except OSError:
        return rows
    for name in names:
        if not (name.startswith("supertool-watch-") and name.endswith(".state.json")):
            continue
        stem = name[len("supertool-watch-"):-len(".state.json")]
        source, _, watcher_id = stem.partition("__")
        if not watcher_id:
            continue
        try:
            with open(os.path.join(STATE_DIR, name), "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        # A watcher bound to a different socket is not stranded on this one; it
        # is somebody else's business, and reporting it here would be the
        # partial-migration confusion #581 records, inverted.
        if state.get("sock_path") not in (None, path):
            continue
        last = state.get("last_emit")
        if isinstance(last, dict) and last.get("state") == "no-listener":
            rows.append((source, watcher_id, last))
    return rows


def _render_stranded(path: str) -> list[str]:
    rows = stranded_watchers(path)
    if not rows:
        return ["  watchers : none recorded an emit into this socket"]
    lines = [f"  watchers : {len(rows)} found nobody listening on their last emit"]
    for source, watcher_id, last in rows[:10]:
        lines.append(f"             {source} {watcher_id} — last emit {last.get('ts', '?')}")
    if len(rows) > 10:
        lines.append(f"             ... and {len(rows) - 10} more")
    return lines


def health(path: str) -> tuple[int, str]:
    """The whole report, and the exit code that encodes its state."""
    state, detail = probe_socket(path)
    head = [f"  socket   : {path}", f"             {detail}"]

    if state == "no-listener":
        body = ["channel: NOT DELIVERING", *head,
                "  consumer : none — every event emitted right now is lost at the source"]
        body += _render_stranded(path)
        return RC_NOT_DELIVERING, "\n".join([*body, "", CEILING])

    if state == "unknown":
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            "  consumer : the socket could not be probed, so nothing is known either way",
            "", CEILING,
        ])

    record, why = read_health(path)
    if record is None:
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            f"  consumer : bound, but {why}",
            "             bytes are accepted; what happens to them is not visible here",
            "", CEILING,
        ])

    objection = _health_objection(record)
    if objection:
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            f"  consumer : bound, but {objection}",
            f"             last published counters: {_int(record, 'forwarded')} forwarded, "
            f"{_int(record, 'dropped')} dropped, updated {record.get('updated', '?')}",
            "", CEILING,
        ])

    return RC_FORWARDING, "\n".join([
        "channel: FORWARDING", *head,
        f"  consumer : pid {record.get('pid')}, up since {record.get('started', '?')}",
        f"  counters : {_int(record, 'lines_read')} lines read, "
        f"{_int(record, 'forwarded')} forwarded, {_int(record, 'dropped')} dropped",
        f"             last forwarded {record.get('last_forwarded') or 'never'}"
        f" (counters refreshed {record.get('updated', '?')})",
        "", CEILING,
    ])


def main(argv: list[str]) -> int:
    sub = argv[1] if len(argv) > 1 else "health"
    if sub != "health":
        sys.stderr.write(
            f"channel: unknown sub-op {sub!r} — the only one is `channel:health`\n"
        )
        return 2
    code, report = health(SOCK_PATH)
    print(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
