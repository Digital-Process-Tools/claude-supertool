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
    CONTRADICTED     the process holding the socket is not the one the health
                     file names (#1192). A fourth state on purpose: this is a
                     finding, and `CANNOT DETERMINE` means no finding — putting
                     an impersonation in that bucket would be this op's own
                     defect. Peer credentials do not close the forgery, because
                     a same-uid process that binds the socket *and* writes the
                     file is its own peer; what they catch is the disagreement.

`CANNOT DETERMINE` is the point of the op rather than its failure mode. It is
the state today's tooling reports as green, and reporting it as green produced a
confidently wrong diagnosis in both directions on 2026-07-29 (#554's own
account) — first "transport is fine" off `sent ok`, then "the radar is dead" off
a drop line, while it had already recovered.

Exit codes are the states, on purpose: 0 forwarding, 1 not delivering, 3 cannot
determine, 4 contradicted. A single non-zero would put answers this op exists to
separate back into one bucket, and 4 is separate from 3 for the same reason —
"I could not tell" and "I can tell, and it is wrong" call for different actions.

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
import struct
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _proc

import _proc  # noqa: E402  (the one liveness probe, shared with gl-mrs / gh-prs)
import _untrusted  # noqa: E402  (the health file is somebody else's text, #1187)
import naming  # noqa: E402  (one name above the two path variables, #1477)

#: Absent on Windows, where it is `0` and the open below carries no guard. Same
#: spelling as `transport.py`, deliberately: this is the same directory and the
#: same threat, and a second convention for it would be one more thing to keep
#: in step.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: Both halves out of one resolver (#1477), so this file and `transport.py`
#: cannot drift: they used to read the same two variables in two places with two
#: copies of the `or` idiom. `RESOLVED.notes` carries every precedence decision
#: that was made getting here, and `_channel_lines` prints them — a name that
#: lost to a stale export must never do so quietly.
RESOLVED = naming.resolve()
SOCK_PATH = RESOLVED.sock
STATE_DIR = RESOLVED.state_dir

#: The consumer is not a supertool subprocess. `claude-channel` is declared in
#: `.mcp.json` and spawned by the harness, so the `.supertool.json`-to-env route
#: that carries a name to every poller does not reach it at all. That asymmetry
#: is the whole reason `consumer_lines` exists.
MCP_FILENAME = ".mcp.json"
CONSUMER_SERVER = "claude-channel"

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
#: A fourth, because a contradiction is not an absence of findings (#1192).
#: `CANNOT DETERMINE` says nothing was established; this says something was —
#: the health file was written by a process that is not holding the socket.
#: Folding it into 3 would be the defect this whole op exists to remove.
RC_CONTRADICTED = 4

#: Linux. `SO_PEERCRED` on a connected AF_UNIX socket yields `struct ucred` —
#: three native ints, pid first — for the process on the other end. Read from
#: the *client* side it is the process that called `listen`, which is exactly
#: the socket-holder this op wants to name.
_UCRED = "3i"

#: macOS. Python exposes neither constant, so both are the raw values from
#: `<sys/un.h>`: `SOL_LOCAL` is 0 and `LOCAL_PEERPID` is 2.
#:
#: **Not `LOCAL_PEERCRED`**, which #1192 named. That option returns a
#: `struct xucred` carrying a uid and *no pid*, and a uid check answers nothing
#: here: the threat is a same-uid process, which passes it trivially. The pid
#: is the only field that can disagree with the health file, so the pid is the
#: field this asks for.
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2

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


def peer_credentials_supported() -> bool:
    """Whether this platform can name the process holding an AF_UNIX socket.

    Measured from `sys.platform` rather than assumed, and false on more than
    Windows: FreeBSD has `LOCAL_PEERCRED` and no `LOCAL_PEERPID`, so it can
    report the holder's uid and never its pid. A uid is not an answer to the
    question this op asks — the threat is a same-uid process — so a platform
    that can only supply one is reported as unable, not as partially able.
    """
    if not hasattr(socket, "AF_UNIX"):
        return False
    if sys.platform.startswith("linux"):
        return hasattr(socket, "SO_PEERCRED")
    return sys.platform == "darwin"


def peer_pid(path: str) -> tuple[int | None, str]:
    """The PID of the process holding this socket, or None and why not (#1192).

    **What this buys, and what it does not.** It does not close the forgery:
    a same-uid process that binds the socket *and* writes the health file is
    its own peer, so the two agree and `FORWARDING` stands. What it catches is
    a **mismatch** — a health file naming a process that is not the one holding
    the socket, which previously drew no objection at all. The ceiling in
    `docs/presets/watch.md` therefore stays exactly where it was.

    Three states, like everything else on this op: a pid, a refusal because the
    connect failed, and a refusal because this platform has no way to ask. The
    last one is named rather than folded into the first, because a report that
    cannot tell "nobody answered" from "I cannot ask here" is the shape this
    file was written to remove.
    """
    if not peer_credentials_supported():
        return None, (
            f"peer credentials for an AF_UNIX socket are not available on "
            f"{sys.platform}, so the process holding it cannot be named from here"
        )
    shown = _untrusted.flat(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(CONNECT_TIMEOUT)
        s.connect(path)
        if sys.platform == "darwin":
            raw = s.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("i"))
            (pid,) = struct.unpack("i", raw)
        else:
            raw = s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                               struct.calcsize(_UCRED))
            pid, _uid, _gid = struct.unpack(_UCRED, raw)
    except OSError as err:
        return None, f"{type(err).__name__} asking who holds {shown}"
    except struct.error as err:
        # A kernel that answered with a shorter option than the struct this
        # unpacks. Not a pid, and not something to guess at.
        return None, f"the peer credentials for {shown} were unreadable ({err})"
    finally:
        try:
            s.close()
        except OSError:
            pass
    if pid <= 0:
        # Linux answers `0` for a socket whose peer is gone, and for one bound
        # in a namespace this process cannot see into. Neither is a process.
        return None, f"the kernel reported no pid for the process holding {shown}"
    return pid, ""


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
        #
        # This filter runs only on files that were read. `sock_path` lives
        # inside the file, so an unread one cannot be attributed either way and
        # its row stays — with the render saying that is unknown too, rather
        # than implying the file is this socket's.
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
        # The unread rows are deliberately NOT filtered by `sock_path`, and the
        # sentence says so: that field is inside the file, so a file that could
        # not be read cannot be attributed to this socket or to another one.
        # Dropping it would be guessing it was somebody else's; claiming it
        # would be guessing it was ours. Naming the doubt is the third state.
        one = len(refused) == 1
        noun = "state file was" if one else "state files were"
        whose = "it belongs" if one else "they belong"
        subject = "its watcher is" if one else "their watchers are"
        lines.append(
            f"             {len(refused)} {noun} not read, so neither which socket "
            f"{whose} to nor whether {subject} stranded is known")
        for row in refused[:_ROW_CAP]:
            lines.append(f"             {row.refusal}")
        if len(refused) > _ROW_CAP:
            lines.append(f"             ... and {len(refused) - _ROW_CAP} more unread")
    return lines


