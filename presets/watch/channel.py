"""`channel:health` — is the watch -> Claude-session bridge actually delivering?

The question #554 asks, and the reason it needs its own op: `pgrep -fl channel.ts`,
`lsof /tmp/supertool-watch.sock` and writing to that socket are all green whether
events reach the session or not. Three probes, one answer, no information.

**Delivery into a Claude session is not observable from outside it, and this op
does not pretend otherwise.** `channel.ts` reaches the session through
`mcp.notification()` — a JSON-RPC notification, so no id, no response and
nothing to await — and it never writes back to the producer connection either.
No ack exists to read. That is a finding, not a gap in this implementation, and
it is why the answer here has five states rather than two:

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
    BOUND, NOT SUBSCRIBED
                     a consumer is bound, verified and counting, and no session
                     is subscribed to its channel (#1543), so every event it
                     reads is handed to a transport nobody is listening on. A
                     fifth state for the same reason as the fourth: it is a
                     finding. Subscription is only partly observable from
                     outside the session and `subscription()` claims exactly the
                     part that is — the process that spawned the socket-holder,
                     and whether the channel tag in its argv names a *configured*
                     MCP server. Everything it could not ask is `CANNOT
                     DETERMINE` with the reason, never this and never the one
                     above it.

`CANNOT DETERMINE` is the point of the op rather than its failure mode. It is
the state today's tooling reports as green, and reporting it as green produced a
confidently wrong diagnosis in both directions on 2026-07-29 (#554's own
account) — first "transport is fine" off `sent ok`, then "the radar is dead" off
a drop line, while it had already recovered.

Exit codes are the states, on purpose: 0 forwarding, 1 not delivering, 3 cannot
determine, 4 contradicted, 5 bound but not subscribed. A single non-zero would
put answers this op exists to separate back into one bucket, and 4 and 5 are
separate from 3 for the same reason — "I could not tell" and "I can tell, and it
is wrong" call for different actions.

**Measured caveat, so nobody builds on a code that is not there.** The supertool
wrapper reports any non-zero op as `FAIL` and exits 1, so 3 survives only when
this file is run directly (`python3 presets/watch/channel.py health`). Through
`supertool 'channel:health'` the states are carried by the *first line* of the
report — `channel: FORWARDING` / `NOT DELIVERING` / `CANNOT DETERMINE` /
`CONTRADICTED` / `BOUND, NOT SUBSCRIBED` — which is what the tests key on and
what a caller should key on too.

**Measured cost, so nobody is surprised by it.** The subscription probe spawns
`claude mcp get` once per `server:` tag — 1.1-2.0s each over six samples on
2026-08-13, across a live server, a configured-but-dead one and an unknown
name. So this op is not instant, and `radar` pays it once per run when its
board has counted an accepted emit. The flag is variadic, so the tag count is
somebody else's argv rather than a constant: every lookup in one call shares
`MCP_LOOKUP_BUDGET`, and a tag the budget did not reach is reported unasked
rather than unconfigured (#1558). That spawn is also why this op is declared
`acts` and not `read-only` — probing a tag starts whatever the harness has
configured under a name this tool read out of another process.

**`probe` is the second sub-op and the second question (#1593).** Everything
above reads counters somebody else's traffic wrote, so with no traffic of its
own `health` cannot say whether the read-and-forward path is working *now*: a
consumer wedged on its read loop publishes exactly the numbers of an idle one.
`probe` writes one synthetic event — reserved `source`, no watcher state file —
and reports which of the consumer's own counters moved, in the same vocabulary,
with two states of its own for the two findings `health` has no traffic to
reach: `ACCEPTED, NOT FORWARDED` (6) and `ACCEPTED, DISCARDED` (7).

**And it declines the thing the caller wants, on purpose.** `FORWARDED` is not
arrival and the report never spells it as one; what it does instead is name the
exact `<channel watcher_source="channel-probe" ...>` tag the caller should now
look for, say that the increment is not attributable to this event, and say
which half of the bridge is left to suspect if the tag never appears. A success
line reading like receipt confirmation would rebuild #554 inside the fix for
#1593.
"""
from __future__ import annotations

import calendar
import errno
import json
import os
import re
import shlex
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

# Same pair, same order and same reason as `transport.py` (#1624): `naming` is
# a sibling of this file and nothing outside it is obliged to have put this
# directory on the path.
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _proc
sys.path.insert(0, str(Path(__file__).parent))  # for naming, our own sibling

import _proc  # noqa: E402  (the one liveness probe, shared with gl-mrs / gh-prs)
import _untrusted  # noqa: E402  (the health file is somebody else's text, #1187)
import naming  # noqa: E402  (one name above the two path variables, #1477)
import sourcepath  # noqa: E402  (where sources may live, one resolver, #2135)
import transport  # noqa: E402  (the wire shape a probe emits, #1593)

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

#: The variables an `.mcp.json` `env` block can use to put the consumer on a
#: channel. Declaring none of them is not the same as declaring the default: it
#: means the consumer takes whatever the session it was spawned from exported
#: (#1541).
CHANNEL_VARS = (naming.NAME_ENV, naming.SOCK_ENV, naming.STATE_DIR_ENV)

#: Where a consumer publishes its own counters. Derived from the socket path
#: rather than fixed, so two sessions running on separate `SUPERTOOL_WATCH_SOCK`
#: paths (the documented multi-session arrangement) get separate health files
#: instead of overwriting each other's — which would be this issue's defect
#: rebuilt inside its own fix.
HEALTH_SUFFIX = ".health.json"

#: Where the losing side of #550's collision leaves a record on its way out
#: (#2133). `channel.ts`'s `refuse()` already knows exactly why it lost this
#: socket -- its stderr message says so -- but that text used to go nowhere a
#: reader could reach: the harness had already marked the session's channel
#: servers `CONNECTION_CLOSED` before anyone opened a terminal. This is that
#: same fact, persisted beside the socket instead of only spoken to stderr.
#: The bound consumer clears it the instant it (re)binds, so what remains here
#: happened during *this* consumer's own run, not some earlier session's.
REFUSAL_SUFFIX = ".refused.json"

#: The `why` `read_refusal` hands back when nothing is wrong -- literally
#: nothing: no rival has ever recorded losing this socket. Every call site
#: compares against this by name rather than treating any `record is None` as
#: that one answer, because the same return shape covers a second, very
#: different case: the marker exists and could not be *read* -- a same-uid
#: symlink at the predictable name, the exact attack #148/#1184/#1187 already
#: guard the health file against, or ordinary corruption. Folding the two
#: together would let either one hide the collision this file exists to
#: surface, by making the evidence unreadable rather than absent.
_REFUSAL_ABSENT = "no rival consumer has recorded losing this socket"

#: Where a session's own self-report of `channel:received:N` lives, beside
#: the socket like every other sidecar here (#2150). #2051's remedy 1: every
#: number this subsystem publishes today is the forwarder describing its own
#: outbox -- `forwarded`, `dropped`, `lines_read` -- and none of them can see
#: the inbox. This is the one file written from the receiving side.
RECEIVED_SUFFIX = ".received.json"

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
#: A fifth, and the same argument again (#1543). A consumer that is bound,
#: verified and counting, with no session subscribed to its channel, is a
#: finding: every event it reads is handed to a transport nobody is listening
#: on. `CANNOT DETERMINE` would say nothing was established, and 0 would say
#: the opposite of the truth.
RC_NOT_SUBSCRIBED = 5

#: `probe` only. The consumer read the synthetic line off the wire — its own
#: `lines_read` moved — and neither forwarded nor discarded it. A finding, and
#: separate from `CANNOT DETERMINE` for the reason 4 and 5 are: something was
#: established. Separate from 5 too, and not a renaming of it: a probe never
#: asks the subscription question, so a report using that code would carry a
#: claim nothing in this path measured.
RC_PROBE_NOT_FORWARDED = 6
#: `probe` only, and a different place to go looking. `dropped` moved instead,
#: so the consumer read the event and refused it — the burst budget, a routing
#: key over the attribute cap, a handler that threw. Folding it into 6 would
#: report "it went nowhere" for an event whose disposal is on the record.
RC_PROBE_DISCARDED = 7

#: The subscription question, in the same three states as everything else here.
SUB_SUBSCRIBED = "subscribed"
SUB_NOT_SUBSCRIBED = "not-subscribed"
SUB_UNKNOWN = "unknown"

