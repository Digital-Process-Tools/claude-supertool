#!/usr/bin/env python3
"""One name for a watch channel, above the two variables it derives (#1477).

A private channel used to be two exports that had to agree:

    export SUPERTOOL_WATCH_SOCK=/tmp/supertool-watch-oss.sock
    export SUPERTOOL_WATCH_STATE_DIR=/tmp/supertool-watch-oss

and `presets/watch/README.md` says the quiet part: **setting only the socket is
worse than setting neither.** The poller slot is a pid file held `O_CREAT|O_EXCL`
by exactly one process per state directory (#476), so a second session that
redirects its socket and shares `/tmp` spawns no pollers at all — every slot is
already held, by pollers that captured the *first* session's socket at spawn and
keep it for life. Both boards render healthy from both sides (#1309).

The two variables are never independently useful. The only arrangement they can
express that a single name cannot is exactly the broken one, so the name is the
knob and they are the escape hatch.

**Why this is an environment variable and not a new config route.** A
non-reserved key in an op's `.supertool.json` block already reaches the
subprocess as a `SUPERTOOL_`-prefixed variable (`docs/contributing.md`), so
`{"ops": {"radar": {"watch_name": "oss"}}}` arrives here as
`SUPERTOOL_WATCH_NAME` with no new plumbing at all. What that route does **not**
reach is the consumer: `claude-channel` is spawned by the harness from
`.mcp.json`, never by supertool. That asymmetry is why `channel.consumer_lines`
exists — the name has two homes, and two homes that can disagree need a check,
not a promise.

**Precedence, and it is a decision rather than an accident.** An explicit
`SUPERTOOL_WATCH_SOCK` or `SUPERTOOL_WATCH_STATE_DIR` **overrides** the name.
Not because an export is more authoritative in principle, but because it is the
value a *running* poller already captured and cannot migrate away from
(`README.md`, "A watcher spawned before the variable was changed"): making the
name win would move the paths underneath a live fleet. The override is put in
`notes` and every surface that resolves prints them. A name losing silently to a
stale export is the failure this repo files hardest against, and half of what
this module is for is making sure that cannot happen quietly.

**`<base>` stays `/tmp`.** It is world-traversable and that is the subject of
#1184/#1187/#1197/#1200 — a per-name subdirectory is an opportunity to stop
deriving predictable names in a shared directory, and moving the base is a
migration for every running poller. That belongs in its own issue. What is done
here is narrower and free: the derived state directory is created `0700` rather
than inheriting `/tmp`'s mode.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _untrusted

import _untrusted  # noqa: E402  (a name is operator text on a rendered surface, #1423)

NAME_ENV = "SUPERTOOL_WATCH_NAME"
SOCK_ENV = "SUPERTOOL_WATCH_SOCK"
STATE_DIR_ENV = "SUPERTOOL_WATCH_STATE_DIR"

#: Not `os.path.join`, and deliberately: these are AF_UNIX paths, the two
#: constants they have to reproduce byte-for-byte are POSIX literals, and a
#: backslash separator on one platform would make a derived name disagree with
#: the default it is meant to sit beside.
BASE_DIR = "/tmp"
DEFAULT_SOCK = f"{BASE_DIR}/supertool-watch.sock"
DEFAULT_STATE_DIR = BASE_DIR

#: One path component, and nothing that can leave the directory it is joined to.
#: A leading dot is out because a state directory nothing lists is a fleet that
#: renders as absent; a leading dash is out because the name reaches argv-shaped
#: contexts. 32 is long enough for `oss`, `dvsi`, `pr-1477` and short enough that
#: the derived socket stays inside macOS's ~104-byte AF_UNIX path limit.
#:
#: `\Z`, not `$`: Python's `$` matches before a final newline, so `^…$` accepts
#: `oss` followed by a newline as the name `oss` (#1188). The `.strip()` in
#: `resolve` happens to hide that today, which is exactly the kind of accidental
#: defence that guard exists to refuse — and `channel.ts` uses JavaScript's `$`,
#: which is already strict, so the two ends only agree with `\Z` here.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")


class Resolved(NamedTuple):
    """Where this process's watch channel lives, and how that was decided.

    `notes` is not decoration. Every branch below that could surprise the reader
    — an override, a half-configured pair — writes a line here, and the surfaces
    print them. A resolution whose reasoning is invisible is the same defect as
    a verdict whose evidence is invisible.
    """

    name: str
    sock: str
    state_dir: str
    notes: list[str]
    refusal: str
    #: True only when the state directory was derived from the name. A path the
    #: operator handed over — or the `/tmp` default — is theirs, and its absence
    #: stays an unanswerable state rather than something this code manufactures
    #: (#693). `ensure_state_dir` will only create what this flag covers.
    state_dir_is_derived: bool = False


def sock_for(name: str) -> str:
    return f"{BASE_DIR}/supertool-watch-{name}.sock"


def state_dir_for(name: str) -> str:
    return f"{BASE_DIR}/supertool-watch-{name}"


def resolve(env: dict[str, str] | None = None) -> Resolved:
    """The socket and the state directory this process should use.

    Pure: reads a mapping, touches no filesystem. `ensure_state_dir` is the
    separate call for the one side effect, because a module constant computed at
    import must not create directories as a side effect of somebody importing it.
    """
    src = os.environ if env is None else env
    # `or` rather than `in`, matching the two variables it sits above: an
    # operator who exports an empty string gets the default, not a refusal
    # about a name they did not set.
    raw = (src.get(NAME_ENV) or "").strip()
    explicit_sock = src.get(SOCK_ENV) or ""
    explicit_state = src.get(STATE_DIR_ENV) or ""

    notes: list[str] = []
    refusal = ""
    name = ""
    if raw:
        if NAME_RE.match(raw):
            name = raw
        else:
            refusal = (
                f"{NAME_ENV}={_untrusted.flat(raw)!r} is not usable as a path "
                f"component and was ignored — it must match {NAME_RE.pattern}. "
                f"This channel is on the default paths, not a private one."
            )

    sock = sock_for(name) if name else DEFAULT_SOCK
    state_dir = state_dir_for(name) if name else DEFAULT_STATE_DIR

    if explicit_sock:
        if name:
            notes.append(
                f"{SOCK_ENV} is set and overrides the name: the socket is "
                f"{explicit_sock}, not {sock}")
        sock = explicit_sock
    if explicit_state:
        if name:
            notes.append(
                f"{STATE_DIR_ENV} is set and overrides the name: poller slots are "
                f"in {explicit_state}, not {state_dir}")
        state_dir = explicit_state

    if not name and bool(explicit_sock) != bool(explicit_state):
        # The state `README.md` calls worse than setting neither. It does not
        # stop being a footgun because the operator declined the name, and the
        # name is the one-step way out of it.
        missing = STATE_DIR_ENV if explicit_sock else SOCK_ENV
        notes.append(
            f"{missing} is NOT set while its partner is — the half-configured "
            f"state that is worse than configuring neither (#1309). "
            f"{NAME_ENV} sets both from one word.")

    return Resolved(name=name, sock=sock, state_dir=state_dir,
                    notes=notes, refusal=refusal,
                    state_dir_is_derived=bool(name) and not explicit_state)


def state_dir_provenance(resolved: Resolved) -> str:
    """Which knob put the poller slots where they are, in the operator's words.

    `cmd_watch`'s refusal used to end "Check that SUPERTOOL_WATCH_STATE_DIR names
    a writable directory" unconditionally. Under a name that is the one variable
    the operator deliberately did not set, so the sentence sends them to a knob
    that is not in force — a refusal naming the wrong cause is barely better than
    a silent one, and it is the same misdirection this file exists to remove.
    """
    if resolved.state_dir_is_derived:
        return (f"{STATE_DIR_ENV} is not set — this directory was derived from "
                f"{NAME_ENV}={resolved.name}")
    if resolved.state_dir != DEFAULT_STATE_DIR:
        return f"set by {STATE_DIR_ENV}"
    return (f"the default; {NAME_ENV} or {STATE_DIR_ENV} would move it")


#: What a scan of the state directory established about the directory itself.
#: Three states, not two, because a name derives a directory only a *spawn*
#: creates (`ensure_state_dir`) while the default is `/tmp`, which always exists:
#: `os.listdir` was unreachable-by-luck on the default and raised on the first
#: read after naming a channel (#1502).
#:
#: `ABSENT` is a knowable fact about the world — zero watchers, nothing has ever
#: spawned on this channel. `UNREADABLE` is the admission that the population is
#: unknown. A reader that collapses the second into the first prints a claim
#: about the fleet on the strength of a listing that never happened, which is
#: this preset's most-filed defect.
STATE_DIR_OK = "ok"
STATE_DIR_ABSENT = "absent"
STATE_DIR_UNREADABLE = "unreadable"


def state_dir_listing(state_dir: str) -> tuple[list[str], str, str]:
    """(sorted entries, one of the three states above, why not). Creates nothing.

    Here rather than in `transport`, because `transport` and `channel` both
    enumerate this directory and `channel.py` already says of it that "a second
    convention for it would be one more thing to keep in step". Two copies of a
    three-state classifier is two places to get it wrong, and #1502's first fix
    was scoped to `transport.py` while `channel.stranded_watchers` still turned
    an unlistable directory into `none recorded an emit into this socket`.

    Takes the directory rather than reading a module constant: each module
    resolves its own, and the tests monkeypatch each module's.

    It creates nothing. Manufacturing the directory on a read would give a read
    side effects and resurrect #693 for an operator-supplied
    `SUPERTOOL_WATCH_STATE_DIR`, which is why only a *derived* one is created and
    only on the spawn path.
    """
    try:
        return sorted(os.listdir(state_dir)), STATE_DIR_OK, ""
    except FileNotFoundError:
        return [], STATE_DIR_ABSENT, ""
    except OSError as err:
        # A file at the name (`NotADirectoryError` on POSIX and on Windows), a
        # mode that excludes this uid, a vanished mount. All the same answer: it
        # is there and the population could not be established. `FileNotFoundError`
        # is caught above and is not reachable here, which is what keeps "nothing
        # spawned yet" from absorbing "could not look".
        return [], STATE_DIR_UNREADABLE, (
            f"{state_dir} could not be listed ({type(err).__name__})")


def state_dir_absence_note(state_dir: str, state: str, why: str) -> str:
    """One sentence for a reader whose listing found nothing, or "" for `ok`.

    Shared so the two surfaces that say it cannot word it differently, and so a
    third one cannot omit it.
    """
    if state == STATE_DIR_ABSENT:
        return (f"the state directory {state_dir} does not exist yet, so nothing "
                f"has ever spawned on this channel")
    if state == STATE_DIR_UNREADABLE:
        return f"{why}, so nothing here is evidence of absence"
    return ""


def disclosure_lines(resolved: Resolved) -> list[str]:
    """The channel this process is on, for any surface that renders a board.

    `[]` when there is nothing to say — the default paths, no override, no
    refused name. A banner printed on every board is one nobody reads, and on
    the default channel there is no half-set state to disclose.

    **Why a formatter here rather than a print in `transport` (#1495).** The
    #1476/#1477 reviewer raised `notes` as computed-and-never-printed and it was
    argued down there deliberately: `transport` is imported by a *detached
    poller* whose stdout has no reader, so a print from it writes into nothing.
    What was left open is exactly this — one formatter, consumed by every render
    through `transport.channel_disclosure`, so `radar`, `watches` and
    `channel:health` cannot disagree about the same resolution. `delivery_of`
    lives in `transport` for the same reason.

    The paths are this module's own text; the *name* is not. It arrives from an
    op's `.supertool.json` block or from the environment, and it lands on
    `watches`' fixed-width board, where a newline used to be able to print a
    whole extra row at column 0 (#1423). So it is flattened here, once, rather
    than at each of the three call sites.
    """
    out: list[str] = []
    if resolved.name:
        out.append(
            f"name {_untrusted.flat(resolved.name)} (from {NAME_ENV}) — socket "
            f"{resolved.sock}, poller slots {resolved.state_dir}")
    if resolved.refusal:
        out.append(resolved.refusal)
    out.extend(resolved.notes)
    return out


def ensure_state_dir(resolved: Resolved, state_dir: str) -> str:
    """Create a *derived* state directory if it is missing. "" or why not.

    A name derives a directory nobody has made, and `claim_pidfile`'s `os.open`
    inside a missing directory raises `ENOENT` — which lands, correctly, in
    `CLAIM_UNKNOWN` and a refusal telling the operator to check a variable they
    deliberately did not set. Correct and useless: the point of the name is not
    having to know about the directory.

    **It creates only what this process derived, and that boundary is the whole
    care in this function.** A `SUPERTOOL_WATCH_STATE_DIR` the operator supplied
    — and the `/tmp` default — is somebody else's path, and a missing one there
    is an *unanswerable* state that `cmd_watch` reports rather than repairs
    (#693, `tests/test_unanswerable_checks_693.py`). Manufacturing it would trade
    a loud refusal for a poller quietly spawned into a directory nobody asked
    for, which is the same trade this repo keeps filing against.

    `state_dir` is passed rather than read off `resolved` because callers
    monkeypatch the module constant; the flag says whether creating is allowed,
    the argument says where.

    `0700`, not `/tmp`'s mode. `mode=` applies only to directories this call
    creates — a small piece of the #1184 family bought for free, not a claim to
    have closed it.
    """
    if not resolved.state_dir_is_derived:
        return ""
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
    except OSError as err:
        return (f"{state_dir} could not be created ({type(err).__name__}) — "
                f"no poller slot can be claimed there")
    return ""
