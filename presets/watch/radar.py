#!/usr/bin/env python3
"""radar — reconcile registered tiers against live truth, then report.

Radar's core is one sentence, and merge requests are not in it:

    Reconcile registered tiers against live truth, heal their watchers, report,
    stay idempotent, and never render an unknown as green.

GitLab MRs were simply the first tier anyone wrote (#528). The board lives in
`tiers/gl_mrs.py` and joins on exactly the same footing as `gl-runners`,
`gh-prs`, or anything else with a population and a standing question.

    {"ops": {"radar": {"radar_tiers": {"gl-mrs": {}, "gl-runners": {}}}}}

With no tiers configured radar refuses and says how to fix it. It does not
quietly do nothing, and it does not assume `gl-mrs`:

  * Silence is the failure this whole preset is built against. An unconfigured
    radar that prints nothing is byte-identical to a healthy one.
  * A `gl-mrs` default is an opinion imposed on strangers — it points GitLab
    API calls at people who may be on GitHub, and hides from them that radar
    is configurable at all.
  * The refusal is also the documentation: it teaches the config at the moment
    someone needs it.

The tier contract
-----------------

A tier is a Python module reachable by name (see `_tier_module`) exposing:

    RADAR_OPTIONS       set[str] — config keys this tier understands. Anything
                        else in its config block is named on the board, never
                        silently ignored.
    RADAR_QUIET_DEFAULT bool, optional (default True) — is a healthy tier
                        silent? True for a side-concern like the runner fleet,
                        where a green line per run is the noise that trains a
                        reader to skim past the red one. False for a tier whose
                        report *is* the board: an MR reconcile that prints
                        nothing on a quiet day is indistinguishable from one
                        that failed to run.
    radar_report(options) -> (lines, healthy)
                        `healthy` means "this tier could tell you the truth",
                        not "the world is fine". A board full of red MRs is
                        healthy; a board that could not be built is not.

Radar injects two reserved keys into `options` before the call. Config cannot
set them — `read_tiers` refuses any key starting with `_` — so a tier can trust
them:

    _arg    str, the raw invocation argument (`radar:author=@me` -> "author=@me")
    _watch  callable(source, scope, only=None) -> "alive"|"spawned"|"failed"|
            "capped". Radar's bounded spawner. Every slot a tier asks for is
            recorded, and radar itself emits the cap warning when one is
            refused.

`_watch` is a callable rather than the declarative `radar_watchers(options) ->
[(source, scope, only)]` #528 proposed, and the difference is load-bearing. A
declared slot must be spawned before the report, and the MR tier must *not*
spawn its discovery feed when live GitLab was unreachable — the current
behaviour, pinned by a test. Only the tier knows whether spawning is safe, and
it needs the spawn result inside its own report (the feed's status is a token
in the board footer). Two mechanisms for one job is the drift this codebase
keeps filing bugs about, so there is one: radar owns the bound, the tier owns
the timing.

Never green when it cannot tell
-------------------------------

Three states, not two: ok, a finding, and *cannot tell*. A tier that fails to
resolve or raises is not a quiet degradation — its message goes to stderr and
radar exits non-zero, because a refactor that traded a loud failure for a quiet
one would be the house defect (#486, #533) delivered by the fix for it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

_HERE = Path(__file__).parent

sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))  # for _untrusted
import _untrusted  # noqa: E402  (the state files are somebody else's text, #1423)
import channel  # noqa: E402  (one subscription answer for both surfaces, #1543)
import dispatcher  # noqa: E402
import sourcepath  # noqa: E402  (where sources may live, one resolver, #2135)
import transport  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ops.radar.radar_tiers, JSON-encoded into the env by the op runner — the same
# route ops.gl-job.job_patterns takes.
TIERS_ENV = "SUPERTOOL_RADAR_TIERS"

# Keys radar fills in on a tier's options dict. Refused in config so a tier can
# tell radar's context apart from the operator's configuration.
RESERVED_PREFIX = "_"

NO_TIERS = ('radar: no tiers configured. Add ops.radar.radar_tiers to .supertool.json —\n'
            '       e.g. {"gl-mrs": {}} for the GitLab MR board.')


def ensure_watcher(source: str, scope: str, only: list[str] | None = None) -> str:
    """One live poller for a slot.

    "alive"|"spawned"|"failed"|"capped"|"unclaimable" — the last is #693's third
    state, passed straight through from `start_poller`: the slot could not be
    claimed, so nothing was spawned and nothing was established about it.

    Idempotent, because radar runs on a loop and n pollers over one slot means
    n copies of every event. `start_poller` claims the slot before the fork:
    reading a pidfile and then spawning is not that check, since the PID is
    published after a fork and a detach, so two runs landing in that window
    both see an empty slot and both spawn (#476).

    The death cap from #513 is applied here rather than in any one caller. That
    bound lived on the per-MR path alone, so every other spawner would respawn
    a poller that dies on every tick, forever, silently. Reproducing a fixed
    defect in a new tier is how it comes back, so the cap belongs at the one
    place that spawns.
    """
    if dispatcher._load_source(source) is None:
        return "failed"
    if len(transport.deaths(source, scope)) >= transport.DEATH_RESPAWN_LIMIT:
        return "capped"
    return dispatcher.start_poller(source, scope, only or [])[0]


def watcher_cap_warnings(statuses: dict[str, str]) -> list[str]:
    """Name every slot radar has stopped respawning. Never silent about it.

    A capped slot is not being watched, and the whole lesson of #513 is that a
    monitoring surface going quiet reads exactly like nothing being wrong.
    """
    return [
        f"radar: WARNING — stopped respawning {name}: it has died "
        f"{transport.DEATH_RESPAWN_LIMIT}+ times and nothing is polling it. "
        f"Fix it, then re-arm with `watch:{name}`."
        for name, status in sorted(statuses.items()) if status == "capped"
    ]


def read_tiers(raw: str | None = None) -> tuple[dict[str, dict], list[str]]:
    """({op_name: options}, complaints) from ops.radar.radar_tiers.

    Empty by default, and that default is why `main` refuses rather than
    printing an empty board. Radar has no opinion about what anyone watches;
    a tier that reaches for GitLab, or runners, or any other resource, must be
    asked for by name.

    Nothing here raises. A config this cannot parse yields no tiers plus a
    complaint, which reaches the reader as the refusal plus the reason.
    """
    raw = os.environ.get(TIERS_ENV, "") if raw is None else raw
    raw = raw.strip()
    if not raw:
        return {}, []
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, [f"radar: WARNING — radar_tiers is not valid JSON ({exc.msg}). "
                    f"No tiers loaded."]
    if not isinstance(loaded, dict):
        return {}, ["radar: WARNING — radar_tiers must be an object keyed by op name, "
                    "e.g. {'gl-mrs': {}}. No tiers loaded."]

    out: dict[str, dict] = {}
    problems: list[str] = []
    for name, opts in loaded.items():
        if opts is None:
            opts = {}
        if not isinstance(opts, dict):
            problems.append(f"radar: WARNING — tier '{name}' options must be an object; "
                            f"got {type(opts).__name__}. Tier skipped.")
            continue
        # Underscored keys are radar's own context channel (_arg, _watch). A
        # config that could forge them would be a config that could lie to a
        # tier about what radar asked for.
        reserved = sorted(k for k in opts if str(k).startswith(RESERVED_PREFIX))
        if reserved:
            problems.append(f"radar: WARNING — tier '{name}' option(s) {reserved} start "
                            f"with '{RESERVED_PREFIX}', which radar reserves for the "
                            f"context it supplies. Dropped.")
            opts = {k: v for k, v in opts.items()
                    if not str(k).startswith(RESERVED_PREFIX)}
        out[str(name)] = opts
    return out, problems


def _tier_module(name: str):
    """Resolve a registered tier name to the module implementing it, or None.

    Two places, in order:

      1. `tiers/<name>.py` beside this file, with dashes as underscores. A tier
         that needs radar's own internals — the transport, the dispatcher, the
         shared watch defaults — belongs on this side of the preset boundary.
         The MR board is that case: `presets/gitlab/mrs.py` is a GitLab preset
         and the watch preset already depends on it, so putting reconcile
         machinery there would make the dependency mutual.
      2. the script the preset's op declares, read out of the preset itself
         rather than from a second hardcoded table: the op's `cmd` already
         names its script, and a mapping kept alongside it is one that drifts
         the first time a file moves. `gl-runners` joins this way — its report
         needs nothing but its own API helpers.

      3. `<dir>/<name>/tier.py` for any `<dir>` on
         `SUPERTOOL_WATCH_SOURCES_PATH` (#2165) -- the directory a private
         watch source already lives in. Routes 1 and 2 both land inside the
         installed plugin, so a project declaring its op in its own
         `.supertool.json`, with its script in its own tree, could poll a
         population it had no way to put a line about on the board. It reads
         the sources path rather than a second variable of its own because a
         tier and a source for one population are one concern, and two knobs
         for it is one to forget.

         **This imports and executes Python from a caller-supplied path**, on
         the same terms `sourcepath` states for a poller: anyone who can write
         to a directory on that path, or to the `.supertool.json` naming it,
         runs code as you. Nothing here is a sandbox.

    A name resolves to a directory entry or to an op, never to a table, so
    neither route can fall out of step with the files on disk.
    """
    local = _HERE / "tiers" / f"{name.replace('-', '_')}.py"
    if local.is_file():
        return _load(f"radar_tier_{name.replace('-', '_')}", local)

    presets_dir = _HERE.parent
    for preset in sorted(presets_dir.glob("*.json")):
        try:
            with open(preset, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        entry = (data.get("ops") or {}).get(name)
        if not isinstance(entry, dict):
            continue
        for token in str(entry.get("cmd") or "").split():
            if token.endswith(".py"):
                script = presets_dir / token.replace("{path}", "")
                if script.is_file():
                    return _load(f"radar_tier_{name.replace('-', '_')}", script)

    external, _origin = sourcepath.find_tier(name)
    if external is not None:
        return _load(f"radar_tier_{name.replace('-', '_')}", external)
    return None


def _ships_tier(name: str) -> bool:
    """True when radar's own `tiers/` answers to `name`."""
    return (_HERE / "tiers" / f"{name.replace('-', '_')}.py").is_file()


def tier_shadow_lines(names: list[str] | tuple[str, ...]) -> list[str]:
    """Every registered name whose external `tier.py` a shipped tier hides.

    Named, never silently skipped -- `sourcepath.shadowed` one layer up, with
    the same argument: an operator who put a `gl-mrs` tier on their own path
    believes their board is the one rendering, and a resolution that quietly
    answers with the shipped module is absence-read-as-presence wearing a
    plugin loader. The shipped one still wins; what changes is that the swap is
    said out loud.

    Only registered names are checked. Sweeping the search path for every
    `tier.py` on it would report files nobody asked radar to load, which is a
    complaint about somebody's directory rather than about this board.
    """
    resolved = sourcepath.resolve()
    out: list[str] = []
    for name in names:
        if not _ships_tier(name):
            continue
        external, origin = sourcepath.find_tier(name, resolved)
        if external is None:
            continue
        out.append(f"radar: tier '{_untrusted.flat(name, disclose_newline=True)}' in "
                   f"{origin} was NOT loaded -- radar ships a tier of that name "
                   f"and shipped tiers always win.")
    return out


def _tier_search_lines(name: str) -> list[str]:
    """Where a tier of this name was looked for, for the message that says it wasn't there.

    Three routes, and an operator who has just written their first tier can be
    standing in front of any of them. Naming only the config key -- "check the
    name" -- is the absence that does not say where it looked, in the one
    message this feature is met through.
    """
    resolved = sourcepath.resolve()
    lines = [f"       looked in:",
             f"  {_HERE / 'tiers'}/{name.replace('-', '_')}.py (shipped)",
             f"  the script named by an op '{name}' in {_HERE.parent}/*.json"]
    lines.extend(sourcepath.tier_search_report(resolved))
    return [lines[0]] + ["       " + line for line in lines[1:]]


def _spawner() -> tuple[Callable[..., str], dict[str, str], list[str]]:
    """A `_watch` callable, the ledger of every slot it was asked for, and the
    reap that guards the first spawn.

    The tier chooses when to spawn; radar keeps the bound and the record. A
    slot radar refused is a slot nobody is polling, and the ledger is what lets
    it say so on the same run.

    The reap hangs off the first spawn rather than off `main()` (#957). Radar
    heals, and stopping a duplicate is part of healing — but a run that
    established no coverage cannot have contributed a duplicate, and charging it
    for one made *looking* cost the same as acting. That fusion is the thing
    this repository keeps paying for: a tier that raised before it could spawn,
    or a fleet tier that keeps no watchers at all, would still stop somebody's
    process on the way past, disclosed nowhere the caller was looking.

    Still *before* the first spawn, never after: a reap that ran afterwards
    would be judging this run's own new pollers, and a radar that never reaped
    is how #749's 36 processes accumulated behind 18 tracked slots. Once per
    run, however many slots a tier asks for.
    """
    seen: dict[str, str] = {}
    reaped: list[str] = []
    done = False

    def watch(source: str, scope: str, only: list[str] | None = None) -> str:
        nonlocal done
        if not done:
            done = True
            reaped.extend(dispatcher.reap_duplicate_pollers())
        status = ensure_watcher(source, scope, only)
        seen[f"{source}:{scope}"] = status
        return status

    return watch, seen, reaped


def tier_reports(arg: str = "") -> tuple[list[str], bool, list[str]]:
    """(lines, all_healthy, failures) from every registered tier, in order.

    `lines` is the board — stdout. `failures` is the *cannot tell* channel: a
    tier that could not be resolved, or that raised. It is returned apart from
    `lines` rather than folded into them because the two have different
    destinations and different exit codes. A broken tier must not be able to
    cost a working one its board, and it must not be able to leave radar
    exiting 0 either.

    A healthy tier is silent when its `RADAR_QUIET_DEFAULT` says so — true for
    a side concern, false for a tier whose report is the board itself.

    Order is registration order, so an operator who wants the fleet verdict
    above the MR board writes `gl-runners` first. Nothing here sorts: a tier
    that says "the board below may be this, not your code" is making a claim
    about position, and only the person who wrote the config can settle it.
    """
    tiers, lines = read_tiers()
    watch, spawned, reaped = _spawner()
    all_ok = True
    failures: list[str] = []

    lines.extend(tier_shadow_lines(list(tiers)))

    for name, opts in tiers.items():
        try:
            module = _tier_module(name)
        except Exception as exc:  # noqa: BLE001 — an external tier is somebody else's code
            # Route 3 imports Python from a directory the caller named, so
            # *loading* a tier can now fail the way running one already could.
            # Caught here rather than left to propagate: one project's broken
            # tier taking radar down would cost every other tier its board.
            failures.append(f"radar: WARNING — tier '{name}' could not be loaded: "
                            f"{exc.__class__.__name__}: {exc}")
            all_ok = False
            continue
        report = getattr(module, "radar_report", None) if module else None
        if report is None:
            # Two different facts, and they used to arrive in one sentence: a
            # module that loaded and lacks the function, and a name nothing
            # answered to. The second is the one an operator can act on, and it
            # is the one #2165 was filed about, so it says where it looked.
            why = ("could not be resolved" if module is None
                   else "exposes no radar_report()")
            failures.append("\n".join(
                [f"radar: WARNING — tier '{name}' is registered but {why}; it "
                 f"contributes nothing. Check the name."] + _tier_search_lines(name)))
            all_ok = False
            continue

        unknown = set(opts) - set(getattr(module, "RADAR_OPTIONS", set()))
        if unknown:
            lines.append(f"radar: WARNING — tier '{name}' has unknown option(s) "
                         f"{sorted(unknown)}; ignored. Check for a typo.")

        try:
            tier_lines, ok = report({**opts, "_arg": arg, "_watch": watch})
        except Exception as exc:  # noqa: BLE001 — a broken tier must not take radar down
            failures.append(f"radar: WARNING — tier '{name}' failed: "
                            f"{exc.__class__.__name__}: {exc}")
            all_ok = False
            continue

        all_ok = all_ok and ok
        quiet = opts.get("quiet_when_healthy",
                         getattr(module, "RADAR_QUIET_DEFAULT", True))
        if ok and quiet:
            continue
        lines.extend(tier_lines)

    return reaped + watcher_cap_warnings(spawned) + lines, all_ok, failures


def tier_states(arg: str = "") -> tuple[list[str], list[str]]:
    """`(lines, failures)` — every tier's own account of itself, read-only.

    Radar heals, and healing forks processes. That made *looking* at this
    subsystem cost the same as acting on it, and the result was hours of not
    looking (#859). `watches` is read-only about the fleet; this is the same
    guarantee about the tiers.

    Nothing here calls `radar_report`, nothing reaps, nothing spawns. A tier
    exposing no `radar_state` says so rather than rendering as an empty,
    healthy-looking block — a tier with nothing to show and a tier that cannot
    show anything are two different answers.
    """
    tiers, lines = read_tiers()
    failures: list[str] = []
    lines.extend(tier_shadow_lines(list(tiers)))
    for name, opts in tiers.items():
        try:
            module = _tier_module(name)
        except Exception as exc:  # noqa: BLE001 — same reason as `tier_reports`
            failures.append(f"radar: WARNING — tier '{name}' could not be loaded: "
                            f"{exc.__class__.__name__}: {exc}")
            lines.append(f"{name}: UNLOADABLE — {exc.__class__.__name__}: {exc}")
            continue
        if module is None:
            failures.append("\n".join(
                [f"radar: WARNING — tier '{name}' is registered but could not be "
                 f"resolved; it contributes nothing. Check the name."]
                + _tier_search_lines(name)))
            # This view is the one an operator opens *because* the board said
            # nothing, so the directories go in the board half too rather than
            # only on stderr.
            lines.append(f"{name}: UNRESOLVED — no tier module of that name")
            lines.extend(_tier_search_lines(name))
            continue
        lines.append(f"{name}:")
        lines.append(f"  module    : {getattr(module, '__file__', '?')}")
        unknown = sorted(set(opts) - set(getattr(module, "RADAR_OPTIONS", set())))
        if unknown:
            lines.append(f"  UNKNOWN opt: {unknown} — ignored; check for a typo")
        quiet = opts.get("quiet_when_healthy",
                         getattr(module, "RADAR_QUIET_DEFAULT", True))
        lines.append(f"  quiet ok  : {bool(quiet)}")
        state = getattr(module, "radar_state", None)
        if state is None:
            lines.append("  state     : this tier exposes no radar_state(); its "
                         "state can only be seen by running radar, which spawns")
            continue
        try:
            lines.extend(state({**opts, "_arg": arg}))
        except Exception as exc:  # noqa: BLE001 — one broken tier is not the rest
            failures.append(f"radar: WARNING — tier '{name}' radar_state failed: "
                            f"{exc.__class__.__name__}: {exc}")
    return lines, failures


#: Why the header's count is the count it is. Kept off the verdict line so the
#: verdict stays one line, and worded to name the *other* two surfaces rather
#: than to repeat their answers.
_DELIVERY_FOOTNOTE = (
    "counted over the watcher state files, which includes slots whose poller "
    "has since gone; `watches` renders it per watcher and `channel:health` is "
    "the judgement about the socket. Radar neither stops, re-arms nor reaps "
    "anything on the strength of this line."
)


def channel_banner() -> list[str]:
    """The channel this board is a board *of*, above the board (#1495).

    Radar printed `SOCK_PATH` and `STATE_DIR` as bare paths and never the name,
    so a named channel with a stale `SUPERTOOL_WATCH_SOCK` exported rendered as
    a healthy board with no statement that the name was not in force — #1477's
    half-set knob, relocated one surface over.

    `[]` on the default paths with no override, like every other note in this
    preset: a header printed every time is a header nobody reads. Prefixed
    `radar:` because every line radar writes about itself is, so a reader can
    tell the tool's words from a tier's. Read-only, and the resolution comes
    through the one `transport` accessor `watches` uses, so the two boards
    cannot disagree about the same channel.
    """
    return ["radar: " + line
            for line in transport.channel_disclosure() + sourcepath.op_lines("radar")]


def delivery_banner() -> list[str]:
    """The fleet's worst delivery state, above the board. Report-only (#1183).

    Radar's one dangerous power is that it heals: it forks pollers and, on the
    run that spawns, reaps duplicates. **Nothing here feeds either.** This
    function reads state files, counts them and returns text; no poller is
    stopped, restarted or recorded dead because of a delivery verdict, and the
    footnote says so out loud to the operator as well. A board that got a
    healthy poller reaped would be a worse defect than the one #1183 filed —
    #511 is the precedent, where a render that invited a reasonable "these are
    duplicates" reading cost two live watchers on two different MRs.

    Worst-first, with both counts on the line, so a partly stranded fleet is
    rounded neither up to healthy nor down to broken. The delivered case is
    lower-case and the stranded one is not, because only one of them is news.

    An empty fleet returns no lines at all: with no watcher state files there
    is no fleet to misreport, and a header asserting that would be a claim
    about a population of zero.
    """
    rows = transport.delivery_survey()
    if not rows:
        return []
    total = len(rows)
    lost = [row for row in rows if row[2] == transport.EMIT_NO_LISTENER]
    unsure = [row for row in rows if row[2] == transport.EMIT_UNKNOWN]
    took = [row for row in rows if row[2] == transport.EMIT_ACCEPTED]
    if lost:
        head = (f"radar: DELIVERY — {len(lost)} of {total} watcher state file(s) "
                f"record a last emit that found nobody listening on the socket.")
    elif unsure:
        head = (f"radar: DELIVERY — {len(unsure)} of {total} watcher state file(s) "
                f"cannot say whether their last emit reached anyone.")
    elif len(took) == total:
        head = (f"radar: delivery — all {total} watcher state file(s) had their "
                f"last emit accepted by a listener.")
    elif took:
        # Nothing lost and nothing unsettled, but not everything spoke either.
        # `all N accepted` over a fleet where N-1 had never emitted is the
        # absence read as a clean result — the defect #1183 was filed about,
        # one level up, delivered by the fix for it. Both numbers, always.
        head = (f"radar: delivery — {len(took)} of {total} watcher state file(s) "
                f"had their last emit accepted by a listener; the other "
                f"{total - len(took)} have not emitted yet.")
    else:
        head = (f"radar: delivery — no watcher has recorded an emit yet across "
                f"{total} state file(s), so nothing here says whether the socket "
                f"delivers.")
    lines = [head, "       " + _DELIVERY_FOOTNOTE, *_destination_lines(rows)]
    if took:
        # #1543. `accepted` is the ceiling of the producer half: a listener took
        # the bytes. Every arm above that counts one is a claim about a socket,
        # and the incident this was filed from had a healthy socket, a verified
        # consumer and no session subscribed to the channel — so the bytes were
        # taken and then discarded, under a line that reads as delivery.
        lines += _subscription_lines()
    return lines


def _subscription_lines() -> list[str]:
    """Whether a session is subscribed, for the arms that counted an `accepted`.

    Silent on the positive, like `_destination_lines` and for the same reason:
    a line printed every tick is one nobody reads, and this banner's convention
    is that news is shouted. `channel.subscription_for_socket` is the single
    inference path — this board and `channel:health` answering the same
    question two ways is how they came to disagree about one socket.
    """
    sub = channel.subscription_for_socket(transport.SOCK_PATH)
    if sub.state == channel.SUB_SUBSCRIBED:
        return []
    if sub.state == channel.SUB_NOT_SUBSCRIBED:
        head = ("radar: DELIVERY — the consumer on this socket is BOUND, NOT "
                "SUBSCRIBED: the events counted above were read and then "
                "discarded, because no session is subscribed to this channel.")
    else:
        head = ("radar: DELIVERY — whether any session is SUBSCRIBED to this "
                "channel was not established, so nothing above says the events "
                "counted reached one.")
    return [head, *["       " + line.strip() for line in sub.lines]]


def _destination_lines(rows: list[tuple[str, str, str]]) -> list[str]:
    """Whether `accepted` means accepted by the socket *this* session reads.

    The gap #1309 named. A poller captures `SOCK_PATH` at spawn and keeps it
    for life, so a second session that exports `SUPERTOOL_WATCH_SOCK` and
    leaves `SUPERTOOL_WATCH_STATE_DIR` alone shares the first session's poller
    slots: every slot is held, every emit is accepted, and the header above
    said so — about a socket this session has never read a byte from. The
    datum that settles it has been in the state file since #581 and had no
    reader until here.

    Three states, and the third is what keeps the other two honest:

      * a recorded path that is not this process's — those events reached a
        consumer, and it is not the one this session is talking to;
      * a watcher that emitted and recorded no path at all — an older build,
        or a state file that could not be read. Not agreement. Said so;
      * everything recorded here — no line, because agreement is not news and
        a header printed every time is a header nobody reads.

    Scoped to watchers that have actually emitted: one with nothing to report
    has no destination to disagree about, and `DELIVERY_NO_EMIT` is already
    reported one line up. Report-only, like everything else on this route.

    The two surveys are separate scans of the state directory, so a file that
    disappears between them lands in the unrecorded bucket rather than being
    dropped. That is the right side to fall on — the line it produces says
    only that nothing here establishes where that watcher writes, which is
    exactly true of a slot whose file has gone — but it is why this must not
    be read as "these watchers are running an old build".

    A recorded path is somebody else's text and is not verifiable from here
    (#1423). `STATE_DIR` defaults to `/tmp`, so a co-tenant can plant a state
    file, and a `sock_path` holding a newline used to print a whole extra line
    at column 0 — a forged `delivery — all N accepted`, which is the
    false-clean claim this banner exists to prevent. Two answers, because
    flattening alone would leave radar asserting a path it cannot check:

      * `_untrusted.flat` on every path, so nothing from a state file reaches
        column 0 where the tool speaks. Flattened *before* the set, not after:
        de-duplicating raw values leaves two entries that render identically;
      * the provenance said out loud above the line, once, the way
        `list_watchers` and `channel` say it over the same directory. `repr`
        would be the wrong trade on a line whose job is to tell an operator
        which socket to go and look at.
    """
    emitted = [(source, wid) for source, wid, state in rows
               if state != transport.DELIVERY_NO_EMIT]
    if not emitted:
        return []
    recorded = {(source, wid): path
                for source, wid, path in transport.emit_destinations()}
    # The comparison is against the raw value; only the render is flattened.
    here = _untrusted.flat(transport.SOCK_PATH)
    elsewhere = [slot for slot in emitted
                 if recorded.get(slot, "") not in ("", transport.SOCK_PATH)]
    unrecorded = [slot for slot in emitted if not recorded.get(slot, "")]
    out: list[str] = []
    if elsewhere:
        others = sorted({_untrusted.flat(recorded[slot]) for slot in elsewhere})
        # Above the line it is about, like every other note in this preset: the
        # reader this protects is the one who acts on the first thing they read.
        # Only on the arm that prints somebody else's text — a note on a line
        # radar wrote itself would be a claim about the render, not the source.
        out.append("       " + _untrusted.flat_note(
            "the socket path(s)", "the watchers' own state files"))
        out.append(
            f"radar: DELIVERY — {len(elsewhere)} of {len(emitted)} watcher "
            f"state file(s) that emitted last wrote to a socket this session "
            f"does not read: {', '.join(others)}. This session reads {here}, "
            f"so those events reached a consumer that is not this one.")
    if unrecorded:
        # Upper-case, like the `unsure` arm above and for its reason: the
        # convention on this banner is that news is shouted, and an emit whose
        # destination cannot be established is unresolved rather than fine.
        out.append(
            f"radar: DELIVERY — {len(unrecorded)} of {len(emitted)} watcher "
            f"state file(s) that emitted do not record which socket they "
            f"wrote to, so nothing here says whether they reach {here}.")
    return out


def state_main(arg: str = "") -> int:
    tiers, complaints = read_tiers()
    if not tiers:
        for line in complaints:
            print(line, file=sys.stderr)
        print(NO_TIERS, file=sys.stderr)
        return 1
    lines, failures = tier_states(arg)
    # Above the tier blocks: the reader this protects is the one who acts on the
    # first thing they read. Read-only, like everything else on this route.
    # The channel first, because it says which fleet the delivery banner counted.
    banner = channel_banner() + delivery_banner()
    if banner:
        print("\n".join(banner))
    if lines:
        print("\n".join(lines))
    for line in failures:
        print(line, file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = [a for a in (argv or [])[1:] if a]
    if args and args[0] == "--state":
        return state_main(args[1].strip() if len(args) > 1 else "")

    arg = argv[1].strip() if argv and len(argv) > 1 and argv[1] else ""

    tiers, complaints = read_tiers()
    if not tiers:
        for line in complaints:
            print(line, file=sys.stderr)
        print(NO_TIERS, file=sys.stderr)
        return 1

    # The reap lives in `_spawner`, guarding the first spawn of this run — not
    # here, where it ran on every invocation including the ones that spawned
    # nothing at all (#957). See `_spawner` for the bound and `dispatcher.
    # reap_duplicate_pollers` for what it may act on.
    lines, _all_ok, failures = tier_reports(arg)
    for line in failures:
        print(line, file=sys.stderr)
    banner = channel_banner() + delivery_banner()
    if banner:
        print("\n".join(banner))
    if lines:
        print("\n".join(lines))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