#: A channel's events reach a session only when that session was started with
#: this flag, and the tag it carries names a server the harness has
#: **configured**. Both halves were measured against claude 2.1.219 in #1544:
#: without the flag the consumer runs and delivers nothing, and with the flag
#: naming a `--mcp-config` server the harness answers
#: `server:NAME - no MCP server configured with that name` and the consumer
#: still runs and still delivers nothing. That second state is #1543.
CHANNEL_FLAG = "--dangerously-load-development-channels"
TAG_PREFIX = "server:"

#: `claude mcp get NAME` is the harness's own answer to "is NAME configured" —
#: the same question `bin/oss-workspace` asks before registering the
#: consumer, so this op and the launcher cannot disagree about it. It is read
#: for its EXIT CODE; the prose consulted is the refusal that distinguishes
#: "no such server" from "the lookup failed", and the rejection on a successful
#: lookup's own `Status:` line (#2208, `CLAUDE_REJECTED_STATUS_RE`). Anything
#: else is the third state.
#:
#: **The health-check it performs as a side effect is deliberately discarded,
#: and #1558 asked for the opposite.** The lookup spawns a second instance of
#: the named server to check it, and this repo's consumer refuses to start a
#: second one rather than unlinking a live incumbent (#550). Measured
#: 2026-08-13: `claude mcp get supertool-channel` printed `Status: ✘ Failed to
#: connect` under exit 0 while that same server was holding the socket and
#: forwarding 8 of 8 events. So for the only consumer this op reports on,
#: `Failed to connect` is what *healthy* looks like, and reading that line would
#: turn a correct FORWARDING into a false negative. The exit code answers the
#: question actually being asked — will the harness accept this tag, or refuse
#: it at startup (#1543) — and nothing here claims more than that.
CLAUDE_BIN = "claude"
CLAUDE_UNKNOWN_SERVER = "No MCP server named"

#: A `Status:` line saying the harness did not LOAD this server (#2208).
#:
#: The paragraph above is right about connection status and this does not touch
#: it: `Status: X Failed to connect` stays a positive answer, because for our
#: own consumer that is what healthy looks like. This is a different claim on
#: the same line. `claude mcp get` exits **0** for a server declared in
#: `.mcp.json` that `disabledMcpjsonServers` has switched off, printing
#: `Status: X Rejected (see disabledMcpjsonServers in settings)` -- so the exit
#: code alone reads a server the harness threw away as one it has, which is the
#: exact opposite of the question `subscription()`'s `standing` gate asks.
#:
#: Anchored on the `Status:` line rather than searched for as a substring: the
#: rest of that output is somebody else's config -- a server name, a command
#: path -- and a bare `Rejected` anywhere in it would answer this question out
#: of text its own author chose.
CLAUDE_REJECTED_STATUS_RE = re.compile(
    r"^[ \t]*Status:.*\bRejected\b", re.MULTILINE)

#: Per lookup, and further capped by what is left of `MCP_LOOKUP_BUDGET`. A
#: healthy lookup measured ~1s on 2026-08-13; 15s was the old value and could
#: not fit inside the op's own timeout even once (#1558).
CLAUDE_TIMEOUT = 8

#: `ps` reads a process's parent and argv. Cheap, and absent on Windows — where
#: the spawn raises `FileNotFoundError` and lands in the third state by name
#: rather than escaping as a traceback.
PS_TIMEOUT = 5

#: Every `claude mcp get` in ONE `subscription()` call shares this, because the
#: channel flag is variadic and a per-tag timeout is unbounded in the number of
#: tags — which is somebody else's argv. #1558: the op was declared at 15s while
#: the probe could spend `PS_TIMEOUT * 2 + CLAUDE_TIMEOUT * N`, so the op timeout
#: always won and the reader got supertool's bare `TIMEOUT` with an empty body
#: instead of the `CANNOT DETERMINE` this probe exists to produce. A probe that
#: cannot answer inside its own budget is the defect; the number was the symptom.
#:
#: `SUBSCRIPTION_WORST_CASE` is what `presets/watch.json` must leave room for,
#: and `tests/test_watch_channel_probe_shape_and_budget_1558_1559.py` pins the
#: arithmetic rather than the number, so moving either one moves both.
MCP_LOOKUP_BUDGET = 12.0
SUBSCRIPTION_WORST_CASE = PS_TIMEOUT * 2 + MCP_LOOKUP_BUDGET

#: How long `probe` waits for the consumer's published counters to move.
#:
#: The real consumer increments in the same tick it reads (`forwarded++` then
#: `publishHealth()` in `channel.ts`), and its write is floored at
#: `HEALTH_MIN_INTERVAL_MS = 250`. So the honest observation window is a
#: quarter-second plus whatever the machine is doing, and 3s is that with an
#: order of magnitude of slack — long enough that "did not move" is worth
#: printing, short enough that nobody stops running the op.
#:
#: **The number bounds the claim, not just the wall clock.** A consumer slower
#: than this looks exactly like one that did nothing, so every arm that reports
#: a non-advance prints the budget it waited, and the arm where nothing was
#: even *read* is `CANNOT DETERMINE` rather than a finding.
PROBE_WAIT_SECS = 3.0
PROBE_POLL_SECS = 0.1

#: What `presets/watch.json` must leave room for, on the other sub-op. Two
#: connects at `CONNECT_TIMEOUT` — the emit, and `peer_pid` — plus the wait.
#: The arithmetic is pinned rather than the number, the lesson of #1558: an op
#: whose probe cannot finish inside its own declared timeout can never print
#: the honest verdict it was written to produce, because the wrapper kills it
#: and the reader gets a bare `TIMEOUT` with an empty body.
PROBE_WORST_CASE = CONNECT_TIMEOUT * 2 + PROBE_WAIT_SECS

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