def _mcp_roots() -> list[Path]:
    """Where a `.mcp.json` declaring the consumer could be.

    Two, and both are checked rather than one being guessed at: the plugin root
    (this file is `<root>/presets/watch/channel.py`), which is what
    `${CLAUDE_PLUGIN_ROOT}` resolves to for an installed plugin, and the current
    directory, which is where a project-level `.mcp.json` lives. Reading one and
    reporting on it would be an answer about a file the harness may not be using.
    """
    return [Path(__file__).resolve().parents[2], Path.cwd()]


def _declared_env(mcp_path: Path) -> tuple[dict[str, str] | None, str]:
    """The consumer's declared environment from one `.mcp.json`, or why not.

    `{}` and `None` are different answers and the distinction is the point:
    `{}` means the file declares `claude-channel` and gives it no watch
    variables, which is a *positive* finding — the consumer will bind the
    default path. `None` means nothing was established here.
    """
    if not mcp_path.exists():
        return None, f"no {MCP_FILENAME} at {mcp_path}"
    try:
        doc = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return None, f"{mcp_path} could not be read ({type(err).__name__})"
    if not isinstance(doc, dict):
        return None, f"{mcp_path} is not a JSON object"
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict) or not isinstance(servers.get(CONSUMER_SERVER), dict):
        return None, f"{mcp_path} declares no {CONSUMER_SERVER} server"
    env = servers[CONSUMER_SERVER].get("env")
    if env is None:
        return {}, ""
    if not isinstance(env, dict):
        return None, f"{mcp_path} gives {CONSUMER_SERVER} an `env` that is not an object"
    return {str(k): str(v) for k, v in env.items()}, ""


