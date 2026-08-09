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
    FORWARDING       a consumer is bound and the counters it publishes beside the
                     socket are fresh and say it has handed N events to the MCP
                     transport. The strongest positive fact available anywhere
                     outside the session. The pid in that file is its writer's
                     own claim and not a verified identity — pids are reusable,
                     and nothing outside the consumer can prove the named process
                     is the one holding the socket — so the report attributes the
                     pid rather than asserting it.
    CANNOT DETERMINE something took the bytes and nothing here can see what it
                     did with them: no counters, counters from a pid that is
                     gone, counters that stopped refreshing, no readable
                     `forwarded` number for the verdict to be about, or a
                     health file that is a symlink and was not followed
                     (#1184).

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
import errno
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _proc

import _proc  # noqa: E402  (the one liveness probe, shared with gl-mrs / gh-prs)
import _untrusted  # noqa: E402  (the health file is somebody else's text, #1187)

#: Absent on Windows, where it is `0` and the open below carries no guard. Same
#: spelling as `transport.py`, deliberately: this is the same directory and the
#: same threat, and a second convention for it would be one more thing to keep
#: in step.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

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
    except ConnectionRefusedError:
        return "no-listener", f"{path} exists but refused the connection (ConnectionRefusedError)"
    except FileNotFoundError:
        # The path was there for the check above and gone by this line: a
        # consumer unlinked its socket in between. Same state — nothing is
        # listening — but a socket that vanished and a socket that answered and
        # said no send an operator to two different places, and naming the
        # wrong one is the misreport this op exists to stop making.
        return "no-listener", f"{path} vanished between the check and the connect"
    except OSError as err:
        return "unknown", f"{type(err).__name__} connecting to {path}"
    finally:
        try:
            s.close()
        except OSError:
            pass


def read_health(path: str) -> tuple[dict | None, str]:
    """The consumer's published counters, or None and why not.

    `O_NOFOLLOW`, for the same reason `transport.claim_pidfile` uses it and
    citing the same issue (#148): this name sits in a world-writable directory
    and is derived from a socket path a co-tenant can predict, so a symlink
    planted at it is opened, parsed, and — before #1187 — rendered. Any
    same-uid JSON file was readable that way (#1184).

    `lexists`, not `exists`: `exists` follows the link, so a symlink pointing
    at nothing was reported as *no health file at all* — the absence-read-as-
    presence shape, with a hostile act rendered as a consumer that predates
    the field.

    A refused symlink is its own answer and not folded into either neighbour.
    On Windows `_NOFOLLOW` is `0` and no guard is applied; nothing here claims
    otherwise, and `tests/test_watch_channel_health_hostile_file_1184_1187.py`
    skips rather than passing vacuously there.

    The parse arm catches `ValueError` rather than `json.JSONDecodeError`
    (#1191): the stream is decoded before it is parsed, so invalid UTF-8 raises
    `UnicodeDecodeError`, which is neither an `OSError` nor a
    `JSONDecodeError`. It escaped this function as a traceback — an op whose
    subject is declining beats guessing, answering with a stack trace, for two
    bytes any same-uid writer can put in `/tmp`.
    """
    health_path = path + HEALTH_SUFFIX
    if not os.path.lexists(health_path):
        return None, (
            "the bound consumer publishes no counters — it predates this field, or it is "
            "not claude-channel"
        )
    try:
        fd = os.open(health_path, os.O_RDONLY | _NOFOLLOW)
    except OSError as err:
        # ELOOP on Linux and macOS, EMLINK on the BSDs — both mean the name was
        # a symlink and O_NOFOLLOW refused it.
        if err.errno in (errno.ELOOP, errno.EMLINK):
            return None, (
                f"{health_path} is a symlink and was not followed — a health file is "
                "written in place by the consumer, so this is somebody redirecting the "
                "read at another file"
            )
        return None, f"{health_path} could not be read ({type(err).__name__})"
    # `O_NOFOLLOW` refuses a symlink; it does not refuse a *directory*, and
    # `os.open` on one succeeds. The wrap is then what fails
    # (`IsADirectoryError` on POSIX, `PermissionError` on Windows — #618/#627),
    # and it fails without taking ownership of the descriptor, so the fd leaks.
    # Plain `open()` never could: splitting the open from the wrap is what
    # introduced this, and a co-tenant who `mkdir`s the predictable name would
    # otherwise bleed one descriptor per poll out of the process reading it.
    try:
        handle = os.fdopen(fd, "r", encoding="utf-8")
    except OSError as err:
        os.close(fd)
        return None, f"{health_path} could not be read ({type(err).__name__})"
    try:
        with handle as f:
            record = json.load(f)
    except (OSError, ValueError) as err:
        # `ValueError`, not `json.JSONDecodeError`: the stream is decoded before
        # it is parsed, so two bytes of invalid UTF-8 raise `UnicodeDecodeError`
        # — a `ValueError`, and neither of the two this arm used to name. It
        # escaped as a traceback where the answer is `CANNOT DETERMINE`, which
        # is a same-uid denial of service on the op that reports the outage.
        # Both are `ValueError` subclasses and the base is caught deliberately:
        # a third decode failure must decline too, not crash the report.
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
    if _counter(record, "forwarded") is None:
        return (
            "its health file publishes no readable `forwarded` count, which is the "
            "number a FORWARDING verdict would be reporting"
        )
    return ""


def _counter(record: dict, key: str) -> int | None:
    """A published counter, or None when it is absent or not a number.

    Rendering a missing counter as `0` prints a number this op never read as one
    it did — and "0 forwarded" then means both a quiet morning and an unreadable
    file. That is the absence-read-as-presence defect this op exists to remove,
    rebuilt on its own headline.

    `bool` is excluded deliberately: `true` is an `int` in Python and would
    otherwise render as `1 forwarded`.
    """
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _num(value: int | None) -> str:
    return "?" if value is None else str(value)


def _stamp(record: dict, key: str, default: str = "?") -> str:
    """A string out of somebody else's JSON, kept to the one line this report
    gave it (#1187, and the state files too since #1191).

    Those files are written by separate processes this op cannot authenticate,
    in a directory anyone on the machine can write to, and their stamps were
    interpolated straight into the render: a `started` value carrying a
    newline, a `</channel>` and a directive at column 0 put all three in the
    op's own answer. `_untrusted.flat` is the boundary #819 established and the
    widening #851/#886 gave it — a second scheme here would have to be widened
    again next time.

    Not `fence()`: these are one-line stamps, and two marker lines around a
    timestamp is the noise that gets a convention ignored. The provenance is
    stated once per report by `_health_note`, which is the shape `_board` uses
    for the same reason.
    """
    value = record.get(key)
    if value is None or value == "":
        return default
    return _untrusted.flat(value if isinstance(value, str) else str(value))


def _health_note() -> str:
    """Said once, above the stamps, because a reader acts on what they read
    first. Built per call rather than at import: the wording depends on what
    `sys.stdout` says it can carry (#863)."""
    return "  " + _untrusted.flat_note(
        "the stamps", "the consumer's health file")


#: How many rows of each kind the listing prints before it says how many more
#: there were. A cap that silently truncates would be the defect this whole
#: function is about, so the remainder is always counted out loud.
_ROW_CAP = 10


class Stranded(NamedTuple):
    """One row of the stranded listing — a watcher, or a file that would not
    become one.

    `refusal` is `""` for a row read out of a state file and otherwise says why
    that file was not read. It is a field rather than an omission because the
    alternative — `continue` — is the shape #1191 was filed about: a listing
    whose whole job is to be complete, silently dropping exactly the rows
    somebody tampered with.
    """

    source: str
    watcher_id: str
    last: dict
    refusal: str


def _read_state_file(name: str) -> tuple[dict | None, str]:
    """One state file's JSON, or None and why not (#1191).

    The same read `read_health` performs thirty lines up, for the same reason
    and against the same threat — `STATE_DIR` is `/tmp`, and this name is worse
    than the health file's: it is not derived from anything, so the glob above
    accepts whatever a co-tenant chooses to create.

    **No existence pre-check, deliberately.** `read_health` needs `lexists`
    because it asks whether a health file is published at all; here the name
    came out of `os.listdir`, so it existed, and `O_NOFOLLOW` answers a dangling
    symlink with `ELOOP` rather than `ENOENT`. Adding an `exists` call would
    reintroduce the very bug #1184 removed — the link followed, the absence of
    its target reported as the absence of the file.

    `O_NOFOLLOW` refuses a symlink and does *not* refuse a directory: `os.open`
    succeeds on one and `os.fdopen` then raises without taking the descriptor.
    That leak was introduced by #1184's own fix and caught by its reviewer, and
    it is worse here, inside a loop over every name in the directory.
    """
    path = os.path.join(STATE_DIR, name)
    shown = _untrusted.flat(name)
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as err:
        # ELOOP on Linux and macOS, EMLINK on the BSDs — both mean the name was
        # a symlink and O_NOFOLLOW refused it.
        if err.errno in (errno.ELOOP, errno.EMLINK):
            return None, (
                f"{shown} is a symlink and was not followed — a state file is written "
                "in place by its own poller, so this is somebody redirecting the read "
                "at another file"
            )
        return None, f"{shown} could not be read ({type(err).__name__})"
    try:
        handle = os.fdopen(fd, "r", encoding="utf-8")
    except OSError as err:
        os.close(fd)
        return None, f"{shown} could not be read ({type(err).__name__})"
    try:
        with handle as f:
            state = json.load(f)
    except (OSError, ValueError) as err:
        # See `read_health`: `UnicodeDecodeError` is a `ValueError` and was
        # caught by neither arm this replaced, so invalid UTF-8 in any one file
        # in the directory took down the whole listing with a traceback.
        return None, f"{shown} could not be read ({type(err).__name__})"
    if not isinstance(state, dict):
        return None, f"{shown} is not a JSON object"
    return state, ""


def stranded_watchers(path: str) -> list[Stranded]:
    """Watchers whose last emit to `path` found nobody listening.

    The blast radius of a definite negative. "Nothing is listening" is a fact
    about the socket; "and these six pollers have been writing into it since
    09:14" is the one an operator can act on.

    A file that could not be read comes back as a row carrying a `refusal`
    rather than not coming back at all (#1191). The two candidates were "one
    bad file skips its own row" and "one bad file declines the whole listing",
    and the second is the worse trade: it would let anyone who can write to
    `/tmp` erase every other watcher from the report with a single `ln -s`.
    Skipping in place, and saying so on its own line, keeps the other rows and
    still never renders "I could not look" as "there was nothing to see".
    """
    rows: list[Stranded] = []
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
        state, refusal = _read_state_file(name)
        if state is None:
            rows.append(Stranded(source, watcher_id, {}, refusal))
            continue
        # A watcher bound to a different socket is not stranded on this one; it
        # is somebody else's business, and reporting it here would be the
        # partial-migration confusion #581 records, inverted. Skipped silently
        # and correctly: this is a fact the op established, not one it missed.
        if state.get("sock_path") not in (None, path):
            continue
        last = state.get("last_emit")
        if isinstance(last, dict) and last.get("state") == "no-listener":
            rows.append(Stranded(source, watcher_id, last, ""))
    return rows


def _render_stranded(path: str) -> list[str]:
    """The watcher listing, with its own provenance note when it renders text.

    `source` and the watcher id are parsed out of a *filename* and are as much
    somebody else's words as the `ts` inside the file — a POSIX name carries
    any byte but `/` and NUL, newline included — so all three go through
    `_untrusted` (#1191, same boundary as #1187).
    """
    rows = stranded_watchers(path)
    found = [row for row in rows if not row.refusal]
    refused = [row for row in rows if row.refusal]
    if not found and not refused:
        return ["  watchers : none recorded an emit into this socket"]

    if found:
        head = f"{len(found)} found nobody listening on their last emit"
    else:
        # Not "none recorded an emit": every file that would have said so was
        # unreadable, and reporting that as an empty list is the absence-read-
        # as-presence defect on the listing this op added for #554.
        head = "none of the readable state files recorded an emit into this socket"
    lines = [f"  watchers : {head}",
             "             " + _untrusted.flat_note(
                 "the watcher rows", "the pollers' own state files")]
    for row in found[:_ROW_CAP]:
        lines.append(
            f"             {_untrusted.flat(row.source)} "
            f"{_untrusted.flat(row.watcher_id)} — last emit {_stamp(row.last, 'ts')}")
    if len(found) > _ROW_CAP:
        lines.append(f"             ... and {len(found) - _ROW_CAP} more")
    if refused:
        subject = "its watcher is" if len(refused) == 1 else "their watchers are"
        noun = "state file was" if len(refused) == 1 else "state files were"
        lines.append(
            f"             {len(refused)} {noun} not read, so whether {subject} "
            "stranded is not known either way")
        for row in refused[:_ROW_CAP]:
            lines.append(f"             {row.refusal}")
        if len(refused) > _ROW_CAP:
            lines.append(f"             ... and {len(refused) - _ROW_CAP} more unread")
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
            _health_note(),
            f"  consumer : bound, but {objection}",
            f"             last published counters: {_num(_counter(record, 'forwarded'))} forwarded, "
            f"{_num(_counter(record, 'dropped'))} dropped, updated {_stamp(record, 'updated')}",
            "", CEILING,
        ])

    return RC_FORWARDING, "\n".join([
        "channel: FORWARDING", *head,
        _health_note(),
        f"  consumer : pid {record.get('pid')} (self-reported), up since "
        f"{_stamp(record, 'started')}",
        "             the health file names its own writer; pids are reusable, so",
        "             nothing here proves that process is the one holding the socket",
        f"  counters : {_num(_counter(record, 'lines_read'))} lines read, "
        f"{_num(_counter(record, 'forwarded'))} forwarded, "
        f"{_num(_counter(record, 'dropped'))} dropped",
        f"             last forwarded {_stamp(record, 'last_forwarded', 'never')}"
        f" (counters refreshed {_stamp(record, 'updated')})",
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