#: `probe`'s own ceiling, and the reason the op exists rather than a footnote
#: to it (#1593).
#:
#: Three separate refusals, and dropping any one of them turns a measurement
#: into a claim: the last leg is unobservable from here; the caller has to make
#: the other half of the observation themselves, so the tag is named for them;
#: and the counter that moved cannot be attributed to *this* event, because a
#: poller emitting in the same window advances the same number.
#:
#: The word this must never reach is the one a reader is hoping for. A success
#: line that reads like receipt confirmation would rebuild #554 inside the fix
#: for #1593 — which is why `tests/test_watch_channel_probe_1593.py` asserts
#: against the vocabulary and not only against the verdict.
PROBE_CEILING = (
    "`forwarded` counts events the consumer handed to the MCP transport. It is\n"
    "not a receipt. Whether this event appeared in a Claude session is\n"
    "observable only from inside that session — the bridge sends a JSON-RPC\n"
    "notification, which has no response to wait on — so no process outside it\n"
    "can see the last leg, this one included.\n"
    "The other half of the answer is yours, and it is the half no process here\n"
    "can reach: the `expect` line above says whether a tag should now appear in\n"
    "a session, and only a session can see whether it did. If it does not\n"
    "appear under a report that says it should, the producer half is exonerated\n"
    "and what is left is the subscription and the session — `channel:health`\n"
    "reports on the first of those (BOUND, NOT SUBSCRIBED).\n"
    "Nor is the increment attributable. A poller emitting in the same window\n"
    "advances the same counter and this op cannot tell the two apart. What is\n"
    "established is that the read-and-forward path moved at least one event in\n"
    "the window this emit opened."
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
    return _read_json_sidecar(
        path + HEALTH_SUFFIX,
        absent=(
            "the bound consumer publishes no counters — it predates this field, or it is "
            "not claude-channel"
        ))


def _read_json_sidecar(sidecar_path: str, *, absent: str) -> tuple[dict | None, str]:
    """Read one JSON object a consumer wrote beside its own socket.

    Shared by `read_health` and `read_refusal` (#2133): same world-writable
    directory, same same-uid symlink risk (#148, #1184/#1187), same defensive
    open. `absent` is the caller's reason for "nothing at this path" — the two
    sidecars mean different things by that: no counters published yet, versus
    no rival has ever recorded losing this socket.
    """
    if not os.path.lexists(sidecar_path):
        return None, absent
    try:
        fd = os.open(sidecar_path, os.O_RDONLY | _NOFOLLOW)
    except OSError as err:
        # ELOOP on Linux and macOS, EMLINK on the BSDs — both mean the name was
        # a symlink and O_NOFOLLOW refused it.
        if err.errno in (errno.ELOOP, errno.EMLINK):
            return None, (
                f"{sidecar_path} is a symlink and was not followed — this file is "
                "written in place by the consumer, so this is somebody redirecting the "
                "read at another file"
            )
        return None, f"{sidecar_path} could not be read ({type(err).__name__})"
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
        return None, f"{sidecar_path} could not be read ({type(err).__name__})"
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
        return None, f"{sidecar_path} could not be read ({type(err).__name__})"
    if not isinstance(record, dict):
        return None, f"{sidecar_path} is not a JSON object"
    return record, ""


def read_refusal(path: str) -> tuple[dict | None, str]:
    """A rival consumer's own record of losing this socket, or None and why.

    Written by the process that hit `EADDRINUSE`, confirmed a live listener
    (#550) and exited rather than take it — exactly the refusal #2133 measured
    going only to stderr, unread by a session already past `CONNECTION_CLOSED`
    for both of its channel declarations. This is the same fact, read from the
    marker that process leaves beside the socket on its way out.

    The bound consumer clears this file the moment it (re)binds (see
    `channel.ts`), so a record here happened during *this* consumer's own
    run — not a stale leftover from a collision that has since gone away.
    """
    return _read_json_sidecar(path + REFUSAL_SUFFIX, absent=_REFUSAL_ABSENT)


#: `read_received_receipt`'s `absent` when nobody has ever reported a count
#: from the receiving side for this socket. Named so a caller elsewhere can
#: recognise "no receipt at all" without re-deriving the sentence, the same
#: reason `_REFUSAL_ABSENT` has its own name.
_RECEIVED_ABSENT = "no receipt has been recorded for this socket yet"


def read_received_receipt(path: str) -> tuple[dict | None, str]:
    """This socket's last `channel:received` self-report, or None and why not.

    Same guarded read as `read_health`/`read_refusal`: this sidecar sits in
    the same world-writable directory, under a name derived from the socket
    path the same way, so it carries the same same-uid symlink risk (#148).
    """
    return _read_json_sidecar(path + RECEIVED_SUFFIX, absent=_RECEIVED_ABSENT)


def record_received(path: str, count: int) -> tuple[int, str]:
    """A session's own receipt: `count` channel events it has received so far.

    #2150 -- #2051's remedy 1. Every counter this subsystem publishes today
    is the forwarder describing its own outbox; none of them can see the
    inbox, so `forwarded` and `delivered` are one word wherever it matters.
    This is the receiving side's own report, compared against `forwarded`'s
    advance over the same window.

    Three states, and the third is load-bearing more than usual here: a
    session that never calls this and a session whose counts genuinely agree
    must not read alike.

        AGREE     (0) -- the delta this call reports since the prior receipt
                  equals `forwarded`'s delta over the same window.
        DISAGREE  (1) -- the two deltas differ; named with sign and size.
        UNSETTLED (3) -- no window could be diffed at all: `forwarded` is not
                  readable right now, there is no prior receipt for this
                  socket (the first call, with nothing yet to diff against),
                  the prior receipt itself does not hold a readable pair to
                  diff from, or `forwarded` went backwards since the prior
                  receipt (the consumer restarted, so the window this call
                  would diff spans a boundary nothing forwarded across
                  survives). Must never render as AGREE.

    Every call persists a fresh receipt (best-effort) so the *next* call has
    a baseline, regardless of which of the three states this one reports —
    the same reason `channel.ts` rewrites its heartbeat whether or not
    traffic is flowing.
    """
    prior, _prior_refusal = read_received_receipt(path)
    record, health_refusal = read_health(path)
    now_forwarded = _counter(record, "forwarded") if record else None
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_err = transport.write_json_contained(
        path + RECEIVED_SUFFIX,
        {"received": count, "forwarded_at_report": now_forwarded, "ts": stamp})
    lines = [f"channel: received report for {path}",
             f"  this session reports {count} received so far"]
    if write_err:
        lines.append(f"  WARNING -- could not persist this report ({write_err}); "
                      f"the next call will have no baseline from it")
    if now_forwarded is None:
        reason = health_refusal or "its health file publishes no readable `forwarded` count"
        lines.append(f"  UNSETTLED -- forwarded is not readable right now ({reason}); "
                      f"nothing to compare this report against")
        return 3, "\n".join(lines)
    if prior is None:
        lines.append(f"  UNSETTLED -- no prior receipt for this socket, so this is "
                      f"the first report; forwarded stands at {now_forwarded}. Call "
                      f"again later to compare drift over a window.")
        return 3, "\n".join(lines)
    prior_received = prior.get("received")
    prior_forwarded = prior.get("forwarded_at_report")
    if isinstance(prior_received, bool) or not isinstance(prior_received, int):
        lines.append("  UNSETTLED -- the prior receipt's `received` count is not "
                      "readable, so no window can be diffed")
        return 3, "\n".join(lines)
    if isinstance(prior_forwarded, bool) or not isinstance(prior_forwarded, int):
        lines.append("  UNSETTLED -- the prior receipt recorded no readable "
                      "forwarded baseline, so no window can be diffed")
        return 3, "\n".join(lines)
    delta_forwarded = now_forwarded - prior_forwarded
    if delta_forwarded < 0:
        # `forwarded` only ever counts up in a live consumer's own process
        # (see `channel.ts`'s heartbeat). A smaller value than the prior
        # receipt means the consumer that wrote it is gone and a new one has
        # taken the socket -- the window this call would diff spans a
        # restart, and nothing forwarded on the old process's watch survives
        # to be compared against. Reporting AGREE or DISAGREE off a window
        # that never happened would be worse than declining it.
        lines.append(f"  UNSETTLED -- forwarded went backwards ({prior_forwarded} -> "
                      f"{now_forwarded}), which means the consumer restarted between "
                      f"reports; nothing forwarded across that boundary can be "
                      f"compared. This report is the new baseline.")
        return 3, "\n".join(lines)
    delta_received = count - prior_received
    lines.append(f"  {delta_received} received since the last report, "
                 f"{delta_forwarded} forwarded over the same window")
    if delta_received == delta_forwarded:
        lines.append("  AGREE")
        return 0, "\n".join(lines)
    lines.append(f"  DISAGREE by {delta_forwarded - delta_received}")
    return 1, "\n".join(lines)


def _health_objection(record: dict, *, allow_stale: bool = False) -> str:
    """Why these counters are not evidence, or "" when they are.

    The arms below are the same mistake one step apart: believing a number
    because it is present. A file left behind by a consumer that died is a
    frozen `forwarded` count that never decreases and reads as health forever.

    `allow_stale` waives **only** the staleness arm, and only `probe` passes it
    (#1593), for its *baseline* read. `health` has nothing but the file, so a
    cold stamp ends its inquiry and must. `probe` is about to generate the
    traffic, and for it a cold stamp is the question rather than the answer:
    what settles the verdict is whether the file comes back — fresh, and
    advanced — after the emit, and the after-read is made without this waiver
    precisely so that tolerating a cold baseline never becomes believing one.

    Measured, which is why the waiver exists at all: on 2026-08-15 the consumer
    over this clone was holding the socket with counters 607s cold, and a probe
    that declined on the baseline reported `CANNOT DETERMINE` — while the event
    it had just written arrived in a maintainer's session as a rendered
    `<channel>` tag. The path was working. Aborting on a cold baseline threw
    away the strongest evidence this op can produce, in the one case that
    produces it.
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
    if age > STALE_AFTER_SECS and not allow_stale:
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
    # The directory-level answer is `_render_stranded`'s to report — that is
    # where the sentence about the population is written — so this only needs the
    # names, and it needs them from the one classifier rather than a second local
    # `except OSError` that could disagree with it (#1502).
    names, _dir_state, _dir_why = naming.state_dir_listing(STATE_DIR)
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
    # #1191 gave every unreadable *file* its own line and left the directory
    # itself with two states: `stranded_watchers` returns `[]` both for a
    # directory that held no state file and for one it could not list at all. On
    # a freshly named channel the second is the normal case — only a spawn
    # creates the derived directory — and this arm is exactly where such a
    # channel lands, so the strongest false claim on the report was also the
    # likeliest (#1502). Classified before the rows are read, because the
    # sentence below is a claim about the population and not about any file.
    _names, dir_state, dir_why = naming.state_dir_listing(STATE_DIR)
    declined = naming.state_dir_absence_note(STATE_DIR, dir_state, dir_why)
    rows = stranded_watchers(path)
    found = [row for row in rows if not row.refusal]
    refused = [row for row in rows if row.refusal]
    if not found and not refused:
        if declined:
            return [f"  watchers : not established — {declined}"]
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
    surfaces — and reaches `claude-channel` through none of them: the harness
    spawns it, from `.mcp.json`. Configuring three of four *is* the
    half-configured state `presets/watch/README.md` says is worse than
    configuring nothing, arriving through a new door.

    **The fourth surface has two routes, and only one of them is checkable
    here (#1541).** An `.mcp.json` `env` block is a declaration this op can read
    and compare. An environment variable exported by whatever launched the
    session — what `bin/oss-workspace` does, and the only route that does
    not write a single checkout's private name into an artifact every user
    installs — is not: the consumer inherits the session's environment, and this
    process only has its own, which carries whatever supertool injected from
    `.supertool.json` and says nothing about how the harness was started.

    Four states, and silence is only ever the first of them:

      * they agree — no line, unless a name is in play, where one line saying
        which file was read is what makes a two-session setup legible;
      * they disagree — both resolved sockets, named, always;
      * the config names no channel variable at all — the consumer inherits,
        which is unknown from here and is said as unknown. Before #1541 this
        arm was folded into "they disagree", on the reasoning that an
        undeclared consumer is a default one. That reasoning died with the
        `env` block: it now prints a claim about an environment nobody here
        has read, and it printed it directly under a `FORWARDING` verdict that
        contradicted it;
      * nothing was established — said so, never rendered as agreement.

    The last two are reported only when they could matter (a name, or a
    non-default socket). On a default channel with no `.mcp.json` anywhere there
    is nothing to warn about, and a warning printed every time is one nobody
    reads.
    """
    roots = _mcp_roots() if roots is None else roots
    agreed: list[str] = []
    differed: list[str] = []
    inherits: list[str] = []
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
        if not any(var in env for var in CHANNEL_VARS):
            # The shipped `.mcp.json` declares no channel at all, and since #1541
            # that is the intended state: a name true of one checkout must not
            # ride the artifact into every install. The consumer then takes these
            # variables from the environment of the session that spawned it —
            # which this process cannot read. Its own copy carries whatever
            # supertool injected from `.supertool.json`, and that says nothing
            # about how the harness was launched. So this is the third state, not
            # a disagreement: the old wording claimed the consumer was on the
            # default socket, which after #1541 is a statement about an
            # environment nobody here has seen, printed two lines under a
            # FORWARDING verdict that contradicts it.
            inherits.append(
                f"consumer config {key} names no channel variable, so the "
                f"consumer inherits it from the session that spawned it")
            inherits.append(
                f"that environment is not readable from here: "
                f"`bin/oss-workspace` exports {naming.NAME_ENV}, and a "
                f"session started any other way leaves the consumer on "
                f"{naming.DEFAULT_SOCK} while this process reads "
                f"{naming.flat_path(resolved.sock)}")
            continue
        theirs = naming.resolve(env)
        if theirs.sock == resolved.sock:
            agreed.append(f"consumer config {key} agrees: {_declaration(env)}")
        else:
            differed.append(
                f"consumer config {key} declares {_declaration(env)}, which binds "
                f"{naming.flat_path(theirs.sock)} — this process reads "
                f"{naming.flat_path(resolved.sock)}. The consumer is on another "
                f"channel, so nothing a poller emits here reaches it")
    if differed:
        return differed + agreed
    if agreed:
        return agreed if resolved.name else []
    if resolved.name or resolved.sock != naming.DEFAULT_SOCK:
        return inherits + [f"consumer config NOT checked — {why}" for why in unread]
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
            f"poller slots in {naming.flat_path(resolved.state_dir)}")
    if resolved.refusal:
        body.append(resolved.refusal)
    body.extend(resolved.notes)
    # Whose name this is (#1732). `channel:health` builds its own body rather
    # than going through `transport.channel_disclosure` — it indents every line
    # into the report's column — so the attribution has to be asked for here too,
    # or the one surface an operator opens when they suspect a shared channel is
    # the one that does not say.
    body.extend(naming.project_notes(resolved, naming.declared_names()))
    # The fifth op (#2135). `channel:health` is the surface an operator opens
    # when a watcher they configured is not there, and a search path declared
    # for some ops and not others is one of the two reasons it would not be.
    body.extend(sourcepath.op_lines("channel"))
    body.extend(consumer_lines(resolved))
    if not body:
        return []
    return [f"  channel  : {body[0]}"] + [f"             {line}" for line in body[1:]]