def _declaration(env: dict[str, str]) -> str:
    """What a `.mcp.json` said, flattened. Operator text on a rendered surface
    (#1423), and read from a file rather than typed here."""
    bits = [f"{key}={_untrusted.flat(env[key])}"
            for key in (naming.NAME_ENV, naming.SOCK_ENV, naming.STATE_DIR_ENV)
            if env.get(key)]
    return ", ".join(bits) if bits else "no watch variables at all"


def consumer_lines(resolved: naming.Resolved,
                   roots: list[Path] | None = None) -> list[str]:
    """Whether the consumer is configured for the same channel as the producers.

    This is the deliverable of #1477 rather than its convenience. A name in
    `.supertool.json` reaches the pollers, `radar` and this op — three of four
    surfaces — and cannot reach `claude-channel`, which the harness spawns from
    `.mcp.json`. Configuring three of four *is* the half-configured state
    `presets/watch/README.md` says is worse than configuring nothing, arriving
    through a new door. So the name has two homes, and two homes that can
    disagree get a check.

    Three states, and silence is only ever the first of them:

      * they agree — no line, unless a name is in play, where one line saying
        which file was read is what makes a two-session setup legible;
      * they disagree — both resolved sockets, named, always;
      * nothing was established — said so, never rendered as agreement, and
        only when it could matter (a name, or a non-default socket). On a
        default channel with no `.mcp.json` anywhere there is nothing to warn
        about, and a warning printed every time is one nobody reads.
    """
    roots = _mcp_roots() if roots is None else roots
    agreed: list[str] = []
    differed: list[str] = []
    unread: list[str] = []
    seen: set[str] = set()
    for root in roots:
        mcp_path = Path(root) / MCP_FILENAME
        key = str(mcp_path)
        if key in seen:
            continue
        seen.add(key)
        env, why = _declared_env(mcp_path)
        if env is None:
            unread.append(why)
            continue
        theirs = naming.resolve(env)
        if theirs.sock == resolved.sock:
            agreed.append(f"consumer config {key} agrees: {_declaration(env)}")
        else:
            differed.append(
                f"consumer config {key} declares {_declaration(env)}, which binds "
                f"{theirs.sock} — this process reads {resolved.sock}. The consumer "
                f"is on another channel, so nothing a poller emits here reaches it")
    if differed:
        return differed + agreed
    if agreed:
        return agreed if resolved.name else []
    if resolved.name or resolved.sock != naming.DEFAULT_SOCK:
        return [f"consumer config NOT checked — {why}" for why in unread]
    return []


def _channel_lines(path: str, resolved: naming.Resolved) -> list[str]:
    """The name this channel is running under, and how that was decided.

    Only for the path this process resolved: `health()` takes an argument, and
    printing "name oss" beside a socket the caller passed in by hand would be a
    claim about a channel that is not the one being reported on.
    """
    if path != resolved.sock:
        return []
    body: list[str] = []
    if resolved.name:
        body.append(
            f"name {_untrusted.flat(resolved.name)} (from {naming.NAME_ENV}) — "
            f"poller slots in {resolved.state_dir}")
    if resolved.refusal:
        body.append(resolved.refusal)
    body.extend(resolved.notes)
    body.extend(consumer_lines(resolved))
    if not body:
        return []
    return [f"  channel  : {body[0]}"] + [f"             {line}" for line in body[1:]]


def _holder_lines(path: str) -> list[str]:
    """Who holds this socket, for the two arms that decline before the peer check.

    `peer_pid` shipped with #1192 and had exactly one caller: the point past
    `read_health` where a record already exists. The two arms that return before
    it — no readable health file, and a health file this op objects to — printed
    `CANNOT DETERMINE` without ever asking a question the platform can answer
    (#1476). That collapses two states with opposite remedies: **nothing is
    consuming this socket** means launch a consumer, and **another process is
    consuming it** means delivery works and this session is not the listener.

    The verdict does not move. Naming the holder is not evidence of delivery —
    `CEILING` says why, and it is still printed — so these arms stay
    `CANNOT DETERMINE`. What moves is that the reader is told which of the two
    they are in, or, in the third state, which probe was tried and what it
    returned. A bare `CANNOT DETERMINE` is the thing being fixed.
    """
    holder, why = peer_pid(path)
    if holder is None:
        return [f"             socket-holder NOT resolved — {why}"]
    if holder == os.getpid():
        return [
            f"             socket-holder: pid {holder} — this process. The report is "
            f"being run by",
            "             the process holding the socket, so no separate consumer was found",
        ]
    return [
        f"             socket-holder: pid {holder} — not this process (this process is "
        f"pid {os.getpid()})",
        "             something IS bound and reading is possible: this is `not my",
        "             listener`, not `no listener`. Those call for opposite actions,",
        "             and this arm used to render them identically",
    ]