def _refusal_lines(path: str) -> list[str]:
    """What a rival consumer said when it lost this socket, or nothing (#2133).

    Established from the losing process's own stderr, quoted verbatim into a
    file it writes on the way out — see `read_refusal`. Nothing here decides
    whether the report's own verdict is good or bad on account of it;
    `subscription()` is where that decision lives. This only makes the fact
    readable, which #2133 measured it was not: the refusal happened, the
    session's channel servers were `CONNECTION_CLOSED`, and nobody — man or
    op — ever read the message that explained why.

    Silent when there is nothing to say. A report that grew a `refused` line
    on every call, evidence or not, would be exactly the noise this repo's own
    style guide forbids.
    """
    record, why = read_refusal(path)
    if record is None:
        if why == _REFUSAL_ABSENT:
            return []
        return [
            "  refused  : a refusal marker exists for this socket but could not be "
            "read —",
            f"             {_untrusted.flat(why)}. Same defensive read `read_health` "
            "uses for the",
            "             health file (#148, #1184/#1187) — this declines rather than",
            "             guessing, so a same-uid symlink at this predictable name "
            "cannot hide",
            "             behind a report that reads exactly like no collision at "
            "all (#2133)",
        ]
    reason = record.get("reason")
    reason_text = (_untrusted.flat(reason) if isinstance(reason, str) and reason
                    else "unknown reason")
    pid = record.get("pid")
    pid_text = str(pid) if isinstance(pid, int) else "?"
    ts_text = _stamp(record, "ts")
    return [
        f"  refused  : pid {pid_text} lost this socket at {ts_text} — {reason_text}",
        "             a second claude-channel server was configured for this",
        "             session and exited without binding it. If this session also",
        "             carries a channel MCP declaration outside the one that bound",
        "             this socket, that collision recurs every time it launches (#2133)",
    ]


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


class Subscription(NamedTuple):
    """One subscription answer: the state, and the report lines that carry it.

    `lines` is already indented for the report and already flattened — an argv
    is somebody else's text, read out of a process table anyone on this machine
    can write into (#1423).
    """

    state: str
    lines: list[str]


def _sub(state: str, head: str, rest: tuple[str, ...] = ()) -> Subscription:
    return Subscription(
        state, [f"  session  : {head}"] + [f"             {line}" for line in rest])


def _ps_fields(pid: int) -> tuple[int | None, str, str]:
    """`(ppid, argv, refusal)` for one pid — never `(0, "")` for "did not look".

    `-ww` rather than the default width: macOS truncates the command column to
    the terminal width when stdout is not a tty, which silently cuts the flag
    this whole probe is looking for off the end of a session's argv. Measured
    2026-08-13 — a plain `ps -ax -o command=` redirected to a file lost it.
    """
    try:
        done = subprocess.run(
            ["ps", "-ww", "-o", "ppid=,command=", "-p", str(pid)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as err:
        return None, "", f"`ps` could not be run ({type(err).__name__})"
    if done.returncode != 0:
        return None, "", f"`ps` found no process {pid}"
    text = done.stdout.decode("utf-8", "replace").strip()
    if not text:
        return None, "", f"`ps` returned nothing for pid {pid}"
    ppid_text, _, argv = text.splitlines()[0].strip().partition(" ")
    try:
        ppid = int(ppid_text)
    except ValueError:
        return None, "", f"`ps` output for pid {pid} did not begin with a ppid"
    return ppid, argv.strip(), ""


def _now() -> float:
    """The probe's clock, named so a test can hold it still."""
    return time.monotonic()


#: What a tag is not allowed to be, stated as exclusions rather than a charset.
#: `claude mcp list` printed `claude.ai Gmail` and
#: `plugin:supertool:claude-channel` on 2026-08-13, so spaces, dots and colons
#: are all legitimate in a real configured name and an allowlist narrow enough
#: to feel safe would refuse a working setup — the loud-for-quiet trade this
#: repo forbids. Only shapes no server name can have are excluded.
_CONTROL = frozenset(chr(code) for code in list(range(0x20)) + [0x7f])


def _tag_shape_objection(name: str) -> str:
    """Why this tag will not be asked about, or `""` — the check #1559 wanted.

    The tag comes out of another process's argv and `_channel_tags` filters
    *tokens*, not the remainder after `server:`, so `server:--help` arrived here
    as the name `--help`. `claude mcp get --help` exits 0, which turned a flag
    into a definite `subscribed`.
    """
    if not name:
        return "the tag after `server:` is empty, so it names nothing"
    if name.startswith("-"):
        return ("the tag after `server:` begins with `-`, so it is an option "
                "rather than a server name")
    bad = next((ch for ch in name if ch in _CONTROL), "")
    if bad:
        return (f"the tag after `server:` carries the control character "
                f"{ord(bad):#04x}, which no configured server name has")
    return ""


def _configured(name: str, timeout: float | None = None) -> tuple[bool | None, str]:
    """Does the harness have an MCP server called `name`? Three answers.

    `True` and `False` are both findings; `None` is the admission, and it is
    returned for every outcome that is not one of the two the CLI states
    plainly. A non-zero exit whose text this reader does not recognise is not a
    missing server: it is a lookup that failed.

    **Exit 0 is two answers, not one** (#2208). A server the harness rejected --
    declared in `.mcp.json`, switched off by `disabledMcpjsonServers` -- exits 0
    and says so on its `Status:` line, and it is a `False` here: the harness
    never loaded it, so it is not a second declaration in play. That is the one
    piece of prose read out of a successful lookup, and it is a claim about a
    load rather than about a connection -- see `CLAUDE_REJECTED_STATUS_RE`.

    **This is the construction site for an argv built out of ambient process
    state, so the shape check lives here rather than in the parser** (#1559).
    `_safe_path` and the `paths` declarations gate arguments the *caller*
    supplies into a filename slot; this value never crosses that chokepoint, and
    `_untrusted` is a rendering boundary rather than an argv one. Two halves,
    neither sufficient alone: `--`, so the callee's option parser cannot claim
    as a flag a value this tool did not mean as one, and the shape check, so a
    token that cannot be a server name is *declined* rather than asked about —
    with `--` alone, `server:--help` would merely become a confident negative
    off the same non-server token.
    """
    objection = _tag_shape_objection(name)
    if objection:
        return None, objection
    try:
        done = subprocess.run(
            [CLAUDE_BIN, "mcp", "get", "--", name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=CLAUDE_TIMEOUT if timeout is None else timeout)
    except (OSError, subprocess.SubprocessError) as err:
        return None, f"`{CLAUDE_BIN} mcp get` could not be run ({type(err).__name__})"
    out = done.stdout.decode("utf-8", "replace")
    if done.returncode == 0:
        # #2208: exit 0 covers two answers, not one. A server switched off by
        # `disabledMcpjsonServers` still exits 0 and says so on its own status
        # line, and the harness not having loaded it is a `False` here -- the
        # same answer an absent name gets, because the question every caller
        # asks is whether a SECOND channel-capable server is in play.
        if CLAUDE_REJECTED_STATUS_RE.search(out):
            return False, ""
        return True, ""
    if CLAUDE_UNKNOWN_SERVER in out:
        return False, ""
    return None, (f"`{CLAUDE_BIN} mcp get` exited {done.returncode} without saying "
                  f"the name is unknown")


def _channel_tags(argv: str) -> tuple[list[str] | None, str]:
    """The `server:` names a session argv subscribes to, `[]`, or why not.

    `[]` is a positive finding — this session armed no channel. `None` is the
    admission, and the case that produces it is why this is not a `split()`:
    the flag is variadic, so a server name containing a space arrives as two
    tokens. Reading the first would manufacture a name the harness has never
    heard of, and the confident false negative that follows is this op's own
    defect wearing the fix for it.
    """
    try:
        tokens = shlex.split(argv)
    except ValueError:
        return None, "the session argv did not tokenise"
    values: list[str] = []
    for index, token in enumerate(tokens):
        if token == CHANNEL_FLAG:
            for value in tokens[index + 1:]:
                if value.startswith("-"):
                    break
                values.append(value)
        elif token.startswith(CHANNEL_FLAG + "="):
            values.append(token[len(CHANNEL_FLAG) + 1:])
    if not values:
        return [], ""
    if not all(value.startswith(TAG_PREFIX) for value in values):
        return None, (f"a value after {CHANNEL_FLAG} is not a `{TAG_PREFIX}` tag, so "
                      f"a server name containing a space cannot be told from a "
                      f"second entry")
    return [value[len(TAG_PREFIX):] for value in values], ""


def _looks_like_a_session(argv: str) -> bool:
    """Is this argv recognisably a Claude session?

    Deliberately generous, because the cost of the two errors is not
    symmetrical: a session this fails to recognise costs a `CANNOT DETERMINE`,
    and one it recognises wrongly costs a definite verdict about a channel it
    knows nothing about. A harness launched as `node .../cli.js` is not
    recognised here and lands in the third state by design.
    """
    if CHANNEL_FLAG in argv:
        return True
    first = argv.split(" ", 1)[0]
    return first == CLAUDE_BIN or first.endswith("/" + CLAUDE_BIN)


def _dual_declaration_objection(path: str, tag_name: str,
                                resolved: naming.Resolved,
                                roots: list[Path] | None = None) -> str | None:
    """Is a *different*, standing `.mcp.json` server also going to bind `path`?

    #2133/#2136 catch the collision reactively, off a marker the losing
    process writes beside the socket it lost — and that marker exists only
    between the loss and the winner's next (re)bind, which clears it. #2051's
    own 2026-09-01 comment measured a session past that window: the collision
    had already happened, the harness's MCP connections were both
    `CONNECTION_CLOSED`, and no marker was left beside this socket by the
    time anyone looked, so `subscription()` still read `subscribed`.

    This check needs no marker and nothing has to happen first. This
    repository's own `.mcp.json` declares `claude-channel` unconditionally
    (#1541); absent an explicit `env` block redirecting it, that server
    inherits the session's environment — the same one a
    `--dangerously-load-development-channels server:NAME` tag's consumer
    inherits. Two channel-capable servers resolving one socket by
    construction is a fact about two config files, readable before either
    process ever binds or refuses anything.

    `None` when the matched tag *is* `CONSUMER_SERVER` — subscribing through
    the standing server is one declaration, not two — when no `.mcp.json` in
    the checked roots declares it at all, or when it declares it pointed at
    a different socket (an explicit `env` redirect is a deliberate second
    channel, #2044's shape, not a collision).

    **What this does NOT check (reviewer finding on #2051's own fix): whether
    `tag_name`'s own configured server resolves to `path`.** Only the standing
    `CONSUMER_SERVER` side is compared against `path` here — `tag_name`'s
    server could be declared anywhere `_configured` cannot see into (a
    `--mcp-config` file, a different `.mcp.json` root), so there is no
    general way to ask what socket it targets. A session running two
    genuinely isolated channel servers can therefore still read
    `CANNOT DETERMINE` from this check. That is the same direction as every
    other decline `subscription()` makes — never a false `SUB_SUBSCRIBED` —
    so it costs precision, not correctness in the sense this file cares
    about, but it is a real limitation and not a rounding error.
    """
    if tag_name == CONSUMER_SERVER:
        return None
    for root in (_mcp_roots() if roots is None else roots):
        mcp_path = Path(root) / MCP_FILENAME
        env, why = _declared_env(mcp_path)
        if env is None:
            # `_declared_env` collapses several different reasons into one
            # `(None, why)` shape (#2051 reviewer finding). Two of them are
            # genuinely "nothing declared here" and safe to keep looking
            # past: no `.mcp.json` at this root at all, or one that parsed
            # fine and simply names no `CONSUMER_SERVER` entry. The rest --
            # unreadable (a permission error, a symlink), not valid JSON, or
            # an `env` block that is not an object -- mean the file exists
            # and *could* declare the standing server, and this call cannot
            # tell. Reading that as "no collision" would be exactly the
            # absence-read-as-presence defect this file's neighbours
            # (`consumer_lines`, and the `read_refusal` branch a few lines
            # below this one) already guard against, one call site over.
            if why == f"no {MCP_FILENAME} at {mcp_path}" or (
                    why == f"{mcp_path} declares no {CONSUMER_SERVER} server"):
                continue
            return (f"{mcp_path} could not be checked for a "
                    f"{CONSUMER_SERVER} declaration ({_untrusted.flat(why)}), "
                    f"so whether it collides with {TAG_PREFIX}{tag_name} on "
                    f"this socket cannot be ruled out")
        theirs_sock = (naming.resolve(env).sock
                       if any(var in env for var in CHANNEL_VARS)
                       else resolved.sock)
        if theirs_sock == path:
            return (f"{mcp_path} declares {CONSUMER_SERVER} on this same "
                    f"socket, unconditionally (#1541) — a session carrying "
                    f"both that standing server and {TAG_PREFIX}{tag_name} "
                    f"spawns two channel-capable servers over one socket. "
                    f"One binds; the harness's connection to the other "
                    f"closes (#2133), and there is no marker requirement "
                    f"here for that to be true")
    return None


def subscription(pid: Any, pid_note: str = "", path: str | None = None,
                 resolved: naming.Resolved = None,  # type: ignore[assignment]
                 roots: list[Path] | None = None) -> Subscription:
    """Is any session subscribed to the channel this consumer serves? (#1543)

    The chain, and every link of it is read rather than assumed: the consumer
    is spawned by the session as an MCP server, so its **parent** is the
    session; a session subscribes only through `CHANNEL_FLAG`; and the harness
    refuses a tag naming a server it has not got configured. Two of those are
    in the process table and the third is `claude mcp get`.

    What this does NOT establish, and the report says so on the positive arm:
    that the configured server the tag names is the one holding this socket.
    Two channel-capable servers under one session satisfy both halves
    separately. It is a narrower doubt than the one #1543 was filed about, and
    naming it is cheaper than a probe that would still not close it.

    `path`, optional, is #2133's addition: when the caller has a socket to
    check, a tag naming a *configured* server is no longer read as
    `subscribed` if this run's own evidence (`read_refusal`) says a rival was
    refused this exact socket. `claude mcp get` cannot make that call itself —
    #1558 established that probing it spawns a second process, so a live
    singleton legitimately fails that same connect every time, and "Failed to
    connect" from the lookup is not a usable signal. The marker is a different
    and stronger kind of evidence: not a fresh probe racing the collision, but
    a record the collision already happened, written by the process that lost
    it, during this consumer's own run. Callers with no socket to check (the
    existing #1543 unit tests among them) get exactly the old behaviour.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return _sub(SUB_UNKNOWN,
                    "NOT established — no consumer pid to ask about",
                    ("nothing here says whether a session is subscribed",))
    origin = f" ({pid_note})" if pid_note else ""
    ppid, _argv, why = _ps_fields(pid)
    if ppid is None:
        return _sub(SUB_UNKNOWN,
                    f"NOT established — the parent of consumer pid {pid}{origin} "
                    f"was not read",
                    (why,))
    if ppid <= 1:
        return _sub(SUB_NOT_SUBSCRIBED,
                    f"none — consumer pid {pid}{origin} has been reparented to pid "
                    f"{ppid}, so the",
                    ("session that spawned it has exited. A consumer outlives its "
                     "session only",
                     "as an orphan, and an orphan has nobody to deliver to"))
    _, parent_argv, parent_why = _ps_fields(ppid)
    if not parent_argv:
        return _sub(SUB_UNKNOWN,
                    f"NOT established — pid {ppid} spawned this consumer and its "
                    f"argv was not read",
                    (parent_why,))
    shown = _untrusted.flat(parent_argv)
    if not _looks_like_a_session(parent_argv):
        return _sub(SUB_UNKNOWN,
                    f"NOT established — pid {ppid} spawned this consumer and is not "
                    f"recognisably",
                    (f"a Claude session: {shown}",
                     "a session launched under another argv reads the same from "
                     "here, so this",
                     "is a declined probe rather than a definite negative"))
    tags, tag_why = _channel_tags(parent_argv)
    if tags is None:
        return _sub(SUB_UNKNOWN,
                    f"NOT established — the argv of session pid {ppid} did not parse",
                    (tag_why, shown))
    if not tags:
        return _sub(SUB_NOT_SUBSCRIBED,
                    f"none — session pid {ppid} spawned this consumer and carries no",
                    (f"{CHANNEL_FLAG} tag, so nothing it",
                     "is handed is surfaced. Every event read here is discarded",
                     f"session argv: {shown}"))
    # Every tag is asked, and an unresolved one does not end the loop: the flag
    # is variadic, a session subscribed through the second tag is subscribed,
    # and returning `CANNOT DETERMINE` off the first would be an absence
    # produced by the order of somebody else's argv.
    # Every lookup in this loop shares one wall-clock budget (#1558). The flag
    # is variadic, so a per-tag timeout is unbounded in a number somebody else's
    # argv chooses; a tag the budget ran out before reaching is reported as
    # unasked, which is neither a missing server nor a silent drop.
    undecided: list[str] = []
    deadline = _now() + MCP_LOOKUP_BUDGET
    for name in tags:
        # Shape before budget, and via the same helper `_configured` guards
        # with: a tag that cannot be a server name is refused for free, so
        # reporting it as one the clock ran out on would name a reason the
        # clock played no part in.
        objection = _tag_shape_objection(name)
        if objection:
            undecided.append(f"{TAG_PREFIX}{_untrusted.flat(name)}: {objection}")
            continue
        remaining = deadline - _now()
        if remaining <= 0:
            undecided.append(
                f"{TAG_PREFIX}{_untrusted.flat(name)}: the probe's "
                f"{MCP_LOOKUP_BUDGET:g}s lookup budget was spent before this tag "
                f"was reached, so it was never asked about")
            continue
        answer, ask_why = _configured(name, min(CLAUDE_TIMEOUT, remaining))
        if answer:
            # #2182: both gates below answer one question -- will a SECOND
            # channel-capable server bind this socket? -- and both used to
            # answer it from the filesystem. `_dual_declaration_objection`
            # reads a `.mcp.json` found by walking up from `__file__`;
            # `read_refusal` reads a marker written by whichever process lost
            # a bind. Neither file says what the HARNESS loaded, and both are
            # present in the ordinary supported configuration: every installed
            # copy of this plugin ships a `.mcp.json` declaring
            # `CONSUMER_SERVER`, and `claude mcp get` -- the call `_configured`
            # just made -- itself spawns a rival that loses the bind and marks.
            # Measured 2026-09-02: deleting the marker and running
            # `channel:health` alone brought it straight back, inside that
            # run's own 1.26s, so the refusal was self-inflicted and the
            # verdict self-perpetuating.
            #
            # The harness is the authority, and this file already reaches it.
            # Three answers, and only `False` opens the gates: `True` is a real
            # standing server and keeps #2051/#2133's findings exactly as they
            # were, `None` is a lookup that failed and keeps declining -- the
            # same direction every other decline here takes, because reading an
            # unanswered census as "no rival" would be this file's own defect
            # class one call site over.
            #
            # Not asked when the tag IS `CONSUMER_SERVER`: subscribing through
            # the standing server is one declaration, so there is no second one
            # to look for and the gates keep their old behaviour.
            standing = None
            if name != CONSUMER_SERVER:
                left = deadline - _now()
                if left > 0:
                    standing, _ = _configured(CONSUMER_SERVER,
                                              min(CLAUDE_TIMEOUT, left))
            if path is not None and standing is not False:
                dual_why = _dual_declaration_objection(
                    path, name, resolved if resolved is not None else RESOLVED, roots)
                if dual_why:
                    return _sub(
                        SUB_UNKNOWN,
                        f"NOT established — {TAG_PREFIX}{_untrusted.flat(name)} is "
                        f"configured, but",
                        (_untrusted.flat(dual_why),))
                collision, collision_why = read_refusal(path)
                if collision is not None:
                    reason = collision.get("reason")
                    reason_text = (_untrusted.flat(reason)
                                    if isinstance(reason, str) and reason
                                    else "unknown reason")
                    return _sub(
                        SUB_UNKNOWN,
                        f"NOT established — {TAG_PREFIX}{_untrusted.flat(name)} is "
                        f"configured, but a rival",
                        (f"claude-channel server was refused this exact socket "
                         f"during this run ({reason_text}, see `refused` above). "
                         "Two channel",
                         "servers configured for one session (#2133) means a "
                         "configured tag proves",
                         "nothing about which one the harness actually holds a "
                         "connection to —",
                         "`claude mcp get` cannot tell them apart (#1558), so this "
                         "lands in the",
                         "third state rather than the positive one"))
                if collision_why != _REFUSAL_ABSENT:
                    # A marker exists and could not be read — the same symlink
                    # or corruption risk `_refusal_lines` declines on (#2133).
                    # Reading that as "no collision" would be exactly the
                    # absence-read-as-presence defect this file exists to
                    # remove, one call site over from where it was fixed.
                    return _sub(
                        SUB_UNKNOWN,
                        f"NOT established — {TAG_PREFIX}{_untrusted.flat(name)} is "
                        f"configured, but a refusal",
                        (f"marker for this socket could not be read "
                         f"({_untrusted.flat(collision_why)}), so a collision",
                         "during this run cannot be ruled out. Same defensive read "
                         "`read_health` uses",
                         "(#148, #1184/#1187) — guessing 'no rival' off an "
                         "unreadable marker would be",
                         "the same defect this state exists to remove, one call "
                         "site over"))
            census = ((f"the harness has no {CONSUMER_SERVER} server "
                       f"configured, so a `.mcp.json`",
                       "declaring one was not loaded and any refusal marker "
                       "beside this socket",
                       "was left by a short-lived `claude mcp get` probe, not "
                       "by a session (#2182)")
                      if standing is False else ())
            return _sub(SUB_SUBSCRIBED,
                        f"subscribed — session pid {ppid} carries "
                        f"{TAG_PREFIX}{_untrusted.flat(name)}, and the",
                        ("harness has a server configured under that name",
                         *census,
                         "NOT established: that the configured server is the one "
                         "holding this",
                         "socket. Two channel-capable servers would satisfy both "
                         "halves apart"))
        if answer is None:
            undecided.append(f"{TAG_PREFIX}{_untrusted.flat(name)}: {ask_why}")
    if undecided:
        return _sub(SUB_UNKNOWN,
                    f"NOT established — whether the harness has the server(s) "
                    f"session pid {ppid}",
                    ("asked for configured was not settled", *undecided))
    named = ", ".join(TAG_PREFIX + _untrusted.flat(name) for name in tags)
    return _sub(SUB_NOT_SUBSCRIBED,
                f"none — session pid {ppid} asked for {named}, and the harness has",
                ("no MCP server configured with that name. It refuses the tag at "
                 "startup and",
                 "the session subscribes to nothing; a server loaded from "
                 "`--mcp-config` binds",
                 "this socket and reaches that state (#1544)"))


def subscription_for_socket(path: str) -> Subscription:
    """The same answer, for a caller that has not already resolved the holder.

    One inference path for two surfaces, the way `transport.delivery_of` is one
    for three: `radar` and this op disagreeing about the same question is how
    the board and the health report ended up saying different things about the
    same socket in the first place.
    """
    holder, why = peer_pid(path)
    if holder is not None:
        return subscription(holder, path=path)
    record, _ = read_health(path)
    claimed = record.get("pid") if isinstance(record, dict) else None
    if isinstance(claimed, int):
        return subscription(claimed, "self-reported by the health file", path)
    return _sub(SUB_UNKNOWN,
                "NOT established — no consumer pid was resolved for this socket",
                (why or "the health file names no pid",))


def _identity_lines(record: dict, holder: int | None, holder_why: str) -> list[str]:
    """Who the consumer says it is, and whether that survived a second source.

    Shared by `health` and `probe` (#1593) rather than copied, because the two
    reports make the same claim about the same pid and a second copy of these
    words is a second place for the hedges to rot. The hedges are the content:
    `pid` is the health file's claim about its own writer, and the verified arm
    still declines to call it an identity — a same-uid process that binds the
    socket and writes the file is its own peer and agrees with itself.
    """
    claimed = record.get("pid")
    if holder is not None:
        return [
            f"  consumer : pid {claimed}, up since {_stamp(record, 'started')}",
            "             socket-holder verified: the process holding this socket is",
            "             the one the health file names. That rules out a stale or",
            "             forged file beside a live consumer; it does not rule out a",
            "             same-uid process that bound the socket and wrote the file,",
            "             which is its own peer and agrees with itself.",
        ]
    return [
        f"  consumer : pid {claimed} (self-reported), up since "
        f"{_stamp(record, 'started')}",
        f"             socket-holder NOT checked — {holder_why}",
        "             the health file names its own writer; pids are reusable, so",
        "             nothing here proves that process is the one holding the socket",
    ]


def health(path: str) -> tuple[int, str]:
    """The whole report, and the exit code that encodes its state."""
    state, detail = probe_socket(path)
    head = [f"  socket   : {path}", f"             {detail}"]
    head += _channel_lines(path, RESOLVED)
    head += _refusal_lines(path)

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

    identity = _identity_lines(record, holder, holder_why)

    # #1543: every line above is about the producer half, and all of them were
    # true in the incident that filed this — bound socket, verified holder,
    # fresh counters — while the session had refused the channel tag at startup
    # and nothing could ever arrive. `FORWARDING` with `last forwarded never`
    # renders identically to a quiet morning, so the subscription answer decides
    # the verdict rather than being printed under one that contradicts it.
    sub = subscription(holder if holder is not None else claimed,
                       "" if holder is not None else "self-reported by the health file",
                       path)
    counters = [
        f"  counters : {_num(_counter(record, 'lines_read'))} lines read, "
        f"{_num(_counter(record, 'forwarded'))} forwarded, "
        f"{_num(_counter(record, 'dropped'))} dropped",
        f"             last forwarded {_stamp(record, 'last_forwarded', 'never')}"
        f" (counters refreshed {_stamp(record, 'updated')})",
    ]
    if sub.state == SUB_NOT_SUBSCRIBED:
        # The counters stay, and they are still true: they are what the consumer
        # read and handed on. What they are not is evidence of delivery, and
        # under this verdict a reader can see both facts at once.
        return RC_NOT_SUBSCRIBED, "\n".join([
            "channel: BOUND, NOT SUBSCRIBED", *head,
            _health_note(), *identity, *sub.lines, *counters, "", CEILING,
        ])
    if sub.state == SUB_UNKNOWN:
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            _health_note(), *identity, *sub.lines, *counters, "", CEILING,
        ])
    return RC_FORWARDING, "\n".join([
        "channel: FORWARDING", *head,
        _health_note(),
        *identity,
        *sub.lines,
        *counters,
        "", CEILING,
    ])


def probe(path: str, *, wait: float = PROBE_WAIT_SECS) -> tuple[int, str]:
    """Put one synthetic event through the path, and report only what moved.

    `health` reads counters somebody else's traffic wrote. This writes the
    traffic. That is the whole difference, and it is the one #1593 is about: a
    consumer that is bound, verified and counting publishes the same numbers
    whether it is reading its socket or wedged on it, so with no traffic of its
    own the strongest existing report says nothing about *now*.

    **The refusal is the deliverable.** Arrival inside a session is observable
    only from inside that session, so this reports which of the consumer's own
    counters advanced and then hands the caller an exact tag to look for. It
    never renders `forwarded` as receipt — see `PROBE_CEILING`, which is
    printed under every verdict including the good one, for the same reason
    `CEILING` is.

    **Three counters, not one, and that is why there are more verdicts than
    the issue proposed.** The consumer moves `lines_read` when it takes a line
    off the wire, `forwarded` when it hands one on and `dropped` when it
    refuses one. Reading only `forwarded` folds *has not read it yet* into
    *read it and handed it nowhere*, which are a slow consumer and a broken one
    — and sends an operator to the wrong half of the bridge.

    The wait is bounded, so a consumer slower than `wait` renders exactly like
    one that did nothing. Every arm that reports a non-advance prints the
    budget it actually waited, and the arm where nothing was even read is
    `CANNOT DETERMINE` rather than a finding.
    """
    # Baseline first: a counter read *after* the emit cannot be compared with
    # anything, and re-reading it later would compare the consumer against
    # itself.
    before, before_why = read_health(path)

    watcher_id = transport.probe_id()
    verdict = transport.emit_socket(transport.probe_record(watcher_id), path)
    tag = (f'<channel watcher_source="{transport.PROBE_SOURCE}" '
           f'id="{watcher_id}" event="{transport.PROBE_EVENT}">')

    head = [f"  socket   : {path}", f"             {verdict.detail}"]
    head += _channel_lines(path, RESOLVED)
    head += [
        f"  probe    : one synthetic event written — source "
        f"{transport.PROBE_SOURCE}, id {watcher_id}, event {transport.PROBE_EVENT}",
        "             a reserved source, so no watcher is impersonated; and no",
        "             watcher state file was written or overwritten by this call",
    ]

    def report(headline: str, *body: str) -> str:
        return "\n".join([headline, *head, *body, "", PROBE_CEILING])

    if verdict.state == transport.EMIT_NO_LISTENER:
        return RC_NOT_DELIVERING, report(
            "channel: NOT DELIVERING",
            "  consumer : none — this event was lost at the source, and so is",
            "             every event a poller emits right now",
            "  expect   : nothing. No tag for this probe can appear in any session,",
            "             because nothing took the bytes",
        )
    if verdict.state != transport.EMIT_ACCEPTED:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE",
            f"  consumer : the write did not complete — {verdict.detail}",
            "             nothing here can tell whether a partial line reached a",
            "             consumer, so this is a declined probe, not a negative",
            f"  expect   : possibly {tag} — unmeasured either way",
        )

    if before is None:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE",
            f"  consumer : bound and it took the bytes, but {before_why}",
            "             with no counters there is no baseline, so an advance could",
            "             not have been observed however well the path is working",
            *_holder_lines(path),
            f"  expect   : possibly {tag} — this op could not check",
        )
    # `allow_stale`, and it is the whole shape of this op: a cold stamp on the
    # baseline is what `health` cannot get past, and getting past it is why
    # `probe` exists. Everything else `_health_objection` refuses still refuses
    # here — a file naming a dead pid or no pid has no baseline in it at all.
    objection = _health_objection(before, allow_stale=True)
    if objection:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE", _health_note(),
            f"  consumer : bound and it took the bytes, but {objection}",
            *_holder_lines(path),
            f"  expect   : possibly {tag} — this op could not check",
        )
    #: Recorded, never waved away: a report that quietly started from cold
    #: counters and a report over a channel that was warm all along must not
    #: read the same, because they are different findings about the consumer's
    #: heartbeat even when the verdict on the path is identical.
    cold = _health_objection(before)

    claimed = before.get("pid")
    holder, holder_why = peer_pid(path)
    if holder is not None and holder != claimed:
        # Same argument as `health`'s fourth state (#1192), one step further on:
        # the counters this probe would compare were published by something
        # that is not holding the socket, so an advance in them would be
        # evidence about the impersonator rather than about the event just
        # written.
        return RC_CONTRADICTED, report(
            "channel: CONTRADICTED", _health_note(),
            f"  consumer : the health file names pid {claimed}, but pid {holder} is "
            f"the process holding this socket",
            "             an advance in counters published by something that is not",
            "             the consumer would say nothing about the event just written,",
            "             so this probe is not measured rather than failed",
            f"  expect   : unknown — {tag} may or may not appear",
        )

    identity = _identity_lines(before, holder, holder_why)
    if cold:
        identity += [
            f"             baseline counters were cold — {cold}",
            "             that is not a reason to decline here, it is the question:",
            "             whether they come back advanced after this emit is the",
            "             measurement, and the re-read below waives nothing",
        ]
    # `_health_objection` has already established that `forwarded` is a readable
    # number on the baseline; the other two are optional and their absence is
    # reported rather than defaulted.
    base_fwd = _counter(before, "forwarded")
    base_drop = _counter(before, "dropped")
    base_read = _counter(before, "lines_read")

    started = time.monotonic()
    # No pre-loop defaults for the four names the loop binds, and that is a
    # decision rather than a tidy-up (#1758, where a bot flagged `waited = 0.0`
    # as merely unnecessary). The loop is `while True` with no `continue`, so
    # its first three statements bind all four before anything reads them and
    # every default was dead. What a default would buy is worse than nothing:
    # `waited` is printed as a measured duration, so a restructure that skipped
    # the assignment would render a wait nobody performed as `after 0.0s`, and
    # `after_why` an empty one as a reason-less reason. Unbound is an
    # `UnboundLocalError` on the first CI run; defaulted is a measurement this
    # op never made, printed as one it did — the exact defect this op exists to
    # refuse, rebuilt in its own bookkeeping. `after` keeps its annotation
    # because the type is worth stating; a bare annotation binds no value.
    after: dict | None
    while True:
        after, after_why = read_health(path)
        after_objection = "" if after is None else _health_objection(after)
        waited = time.monotonic() - started
        if after is not None and not after_objection:
            now_fwd = _counter(after, "forwarded")
            now_drop = _counter(after, "dropped")
            if now_fwd is not None and now_fwd > base_fwd:
                also = ""
                if base_drop is not None and now_drop is not None and now_drop > base_drop:
                    also = (f", and `dropped` by {now_drop - base_drop} in the same "
                            "window — at least one event was refused, and which of "
                            "them was this one is not on the record")
                return RC_FORWARDING, report(
                    "channel: FORWARDED", _health_note(), *identity,
                    f"  counters : forwarded {base_fwd} -> {now_fwd} "
                    f"(+{now_fwd - base_fwd}) after {waited:.1f}s{also}",
                    "             the consumer read from this socket and handed an",
                    "             event to the MCP transport inside the window this",
                    "             emit opened",
                    f"  expect   : {tag}",
                    "             in whichever session is subscribed to this channel",
                )
            if base_drop is not None and now_drop is not None and now_drop > base_drop:
                return RC_PROBE_DISCARDED, report(
                    "channel: ACCEPTED, DISCARDED", _health_note(), *identity,
                    f"  counters : dropped {base_drop} -> {now_drop} "
                    f"(+{now_drop - base_drop}) after {waited:.1f}s, forwarded "
                    f"unchanged at {base_fwd}",
                    "             the consumer read an event off this socket and",
                    "             refused it — the burst budget, a routing key over",
                    "             the attribute cap, or a handler that threw. Its own",
                    "             stderr names which, and `claude --debug` surfaces it",
                    f"  expect   : nothing for {tag}",
                    "             unless the discard was somebody else's event",
                )
        if waited >= wait:
            break
        time.sleep(min(PROBE_POLL_SECS, max(0.0, wait - waited)))

    # Out of budget. Everything below is about why, and the three answers are
    # not interchangeable: this op could not look, the consumer never looked, or
    # the consumer looked and did nothing.
    if after is None:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE",
            f"  consumer : it took the bytes, and then {after_why}",
            f"             the counters could not be re-read inside {waited:.1f}s, so",
            "             nothing was measured — this is not a report about the",
            "             consumer, it is a report about this process's eyesight",
            f"  expect   : possibly {tag} — unmeasured",
        )
    if after_objection:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE", _health_note(),
            f"  consumer : it took the bytes, but {after_objection}",
            *identity,
            f"  expect   : possibly {tag} — unmeasured",
        )

    now_read = _counter(after, "lines_read")
    counters = (
        f"  counters : forwarded {base_fwd} (unchanged), dropped "
        f"{_num(base_drop)} -> {_num(_counter(after, 'dropped'))}, lines read "
        f"{_num(base_read)} -> {_num(now_read)}, over {waited:.1f}s"
    )
    if base_read is None or now_read is None:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE", _health_note(), *identity, counters,
            "  consumer : it took the bytes and published no readable `lines_read`,",
            "             so `has not read it yet` and `read it and handed it",
            "             nowhere` cannot be told apart from here — and those are a",
            "             slow consumer and a broken one",
            f"  expect   : possibly {tag} — unmeasured",
        )
    if now_read > base_read:
        return RC_PROBE_NOT_FORWARDED, report(
            "channel: ACCEPTED, NOT FORWARDED", _health_note(), *identity, counters,
            "  consumer : it read an event off this socket and neither forwarded nor",
            "             discarded it. Its counters are fresh, so it is alive and",
            "             still publishing — this is a finding about its read loop,",
            "             not an absence of one",
            f"  expect   : nothing for {tag}",
            f"             (a consumer slower than the {wait:.0f}s waited here would",
            "             look identical, and would forward it after this report)",
        )
    return RC_UNKNOWN, report(
        "channel: CANNOT DETERMINE", _health_note(), *identity, counters,
        "  consumer : bound, alive, publishing — and it has not read this line off",
        f"             the wire within {waited:.1f}s. Wedged on its read loop and",
        "             merely slower than this budget are the same picture from here,",
        "             so this is not reported as a finding",
        f"  expect   : possibly {tag} — unmeasured",
    )


def main(argv: list[str]) -> int:
    sub = argv[1] if len(argv) > 1 else "health"
    if sub == "health":
        code, report = health(SOCK_PATH)
    elif sub == "probe":
        code, report = probe(SOCK_PATH)
    elif sub == "received":
        if len(argv) < 3:
            sys.stderr.write(
                "channel: `received` needs a count -- channel:received:N is "
                "this session's own report of how many channel events it has "
                "received so far (#2150)\n"
            )
            return 2
        try:
            count = int(argv[2])
        except ValueError:
            sys.stderr.write(f"channel: {argv[2]!r} is not an integer count\n")
            return 2
        if count < 0:
            sys.stderr.write("channel: a received count must not be negative\n")
            return 2
        code, report = record_received(SOCK_PATH, count)
    else:
        # Naming all three is the point. This message used to name only two,
        # and left unchanged it would send a caller asking "how many of these
        # did I actually get" back to ops that cannot answer that -- both
        # describe the forwarder's own outbox, and neither can see the inbox.
        sys.stderr.write(
            f"channel: unknown sub-op {sub!r} — the three are `channel:health` "
            "(read the consumer's published counters), `channel:probe` "
            "(write one synthetic event and report which counter moved), and "
            "`channel:received:N` (this session's own report of how many it "
            "received, compared against `forwarded`'s advance)\n"
        )
        return 2
    print(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