def health(path: str) -> tuple[int, str]:
    """The whole report, and the exit code that encodes its state."""
    state, detail = probe_socket(path)
    head = [f"  socket   : {path}", f"             {detail}"]
    head += _channel_lines(path, RESOLVED)

    if state == "no-listener":
        body = ["channel: NOT DELIVERING", *head,
                "  consumer : none — every event emitted right now is lost at the source"]
        body += _render_stranded(path)
        return RC_NOT_DELIVERING, "\n".join([*body, "", CEILING])

    if state == "unknown":
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            "  consumer : the socket could not be probed, so nothing is known either way",
            # #1476 established that this arm must not *claim* a holder: `peer_pid`
            # connects, and the connect above is the one that just failed. #1495 is
            # that it did not say so — it omitted the line the other arms print,
            # and an omitted line reads exactly like a `no holder` line to anyone
            # scanning for one. Three states in the render, not two: the skip and
            # its reason, never a verdict.
            f"             socket-holder NOT asked — {detail}. The holder check is",
            "             the same connect, so there is nothing there to ask; this",
            "             is a declined probe, not an absent holder",
            "", CEILING,
        ])

    record, why = read_health(path)
    if record is None:
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            f"  consumer : bound, but {why}",
            *_holder_lines(path),
            "             bytes are accepted; what happens to them is not visible here",
            "", CEILING,
        ])

    objection = _health_objection(record)
    if objection:
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            _health_note(),
            f"  consumer : bound, but {objection}",
            *_holder_lines(path),
            f"             last published counters: {_num(_counter(record, 'forwarded'))} forwarded, "
            f"{_num(_counter(record, 'dropped'))} dropped, updated {_stamp(record, 'updated')}",
            "", CEILING,
        ])

    claimed = record.get("pid")
    holder, holder_why = peer_pid(path)
    if holder is not None and holder != claimed:
        # Not `CANNOT DETERMINE`: something *was* determined, and it is the one
        # thing the old report could never say (#1192). The counters are still
        # printed — they are what the impersonator published, and an operator
        # comparing them against the real consumer's needs to see them — but the
        # verdict they sit under is no longer a positive one.
        return RC_CONTRADICTED, "\n".join([
            "channel: CONTRADICTED", *head,
            _health_note(),
            f"  consumer : the health file names pid {claimed}, but pid {holder} is "
            f"the process holding this socket",
            "             these are the same process on a healthy channel. They are",
            "             not here, so the counters below were published by something",
            "             that is not the consumer — a live impersonation, or a health",
            "             file left behind beside a legitimate socket. Neither is a",
            "             degraded read: check both pids before trusting any of it.",
            f"  counters : {_num(_counter(record, 'lines_read'))} lines read, "
            f"{_num(_counter(record, 'forwarded'))} forwarded, "
            f"{_num(_counter(record, 'dropped'))} dropped",
            "", CEILING,
        ])

    if holder is not None:
        identity = [
            f"  consumer : pid {claimed}, up since {_stamp(record, 'started')}",
            "             socket-holder verified: the process holding this socket is",
            "             the one the health file names. That rules out a stale or",
            "             forged file beside a live consumer; it does not rule out a",
            "             same-uid process that bound the socket and wrote the file,",
            "             which is its own peer and agrees with itself.",
        ]
    else:
        identity = [
            f"  consumer : pid {claimed} (self-reported), up since "
            f"{_stamp(record, 'started')}",
            f"             socket-holder NOT checked — {holder_why}",
            "             the health file names its own writer; pids are reusable, so",
            "             nothing here proves that process is the one holding the socket",
        ]

    return RC_FORWARDING, "\n".join([
        "channel: FORWARDING", *head,
        _health_note(),
        *identity,
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
