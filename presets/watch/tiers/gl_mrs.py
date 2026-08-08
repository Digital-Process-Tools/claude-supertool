#!/usr/bin/env python3
"""gl-mrs — the GitLab merge-request board, as a radar tier.

This was radar itself until #528. Radar started life as an MR tool and the
accident got promoted to a principle; the board is now one tier among any
number, registered by name like the rest:

    {"ops": {"radar": {"radar_tiers": {"gl-mrs": {}}}}}

Nothing below changed shape in the move. What changed is who owns it: the
snapshot, the exclusions, the feed and the per-MR heal are this tier's
business, and someone who never registers it pays for none of them.

`watches` reports that pollers are alive. It cannot report what is true, and
the two diverge routinely:

    last_event    : pipeline_failed  on pipeline 154177
    source_state  : running          on pipeline 154180

`source_state` is the truth; `last_event` is history. A board built on
`last_event` calls that MR broken when it is mid-retry on a newer pipeline.

Events also cannot survive a session boundary. The transport is
fire-and-forget, so an event emitted with no listener is gone permanently;
pollers are processes that die with the machine; and pollers stop themselves
on terminal state by design. At the start of a session an event-driven view
therefore knows nothing — and "knows nothing" renders identically to
"everything is green". That is the failure this tier exists to remove, so state
is the floor and events are the optimisation, not the other way round.

Hence a reconcile, not a printer:

  1. live truth   one gl-mrs query for open MRs — authoritative. The state
                  files are cache and may be absent or hours stale.
  2. reconcile    prune state files whose watcher reached a terminal state;
                  flag drift where the last event fired on an older pipeline
                  than the one now running.
  3. heal         respawn a watcher for every open MR with no live poller.
                  Covers reboot, crash, a cleared /tmp, and MRs that were
                  green when watchers were last spawned — in one step.
  3b. feed        ensure one live gitlab-mr-feed poller. Reconcile is a
                  snapshot; the feed is the thing that keeps discovering after
                  radar returns, so a session idle for an hour still learns
                  about an MR opened fifty minutes ago. Its absence is
                  reported loudly, because a feed that is not running looks
                  exactly like a day on which nothing happened.

                  #528 reframed that argument rather than dropping it. The
                  discovery guarantee is an argument about *this tier's*
                  internal correctness, not radar's — a reader who never
                  registers gl-mrs does not need it. It still has to work.
  4. report       full board on cold start, delta-only afterwards.

The population is an argument, in the same filter vocabulary as `gl-mrs`:

    radar                            defaults.DEFAULT_FILTER
    radar:author=modular.system      what the automated agent opened
    radar:author=@me,author=x        two queries, unioned by iid

It arrives as `options["_arg"]`, radar's raw invocation argument.

One filter, one population, one board. `live_open_mrs`, `heal` and the feed
poller are three views of a single resolved filter and are never derived from
different ones — a board that omits MRs it is actively receiving events for
renders exactly like a board where those MRs are fine, which is the same
silent incompleteness this tier exists to remove.

That filter lives for one invocation and is not persisted, so a session that
widened the board and then runs a bare `radar` silently gets the default
population back (#486). The footer therefore names the scope on every board,
the default one included: "no label" used to spell both "this is the default"
and "nobody said". A feed poller still live on another scope is named too —
changing the filter respawns the feed without retiring the old one, so
effective scope splits between what this call passed and what a still-running
watcher was started with. It is reported, not killed: two populations at once
is legitimate (see `prune_terminal`), and only the reader can say which one
they meant. What this deliberately does not give is continuity — re-widening
still means re-typing the filter, because a population read from a file
nobody in the session chose is the hidden state that caused the split.

The snapshot is keyed by that filter for the same reason. Two populations
sharing one snapshot file would report every MR of the first as new and every
MR of the second as gone; a lying delta is worse than no delta.

Idempotent, so it is safe on every session start and on a loop. That includes
the feed: radar's `_watch` short-circuits on a live PID, so N radar runs still
leave one feed poller and one copy of every discovery event.

"Nothing moved" means: the set of open MRs is unchanged, no MR changed
pipeline status / pipeline id / draft / conflict flag, and this tier itself
took no action (nothing pruned, nothing healed, no drift). Standing failures
and conflicts are re-printed even when unchanged — an unfixed red is a current
fact, not history. When nothing moved the board still prints one summary line
rather than nothing at all: total silence is indistinguishable from a radar
that failed to run. That is why `RADAR_QUIET_DEFAULT` is False here while it
is True for a side concern like the runner fleet.

Standing exclusions
-------------------

Some MRs are red for a reason nobody intends to fix soon, and re-printing
that row every run is how a reader learns to skim a board — which is how a
real red gets missed. `ops.radar.radar_exclusions` in `.supertool.json` moves
that suppression out of the reader's memory and into the tool.

An exclusion is the one thing in this tier that can hide a failure, so it is
built to be the *opposite* of a silent omission at four points:

  accounted   the row goes, the MR does not. The footer carries an
              `N excluded` token and one line names each suppressed MR, its
              current status and the configured reason. Tallies describe the
              board that was printed, so `1 failing` is never a count with no
              row behind it.
  reasoned    an exclusion with no reason is refused and the row renders. The
              field that makes it auditable is the field that makes it work.
  self-expiring
              an exclusion only ever suppresses a *standing problem*. The
              moment the MR goes green and unconflicted the suppression lifts
              by itself and the board says the reason is spent — so an
              exclusion cannot outlive what it was written for. An optional
              `until` date expires it on a schedule as well.
  board-only  an exclusion is a statement about one row, not a change of
              population. The watcher fleet, the feed and the event filter are
              untouched, deliberately breaking the one-filter symmetry: the
              filter says what this tier is responsible for, and it does not
              stop being responsible for an MR because its row is noisy. The
              watcher is also the only thing that can still report a push
              while the row is suppressed, which is exactly when an exclusion
              is most likely to have gone stale.

Every unanswerable case — unparseable config, unknown key shape, an iid that
is not in the population — resolves to *show the row*.
"""
from __future__ import annotations

import datetime
import glob
import hashlib
import importlib.util
import json
import os
import sys
from typing import Any

from pathlib import Path

_HERE = Path(__file__).parent
_WATCH = _HERE.parent

sys.path.insert(0, str(_WATCH))
import defaults  # noqa: E402
import dispatcher  # noqa: E402
import transport  # noqa: E402

sys.path.insert(0, str(_WATCH.parent))
import _filter_tokens  # noqa: E402  (the one tokenizer the boards share)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mrs = _load("radar_gitlab_mrs", _WATCH.parent / "gitlab" / "mrs.py")
# The snapshot store, shared with every other tier since #859: keeping a
# previous board keyed by the population it describes is not a GitLab argument,
# and a second copy of it in the GitHub tier is how a fixed defect comes back.
snapshot = _load("radar_snapshot", _HERE / "_snapshot.py")

SOURCE = defaults.DEFAULT_SOURCE
FEED_SOURCE = defaults.DEFAULT_FEED_SOURCE
FEED_SCOPE = defaults.DEFAULT_FEED_SCOPE
SNAPSHOT_PREFIX = "supertool-radar"

# ops.radar.radar_exclusions in .supertool.json, JSON-encoded into the env by
# the op runner — the same route ops.gl-job.job_patterns takes.
EXCLUSIONS_ENV = "SUPERTOOL_RADAR_EXCLUSIONS"

_FIX_HINT = "remove it from ops.radar.radar_exclusions in .supertool.json"

FEED_LABEL = {"alive": "feed ok", "spawned": "feed respawned", "failed": "feed DOWN",
              "capped": "feed DOWN (respawn capped)"}

# Config keys this tier understands. `quiet_when_healthy` is accepted for
# symmetry with every other tier and defaults to False below — see
# RADAR_QUIET_DEFAULT.
RADAR_OPTIONS = {"quiet_when_healthy", "stale_running_minutes"}

# An in-progress MR whose reported facts have not moved for this long comes
# back onto the delta board with the reason on the row (#1025). Same number and
# same reasoning as `gh_prs.STALE_RUNNING_MINUTES`; 0 turns it off.
STALE_RUNNING_MINUTES = 240

# The pipeline words that can persist indefinitely *while being wrong*. Both,
# not just `running`: a runner that never picks the job up leaves the pipeline
# at `pending`, and that is the exact symptom #1025 was filed about. Every
# other word is either terminal or a state a human is deliberately sitting on.
IN_PROGRESS_PIPELINES = frozenset({"running", "pending"})

# A healthy MR board still speaks. Silence is what this tier exists to remove:
# a board that prints nothing on a quiet day is byte-identical to a radar that
# failed to run, which is #486 with the failure moved one level up.
RADAR_QUIET_DEFAULT = False

# Filter keys this tier can actually apply, and they are its own — deliberately
# neither the op's set nor the GitHub tier's (#961).
#
# Narrower than `gl-mrs`: `per=` is a page size `live_open_mrs` reads from
# config and never from the arg, so accepting it would drop it in silence one
# level down.
#
# Wider than `tiers/gh_prs.py`: `glab mr list` has `--milestone`,
# `--source-branch` and `--target-branch` and `gh pr list` has none of them.
# Copying the GitHub tier's vocabulary would refuse three filters this tier can
# honour — the opposite error, and just as wrong.
KNOWN_FILTERS = set(mrs._FILTER_FLAG) | {"state"}

# Tokens that are flags rather than key=value — and this tier takes none of
# them (#973). `iids` and `failed` are board *shapes* the op offers and this
# tier does not: a radar board silently narrowed to a bare id list, or to only
# the failing rows, is the same lie as a widened one — and `iids` is the payload
# the feed hands the watcher spawner.
#
# `nopipe` was accepted and never reached `live_open_mrs`, so the board came
# back pipeline-enriched and the caller who asked for a cheaper one was not
# told they had not got it. Refused rather than honoured, and the argument is
# not the GitHub tier's: here it *is* expressible, and what it would produce is
# not a cheaper board but a board with no answer in it. The verdict, the drift
# check against `source_state.pipeline_id` and the heal decision are all read
# off the enrichment `nopipe` removes.
KNOWN_FLAGS: set[str] = set()

# Keys whose value this tier maps rather than forwards. `state=mergd` is in
# KNOWN_FILTERS, so it survives the unknown-token check — and then
# `_build_list_cmd` finds no entry in `_STATE_FLAG`, emits no flag, and glab
# answers with its default, `opened`. The merged board renders as the open one
# and radar heals watchers onto it.
VALUE_DOMAINS: dict[str, object] = {"state": mrs._STATES}


class RadarError(RuntimeError):
    """Live GitLab could not be reached. Never degrade to 'all green'."""


def _parse(arg: str) -> dict[str, list[str]]:
    """Tokenise against *this tier's* vocabulary, or raise. See `resolve_filter`."""
    multi, _flags, unknown = _filter_tokens.parse_multi(
        arg, KNOWN_FILTERS, KNOWN_FLAGS)
    if unknown:
        raise RadarError(
            "radar: gl-mrs tier " + _filter_tokens.unknown_error(
                unknown, KNOWN_FILTERS, KNOWN_FLAGS)
            + " Radar does not just print this population, it watches it: an "
              "unapplied token widens the scope, and the fleet then spawns over "
              "MRs nobody asked about."
        )
    bad = [b for key, vals in multi.items() for v in vals
           for b in _filter_tokens.bad_values({key: v}, VALUE_DOMAINS)]
    if bad:
        raise RadarError("radar: gl-mrs tier " + _filter_tokens.value_error(bad))
    return multi


def default_filter() -> dict[str, list[str]]:
    """The population a bare `radar` covers, read from defaults.py.

    Not a literal here: the whole point of that module is that the shell
    supervisor and this tier cannot describe different populations. Parsed
    through the same check as a typed arg, so a `DEFAULT_FILTER` this tier
    could not honour is loud rather than quietly reduced.
    """
    return _parse(defaults.DEFAULT_FILTER)


def resolve_filter(arg: str = "") -> dict[str, list[str]]:
    """The one filter every other step reads. Tier vocabulary, or `RadarError`.

    A key may repeat — `author=@me,author=modular.system` — because GitLab
    takes one author per query and the union is the population the caller
    described.

    Refusing is the whole point (#961). This used to read
    `mrs._parse_multi(arg)[0]`, discarding the tokenizer's third element — the
    tokens it could not place — so an unapplied token widened the population
    two different ways and said nothing about either:

        radar:milestne=x            -> {} -> `multi or default_filter()`, i.e.
                                       every open MR of mine.
        radar:author=@me,milestne=x -> {"author": ["@me"]}, labelled
                                       `scope author=@me` — a scope line true
                                       about the query and false about the
                                       question.

    Worse here than at the op level, which is why it is refused before anything
    else runs. `gh-prs` printing an unfiltered board wastes a read; this tier
    resolves a *population* and then spawns over it — `heal()` starts a per-MR
    watcher per iid and `feed_scope()` names the discovery feed, so a widened
    scope fires `mr_opened` for strangers' MRs. The mr-feed poller already
    declines exactly this by returning `None` (#939).

    **Refuse, not decline.** The poller returns `None` because it has no reader:
    it runs in a loop and its only channel is the events it emits, so the safe
    answer is to emit nothing. A tier has a reader, and radar's `tier_reports`
    already isolates one — a `RadarError` lands in the `failures` channel
    (stderr, exit 1) while every other tier still renders its board. So refusal
    here costs nothing that declining would have saved and buys the one thing
    an unattended run needs: somebody finds out. `radar_state` is the exception
    and says `REFUSED` in place, because a read-only view that raises is the
    view you cannot open.
    """
    arg = (arg or "").strip()
    multi = _parse(arg) if arg else {}
    return multi or default_filter()


def filter_string(multi: dict[str, list[str]]) -> str:
    """The filter back in gl-mrs arg form — what the user typed, normalised."""
    return ",".join(f"{k}={v}" for k, vals in multi.items() for v in vals)


def canonical_filter_string(multi: dict[str, list[str]]) -> str:
    """The filter in one fixed spelling — sorted keys, sorted values, deduped.

    Used where the filter string is an *identity*, not a display: the feed
    watcher id is the pid filename, so `author=a,author=b` and
    `author=b,author=a` would otherwise be two pollers over one population,
    i.e. two copies of every mr_opened (#476).

    Safe as an identity key in the direction that matters. It only merges
    filters that are already the same set, so it can never refuse to start a
    poller for a filter that would have selected something different — the
    failure that would show up as a watcher silently not existing.
    """
    return ",".join(f"{k}={v}"
                    for k, vals in sorted(multi.items())
                    for v in sorted(set(vals)))


def filter_key(multi: dict[str, list[str]]) -> str:
    """Stable short hash of the filter, insensitive to key and value order.

    `author=a,author=b` and `author=b,author=a` are the same population and
    must share a snapshot; `author=a` and `author=b` must not.

    The hashing itself is `_snapshot.key`; what stays here is the *filter*
    normalisation, which is GitLab-shaped (one key may repeat, because the list
    endpoint takes one author per query).
    """
    return snapshot.key({k: sorted(set(v)) for k, v in sorted(multi.items())})


def _snapshot_path(multi: dict[str, list[str]]) -> str:
    return snapshot.path(SNAPSHOT_PREFIX, filter_key(multi))


# ---------------------------------------------------------------------------
# 1. live truth
# ---------------------------------------------------------------------------

def _query(filters: dict[str, str], per_page: int) -> list[dict]:
    """One `glab mr list`. RadarError on any failure, never an empty list.

    A partial union is a board that is quietly missing rows, so one failing
    query fails the whole reconcile — an unreachable GitLab must never render
    as an MR that is fine.
    """
    try:
        result = mrs._run(mrs._build_list_cmd(filters, per_page))
    except Exception as exc:  # noqa: BLE001 — surfaced as RadarError
        raise RadarError(f"glab mr list failed: {exc}") from exc
    if result.returncode != 0:
        err = (result.stderr or "").strip() or "unknown error"
        if "not logged in" in err.lower() or "401" in err:
            raise RadarError("glab not authenticated. Run: glab auth login")
        raise RadarError(f"glab mr list: {err}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError("could not parse glab JSON output") from exc
    if not isinstance(data, list):
        raise RadarError("glab returned no MR list")
    return data


def live_open_mrs(multi: dict[str, list[str]] | None = None) -> list[dict]:
    """Every open MR the filter describes, pipeline-enriched.

    One gl-mrs query per value combination, unioned by iid — the list endpoint
    takes a single author, so two authors is two calls. Enrichment runs once
    over the union, so an MR both queries return is enriched once.
    """
    multi = default_filter() if multi is None else multi
    cfg = mrs._get_config()
    merged: dict[str, dict] = {}
    for filters in mrs._expand_filters(multi):
        for m in _query(filters, cfg["per_page"]):
            if isinstance(m, dict) and m.get("iid") is not None:
                merged.setdefault(str(m["iid"]), m)
    data = list(merged.values())
    mrs._enrich(data, cfg["enrich_cap"], cfg["enrich_workers"])
    return data


# ---------------------------------------------------------------------------
# 2. reconcile
# ---------------------------------------------------------------------------

def read_state_files() -> dict[str, dict]:
    """{iid: full state file} for this source, from the state-file cache."""
    prefix = f"supertool-watch-{SOURCE}__"
    suffix = ".state.json"
    out: dict[str, dict] = {}
    pattern = os.path.join(transport.STATE_DIR, f"{prefix}*{suffix}")
    for path in sorted(glob.glob(pattern)):
        iid = os.path.basename(path)[len(prefix):-len(suffix)]
        if not iid:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            out[iid] = loaded
    return out


def prune_terminal(states: dict[str, dict], watched: set[str]) -> list[str]:
    """Delete state files whose watcher reached a terminal state (item 4).

    A file owned by a live poller is left alone — the poller will clear it
    itself when it stops. Only MRs whose live state is terminal are pruned;
    "absent from my open list" is deliberately not a prune trigger, because a
    watcher may legitimately follow an MR this tier's filter never returns —
    a second radar over another population is exactly that case.
    """
    poller = dispatcher._load_source(SOURCE)
    is_terminal = getattr(poller, "is_terminal", None) if poller else None
    if is_terminal is None:
        return []
    pruned: list[str] = []
    for iid, full in states.items():
        if iid in watched:
            continue
        if is_terminal(full.get("source_state") or {}):
            if transport.clear_state(SOURCE, iid):
                pruned.append(iid)
    return pruned


def drift(states: dict[str, dict]) -> dict[str, tuple[str, str]]:
    """{iid: (event_pipeline_id, current_pipeline_id)} where they disagree.

    A newer pipeline superseded the last event, so the event is stale history.
    Reported rather than silently resolved — the divergence is the signal.
    """
    out: dict[str, tuple[str, str]] = {}
    for iid, full in states.items():
        event_pipe = str(((full.get("last_event") or {}).get("payload") or {}).get("pipeline_id") or "")
        live_pipe = str((full.get("source_state") or {}).get("pipeline_id") or "")
        if event_pipe and live_pipe and event_pipe != live_pipe:
            out[iid] = (event_pipe, live_pipe)
    return out


# ---------------------------------------------------------------------------
# 3. heal
# ---------------------------------------------------------------------------

def heal(open_iids: list[str], watched: set[str]) -> tuple[list[str], list[str], list[str]]:
    """Respawn a watcher for every open MR without a live poller.

    Returns (healed, still_uncovered, refused); `refused` is a subset of
    `still_uncovered`. The event filter comes from defaults.py
    so a healed watcher is identical to one watch-mine.sh would have spawned.

    Spawning goes through `dispatcher.start_poller`, the one door, for the
    reason #476 gives: `watched` is derived from pidfiles a grandchild
    publishes after a fork, an import and a detach, so a caller that reads it
    and then forks is testing a slot it cannot see being filled. Radar runs on
    a loop from more than one place, and item 1 of #417 widened this tier from
    "the MRs that are already red" to "every open MR" — so this is the tier
    where losing that race duplicates the whole fleet rather than one poller.

    A slot already held is neither healed nor uncovered: nothing spawned it
    here, so claiming the action would be false, but the MR *is* covered, and a
    spurious "unwatched" warning corrodes the only signal on the board that
    has to be trusted absolutely.

    Healing is bounded, and the bound is #513's substance. A gap left by a
    poller that died is reaped first, so the death is on record before the
    claim overwrites the evidence; past `transport.DEATH_RESPAWN_LIMIT` deaths
    the slot is refused rather than respawned. Respawning forever would keep
    the board green while a watcher failed over and over — a visible failure
    converted into an invisible loop, which is the same bug one level up. A
    refused slot is reported as uncovered, because it is: the automation has
    stopped, and the only thing worse than saying so is not saying so.
    """
    gaps = [iid for iid in open_iids if iid not in watched]
    if not gaps:
        return [], [], []
    if dispatcher._load_source(SOURCE) is None:
        return [], gaps, []
    only = [e for e in defaults.DEFAULT_ONLY.split(",") if e]
    healed: list[str] = []
    failed: list[str] = []
    refused: list[str] = []
    for iid in gaps:
        transport.reap_dead_pidfile(SOURCE, iid)
        if len(transport.deaths(SOURCE, iid)) >= transport.DEATH_RESPAWN_LIMIT:
            refused.append(iid)
            continue
        status, _pid = dispatcher.start_poller(SOURCE, iid, only)
        if status == "spawned":
            healed.append(iid)
        elif status in ("failed", "unclaimable"):
            # `unclaimable` is not "healed" and it is not silence: the gap this
            # tier set out to close is still open and the operator has to be
            # told, the same as for an outright failed spawn (#693).
            failed.append(iid)
    return healed, failed + refused, refused


def loss_warnings(healed: list[str], refused: list[str]) -> list[str]:
    """What the board says about watchers it lost, and about ones it gave up on.

    Two lines, two different actions. A healed loss is reported once, on the
    run that healed it, and then goes quiet — a permanent mark on a slot that
    is now covered is what trains a reader to skim. A refusal is reported on
    every run until an operator re-arms or acknowledges it, because the MR is
    genuinely unwatched for as long as it stands.
    """
    out: list[str] = []
    for iid in refused:
        n = len(transport.deaths(SOURCE, iid))
        out.append(f"radar: WARNING — !{iid} has lost its poller {n} times; "
                   f"NOT respawning. This MR is unwatched until the cause is "
                   f"fixed and it is re-armed: "
                   f"./supertool 'watch:{SOURCE}:{iid}'.")
    for iid in healed:
        recorded = transport.deaths(SOURCE, iid)
        if not recorded:
            continue
        last = recorded[-1].get("pid", "?")
        out.append(f"radar: NOTE — !{iid} lost its poller (PID {last} died "
                   f"without being unwatched, {len(recorded)} recorded); "
                   f"respawned.")
    return out


# ---------------------------------------------------------------------------
# 3b. feed — the part that discovers MRs this tier has never seen
# ---------------------------------------------------------------------------

def feed_scope(multi: dict[str, list[str]] | None = None) -> str:
    """The feed watcher id covering this population.

    The feed source accepts either one of its aliases or a literal gl-mrs
    filter string as its id, so the board's filter reaches the discovery feed
    unchanged. An alias is preferred when it expands to the same filter: the
    id is the pid filename, so `@me` and `author=@me,state=opened` would
    otherwise be two pollers over one population, i.e. two copies of every
    mr_opened. For the same reason the fallback is the canonical spelling of
    the filter rather than the caller's: key order is not identity (#476).
    """
    multi = default_filter() if multi is None else multi
    poller = dispatcher._load_source(FEED_SOURCE)
    aliases = getattr(poller, "ALIASES", None) or {}
    for alias, expansion in aliases.items():
        if mrs._parse_multi(expansion)[0] == multi:
            return alias
    return canonical_filter_string(multi)


def feed_pid(scope: str = FEED_SCOPE) -> int:
    """PID recorded for the feed poller, or 0 when there is no readable file."""
    return transport.read_pid(FEED_SOURCE, scope)


def feed_only() -> list[str]:
    """The event filter a feed poller is started with, from defaults.py."""
    return [e for e in defaults.DEFAULT_FEED_ONLY.split(",") if e]


def other_feed_scopes(scope: str = FEED_SCOPE) -> list[str]:
    """Live feed pollers covering a population other than this board's.

    Changing the filter respawns the feed; the previous one is not retired, so
    a machine can carry a feed started with `author=@me,author=x` while the
    current invocation resolved plain `author=@me`. Effective scope is then
    split between what this call passed and what a still-running watcher was
    started with, and neither half is the whole — a board that reports its own
    half as the answer is narrower than the reader believes it is.

    Read-only: pid files only, nothing is spawned and nothing is killed. Two
    scopes are a legitimate arrangement (`prune_terminal` says as much about
    per-MR watchers), so this reports the split rather than resolving it.
    """
    return sorted({
        str(row.get("id") or "")
        for row in transport.list_active_pids()
        if row.get("source") == FEED_SOURCE and str(row.get("id") or "") != scope
    } - {""})


def feed_error(scope: str = FEED_SCOPE) -> str:
    """Last error the feed poller recorded, or "" when it is polling cleanly.

    A feed that is alive but erroring every tick discovers nothing while
    looking healthy in `watches` — the same silence as a dead one, so it gets
    the same report. The dispatcher clears this key on a successful poll, so a
    message here is current rather than a scar.
    """
    state = transport.read_state(FEED_SOURCE, scope)
    return str((state.get("last_error") or {}).get("message") or "")


# ---------------------------------------------------------------------------
# 4. report
# ---------------------------------------------------------------------------

def _snap_entry(m: dict) -> dict[str, Any]:
    """The facts this tier reports about one MR. Delta is computed over these."""
    return {
        "pipeline": str(m.get("_pipeline") or ""),
        "pipeline_id": str(m.get("_pipeline_id") or ""),
        "draft": bool(m.get("draft")),
        # "conflict" | "empty" | "" — see mrs._conflict_label. This key held a
        # bool before #471, so the first run after upgrading reads every row
        # with a stored `false` as moved and prints a full board once.
        "conflict": mrs._conflict_label(m),
    }


def read_snapshot(multi: dict[str, list[str]] | None = None) -> dict[str, Any] | None:
    """Previous board for this filter, or None on cold start.

    Keyed by filter: comparing one population against another's snapshot
    reports every row of each as a change, which is a delta column that lies.
    """
    multi = default_filter() if multi is None else multi
    return snapshot.read(SNAPSHOT_PREFIX, filter_key(multi), "mrs")


def write_snapshot(entries: dict[str, dict],
                   multi: dict[str, list[str]] | None = None) -> None:
    snapshot.write(SNAPSHOT_PREFIX,
                   filter_key(default_filter() if multi is None else multi),
                   entries, "mrs")


def _departed(previous: dict[str, Any] | None,
              open_mrs: list[dict]) -> list[str]:
    """Iids in the previous snapshot and not in the live population.

    Against the whole population, never the printed board: an exclusion removes
    a row and not a member. Shared with `radar_report`, which needs the same
    answer for `healthy` (#1024).
    """
    prev_entries: dict[str, Any] = (previous or {}).get("mrs", {}) or {}
    live = {str(m.get("iid")) for m in open_mrs}
    return [i for i in prev_entries if i not in live]


def _marks(iid: str, drifted: dict[str, tuple[str, str]],
           healed: set[str], uncovered: set[str],
           stale_minutes: float = 0.0, stale_state: str = "") -> str:
    """The two novel signals, appended to the shared gl-mrs row format."""
    out = []
    if iid in drifted:
        was, now = drifted[iid]
        out.append(f"[drift: {was}→{now}]")
    if stale_minutes:
        out.append(f"[{snapshot.unchanged_label(stale_minutes, stale_state)}]")
    if iid in healed:
        out.append("[healed]")
    elif iid in uncovered:
        out.append("[unwatched]")
    return ("  " + " ".join(out)) if out else ""


def _is_standing_problem(m: dict) -> bool:
    """Unresolved red or conflict — a current fact, so never delta-suppressed."""
    return bool(_problem_label(m))


def _stale_running(m: dict, previous_entry: Any, threshold: float,
                   now: str | None = None) -> float:
    """Minutes an in-progress MR has been unchanged, past `threshold`. Else 0.

    The same omission `gh_prs._stale_running` documents, in the same predicate
    one file over (#1025). An in-progress pipeline is correctly not a standing
    problem — it is the ordinary state of an MR that was just pushed — and it
    is also the only state that can sit still forever while being wrong. So the
    elision is kept and given an expiry.

    `None` from `unchanged_minutes` is unknown, and unknown is not stale.
    """
    if threshold <= 0:
        return 0.0
    if str(m.get("_pipeline") or "") not in IN_PROGRESS_PIPELINES:
        return 0.0
    mins = snapshot.unchanged_minutes(previous_entry, now)
    if mins is None or mins < threshold:
        return 0.0
    return mins


def _problem_label(m: dict) -> str:
    """"failed", "conflict", "empty", "failed+conflict", … or "" when fine.

    Printed on the exclusion line, so a suppressed MR that picks up a second
    problem changes what the board says about it without un-suppressing it.

    An empty MR stays a standing problem, only under its own name: it is still
    unmergeable, and dropping it out of the set would trade #471's mislabel
    for a silent omission — the worse of the two (#445/#454/#414).
    """
    bits = []
    if str(m.get("_pipeline") or "") == "failed":
        bits.append("failed")
    blocked = mrs._conflict_label(m)
    if blocked:
        bits.append(blocked)
    return "+".join(bits)


# ---------------------------------------------------------------------------
# standing exclusions
# ---------------------------------------------------------------------------

def _refused(iid: str, why: str) -> str:
    return (f"radar: exclusion !{iid} REFUSED — {why}. The row is shown; "
            f"fix ops.radar.radar_exclusions in .supertool.json")


def _not_applied(iid: str, why: str) -> str:
    return f"radar: exclusion !{iid} NOT applied — {why}; {_FIX_HINT}"


def read_exclusions(raw: str | None = None) -> tuple[dict[str, dict[str, str]], list[str]]:
    """({iid: {"reason", "until"}}, complaints) from the configured JSON.

    Shapes accepted, both keyed by iid: a bare reason string, or an object
    with `reason` and optional `until` (ISO date). A reason is mandatory —
    an exclusion nobody had to justify is the one that becomes a permanent
    blind spot, and refusing it costs a line of output instead of a red.

    Nothing here raises and nothing here defaults to suppression: a config
    this function cannot understand yields no exclusions plus a complaint,
    which renders as the ordinary board it was trying to trim.
    """
    raw = os.environ.get(EXCLUSIONS_ENV, "") if raw is None else raw
    raw = raw.strip()
    if not raw:
        return {}, []
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, [f"radar: WARNING — radar_exclusions is not valid JSON ({exc.msg}). "
                    f"No exclusions applied."]
    if not isinstance(loaded, dict):
        return {}, ["radar: WARNING — radar_exclusions must be an object keyed by MR "
                    "iid. No exclusions applied."]

    out: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for key, spec in loaded.items():
        iid = str(key).strip().lstrip("!")
        if isinstance(spec, str):
            spec = {"reason": spec}
        if not isinstance(spec, dict):
            problems.append(_refused(iid, "it is neither a reason string nor an object"))
            continue
        if not iid.isdigit():
            problems.append(_refused(iid, "the key is not an MR iid"))
            continue
        reason = str(spec.get("reason") or "").strip()
        if not reason:
            problems.append(_refused(iid, "it carries no reason"))
            continue
        out[iid] = {"reason": reason, "until": str(spec.get("until") or "").strip()}
    return out, problems


def resolve_exclusions(open_mrs: list[dict], exclusions: dict[str, dict[str, str]],
                       covered: set[str], today: str = "") -> tuple[set[str], list[str]]:
    """(suppressed iids, one accounting line per configured exclusion).

    Matching is an exact iid lookup, never a prefix: excluding 1950 must not
    swallow 19509.

    An exclusion is honoured only while it is still true — the MR is in the
    population, not past its `until`, and actually carrying a standing
    problem. Each of the three failures prints the reason it did not apply
    rather than silently doing nothing, because a standing exclusion that
    stopped suppressing anything is dead config that will suppress something
    the day that iid comes back.
    """
    if not exclusions:
        return set(), []
    today = today or datetime.date.today().isoformat()
    by_iid = {str(m.get("iid")): m for m in open_mrs if m.get("iid") is not None}

    suppressed: set[str] = set()
    lines: list[str] = []
    for iid in sorted(exclusions):
        spec = exclusions[iid]
        m = by_iid.get(iid)
        if m is None:
            lines.append(_not_applied(iid, "no open MR with that iid in this population"))
            continue
        until = spec["until"]
        if until and until < today:
            lines.append(_not_applied(iid, f"expired {until}"))
            continue
        label = _problem_label(m)
        if not label:
            pipe = str(m.get("_pipeline") or "none")
            lines.append(_not_applied(
                iid, f"pipeline is '{pipe}' and there is no conflict, so the "
                     f"reason is spent"))
            continue
        suppressed.add(iid)
        cover = "still watched" if iid in covered else "UNWATCHED"
        lines.append(f"radar: excluded !{iid} {label}, {cover} — {spec['reason']}")
    return suppressed, lines


def _footer(open_mrs: list[dict], covered: set[str], healed: list[str],
            drifted: dict[str, tuple[str, str]], pruned: list[str],
            uncovered: list[str], gone: int, feed: str, label: str = "",
            excluded: int = 0, elided: int = 0,
            departed_capped: bool = False) -> str:
    """Tallies over the board that was printed, plus what was kept off it.

    `elided` is the delta's own withholding, disclosed for the same reason the
    exclusion total is (#1022). Every count below describes all `open_mrs`
    while `render` prints only the rows that moved, so without this token the
    footer and the rows disagree by construction and nothing says so.

    `open_mrs` here is the *shown* population. A footer counting the full one
    would report a failure with no row behind it, which sends the reader
    hunting for something that was deliberately removed — the exclusion
    restores the total as its own token instead.

    `healed` / `unwatched` / `pruned` / `drift` stay over the whole
    population: they report what this tier did, and it acts on excluded MRs
    exactly as it acts on any other.
    """
    counts: dict[str, int] = {}
    for m in open_mrs:
        counts[str(m.get("_pipeline") or "none")] = counts.get(str(m.get("_pipeline") or "none"), 0) + 1
    parts = [label] if label else []
    parts.append(f"{len(open_mrs)} open")
    if counts.get("failed"):
        parts.append(f"{counts['failed']} failing")
    if counts.get("running"):
        parts.append(f"{counts['running']} running")
    if counts.get("success"):
        parts.append(f"{counts['success']} green")
    # Read off the rows rather than derived from the cap, and counted over the
    # population this footer describes, so the tokens always add up. Without it
    # an MR with no pipeline status lands in the "none" bucket, which is
    # counted above and then never printed — 45 open, 40 green, and five rows
    # the tally silently declines to account for.
    unchecked = mrs._unchecked_count(open_mrs)
    if unchecked:
        parts.append(f"{unchecked} unchecked")
    parts.append(f"{len([m for m in open_mrs if str(m.get('iid')) in covered])} watched")
    if healed:
        parts.append(f"{len(healed)} healed")
    if uncovered:
        parts.append(f"{len(uncovered)} unwatched")
    if drifted:
        parts.append(f"{len(drifted)} drift")
    if pruned:
        parts.append(f"{len(pruned)} pruned")
    if gone:
        # See gh_prs._footer (#1024). `open_mrs` is filter-scoped and leaving a
        # filter is not merging; on a full page not even leaving is established.
        parts.append(f"{gone} off this page" if departed_capped
                     else f"{gone} left this board")
    if excluded:
        parts.append(f"{excluded} excluded")
    if elided:
        parts.append(f"{elided} unchanged not shown")
    parts.append(FEED_LABEL.get(feed, feed))
    return " | ".join(parts)


def _feed_warnings(feed: str, feed_err: str,
                   others: list[str] | None = None) -> list[str]:
    """A blind board must say so. A dead or erroring feed discovers nothing,
    and the symptom of that is a board that simply stops gaining rows — which
    is exactly what an all-quiet day looks like.

    `others` is the same failure from the opposite side (#486): a feed left
    running by an earlier, wider filter is still receiving MRs that this
    board does not list, and the board looks healthy while omitting them.
    Named, not killed — a second population can be deliberate.
    """
    out: list[str] = []
    if feed == "failed":
        out.append("radar: WARNING — MR feed poller is down. New MRs will not be "
                   "discovered until the next radar run.")
    elif feed == "capped":
        out.append("radar: WARNING — MR feed poller has died too often and is no "
                   "longer being respawned. New MRs will NOT be discovered.")
    elif feed_err:
        out.append(f"radar: WARNING — MR feed poller is failing to poll: {feed_err}")
    for other in others or []:
        out.append(f"radar: NOTE — a feed poller is also live on scope '{other}', "
                   f"which this board does not cover. Its MRs are not on this "
                   f"board; re-run as radar:{other} to see them.")
    return out


def _unchecked_warning(unchecked: int, total: int) -> list[str]:
    """Disclose the MRs whose pipeline was never read. [] when there are none.

    The empty return is load-bearing: on a fully-checked board the absence of
    this line is how the tier claims it saw everything, so a complete board
    prints nothing extra and the marker keeps meaning something.

    `_enrich` is capped (#659 is the radar half of #652), and an MR it never
    reached has no `_pipeline` at all — which `mrs._is_failing` reads as "not
    failing" and `mrs._sort_key` therefore sorts among the green. The row is
    still on the board, but nothing about it says its status is unknown, and on
    a delta board it is not even that: an unenriched MR is not a standing
    problem, so it drops out of every run after the first.

    Deliberately not the word "capped". This tier already spends that word on
    the respawn cap (`FEED_LABEL`, radar's watcher cap warnings), and a reader
    who knows it there would read it here as a watcher problem.
    """
    if unchecked <= 0:
        return []
    cap = mrs._get_config()["enrich_cap"]
    line = (f"radar: WARNING — {unchecked} of {total} MRs on this board were not "
            f"checked: their pipeline status is unknown, not green, so a failing "
            f"one among them is indistinguishable from a passing one here.")
    # The cap is named as the escape only when the cap is what cut. Below it the
    # unchecked MRs are detail lookups that failed, and pointing at a limit that
    # never applied is a confidently wrong cause — the rule mrs._cap_notice
    # states, applied at the surface that renders rather than the one that lists.
    if total > cap:
        line += f" Enrichment cap is {cap}; raise {mrs.ENRICH_CAP_KNOB}=N."
    return [line]


def render(open_mrs: list[dict], covered: set[str], healed: list[str],
           drifted: dict[str, tuple[str, str]], pruned: list[str],
           uncovered: list[str], previous: dict[str, Any] | None,
           feed: str = "alive", feed_err: str = "", label: str = "",
           excluded: set[str] | None = None, notes: list[str] | None = None,
           other_scopes: list[str] | None = None,
           losses: list[str] | None = None,
           page_capped: bool = False,
           now: str | None = None,
           stale_running_minutes: float = STALE_RUNNING_MINUTES) -> list[str]:
    """Full board on cold start; changed + standing-problem rows afterwards.

    `label` names the population on every board, the default one included
    (#486). Two radars over different filters in one window otherwise print
    two boards indistinguishable from one board printed twice — and, worse,
    a session that widened the filter and then ran a bare `radar` got the
    narrow board back with nothing on it saying so. An omission the tool
    produced renders exactly like an absence in the world, so the scope the
    board was actually built from is stated rather than implied.

    `excluded` removes rows; `notes` says so. They are two arguments rather
    than one because the notes outlive the suppression — an exclusion that
    did *not* apply prints a line and no row is removed, and that is the case
    that stops a spent reason becoming a permanent blind spot. Notes are
    printed on every run, including a delta-suppressed one: the repetition is
    what the exclusion was removing, and making the suppression itself
    invisible would rebuild the problem one level up.
    """
    cold = previous is None
    prev_entries: dict[str, Any] = (previous or {}).get("mrs", {}) or {}
    healed_set, uncovered_set = set(healed), set(uncovered)
    excluded = excluded or set()
    board_mrs = [m for m in open_mrs if str(m.get("iid", "?")) not in excluded]

    shown = []
    elided: list[str] = []
    for m in sorted(board_mrs, key=mrs._sort_key):
        iid = str(m.get("iid", "?"))
        prev_entry = prev_entries.get(iid)
        # `facts`, not the raw entry: the entry also carries `_since`, which
        # must never read as a move — see `_snapshot.facts`.
        moved = snapshot.facts(prev_entry) != _snap_entry(m)
        notable = iid in drifted or iid in healed_set or iid in uncovered_set
        stale = _stale_running(m, prev_entry, stale_running_minutes, now)
        if cold or moved or notable or _is_standing_problem(m) or stale:
            marks = _marks(iid, drifted, healed_set, uncovered_set, stale,
                           str(m.get("_pipeline") or ""))
            shown.append(mrs._row(m, covered, True, marks))
        else:
            elided.append(iid)

    # Against `open_mrs`, never `board_mrs`: an exclusion removes a row and not
    # a member, and counting one as departed reports the operator's own standing
    # decision back to them as a merge.
    departed = _departed(previous, open_mrs)
    footer = _footer(board_mrs, covered, healed, drifted, pruned, uncovered,
                     len(departed), feed, label, len(excluded), len(elided),
                     page_capped)

    # Partial boards only — see the same call in `gh_prs.render`. `excluded` is
    # a different withholding with its own `notes`, and the two are counted
    # separately because one is the operator's standing decision and this one
    # is this tick's delta.
    elision = (snapshot.elided_note(elided, len(board_mrs), "MRs", "!", "gl-mrs")
               if shown else [])

    lines = (_feed_warnings(feed, feed_err, other_scopes)
             + _unchecked_warning(mrs._unchecked_count(open_mrs), len(open_mrs))
             + elision
             + snapshot.departed_note(departed, "MR", "!", "gl-mr:<iid>",
                                      page_capped)
             + list(losses or []))
    if cold:
        lines.append("radar: cold start — no prior snapshot, full board")
    if shown:
        # The radar board renders `mrs._row` directly rather than through
        # `mrs._render_table`, so it needs its own copy of that board's one
        # disclosure line (#819). The titles below are the MR authors' words,
        # and radar is read by an agent that has been told to act on what it
        # sees — which is the reader the note exists for.
        lines.append(mrs._untrusted.flat_note("MR titles"))
        lines.extend(shown)
        lines.append("")
        lines.append(footer)
    elif cold:
        # "No open MRs" would be false when the population is non-empty and
        # every row of it was excluded — the exact silent omission the
        # accounting lines below exist to prevent.
        lines.append("All open MRs in this population are excluded."
                     if excluded else "No open MRs.")
        lines.append("")
        lines.append(footer)
    elif departed:
        # See gh_prs.render (#1024): a departure is the one change with no row.
        lines.append(f"radar: no rows changed | {footer}")
    else:
        lines.append(f"radar: no change | {footer}")
    lines.extend(notes or [])
    return lines


def _no_watch(source: str, scope: str, only: list[str] | None = None) -> str:
    """Fallback `_watch` when a caller supplied none.

    "failed", never "alive": a tier asked to reconcile without a way to spawn
    cannot keep a feed running, and reporting the feed as fine because nobody
    handed us a spawner is exactly the tool-produced absence read as an
    absence in the world.
    """
    return "failed"


def radar_report(options: dict | None = None) -> tuple[list[str], bool]:
    """(lines, healthy) — the MR board, as radar's tier contract wants it.

    `healthy` here means "coverage is known and complete", not "no MR is red".
    A board full of failing pipelines is a healthy report of an unhealthy
    world; a board that lost its feed, or whose watchers are capped, is a
    report that cannot be trusted to be complete, and that is the thing radar
    must never render as green.

    An MR whose pipeline was never read counts against that completeness for
    the same reason (#659). `healthy` has exactly one consumer —
    `quiet_when_healthy`, which suppresses the whole board — so claiming health
    over MRs nobody checked is what lets a configured radar print nothing at
    all about a board it could not see the whole of. It stays a claim about
    coverage rather than an alarm: it does not touch radar's exit code, which
    belongs to tiers that could not run.

    Raises `RadarError` when live GitLab could not be reached. Radar catches
    it, prints it to stderr and exits non-zero — deliberately louder than a
    `healthy=False` return, because an unreachable GitLab is not a finding
    about MRs, it is the absence of any finding at all. Nothing is pruned,
    healed, spawned or snapshotted on that path: acting on a population we
    could not read is how a cache gets overwritten with a guess.
    """
    options = options or {}
    watch = options.get("_watch") or _no_watch
    multi = resolve_filter(str(options.get("_arg") or ""))

    open_mrs = live_open_mrs(multi)

    open_iids = [str(m.get("iid")) for m in open_mrs if m.get("iid") is not None]
    watched = mrs._watched_iids(transport.STATE_DIR)

    states = read_state_files()
    pruned = prune_terminal(states, watched)
    drifted = drift({i: s for i, s in states.items() if i not in set(pruned)})

    healed, uncovered, refused = heal(open_iids, watched)
    covered = watched | set(healed)

    scope = feed_scope(multi)
    feed = watch(FEED_SOURCE, scope, feed_only())
    feed_err = feed_error(scope) if feed == "alive" else ""

    exclusions, excl_problems = read_exclusions()
    excluded, excl_lines = resolve_exclusions(open_mrs, exclusions, covered)

    # Stated on every board, default included: "no label" used to spell both
    # "this is the default population" and "nobody said which population this
    # is", and the filter does not survive an invocation (#486).
    label = f"scope {filter_string(multi)}"
    if multi == default_filter():
        label += " (default)"
    previous = read_snapshot(multi)
    other_scopes = other_feed_scopes(scope)
    # `_query` fetches one page per filter expansion with no pagination loop, so
    # a population that reached the page size may be truncated and cannot
    # establish which of its previous members left (#1024). The union of two
    # expansions can reach `per_page` without either query being full, so this
    # over-declines rather than under-declines — the direction that never turns
    # a page limit into a claim about a merge.
    per_page = int(mrs._get_config().get("per_page") or 0)
    page_capped = bool(per_page) and len(open_mrs) >= per_page
    departed = _departed(previous, open_mrs)
    stale_after = options.get("stale_running_minutes", STALE_RUNNING_MINUTES)
    try:
        stale_after = float(stale_after)
    except (TypeError, ValueError):
        stale_after = STALE_RUNNING_MINUTES
    stamped_at = snapshot.now_iso()
    lines = render(open_mrs, covered, healed, drifted, pruned, uncovered, previous,
                   feed, feed_err, label, excluded, excl_problems + excl_lines,
                   other_scopes, loss_warnings(healed, refused), page_capped,
                   now=stamped_at, stale_running_minutes=stale_after)
    # The snapshot records the whole population, excluded rows included:
    # keyed on what is true, not on what was printed. Otherwise the run after
    # an exclusion is lifted reports a months-old MR as new.
    prev_entries: dict[str, Any] = (previous or {}).get("mrs", {}) or {}
    write_snapshot(
        {str(m.get("iid")): snapshot.stamp(_snap_entry(m),
                                           prev_entries.get(str(m.get("iid"))),
                                           stamped_at)
         for m in open_mrs if m.get("iid") is not None},
        multi,
    )

    # `departed` counts against health for the reason spelled out on the
    # docstring above: `quiet_when_healthy` drops `lines` wholesale, and a
    # departure-only tick is entirely elided rows plus one summary line.
    healthy = not (uncovered or other_scopes or feed_err or departed
                   or mrs._unchecked_count(open_mrs)
                   or feed in ("failed", "capped"))
    return lines, healthy


def radar_state(options: dict | None = None) -> list[str]:
    """What this tier knows, without spawning or calling GitLab (#859).

    `radar_report` heals: it respawns per-MR watchers and keeps the feed alive,
    which forks processes on the operator's machine. That made *looking* at
    this tier cost the same as acting on it, so looking did not happen. Every
    line below comes from a file already on disk — the snapshot, the pid files,
    the state files — and `glab` is never invoked.
    """
    options = options or {}
    try:
        multi = resolve_filter(str(options.get("_arg") or ""))
    except RadarError as exc:
        # Inspection must stay openable. Everything below is keyed on the
        # filter, so there is nothing honest to print — but raising would make
        # the one view that never spawns the one view you cannot look at.
        return [f"  filter    : REFUSED — {exc}"]
    scope = feed_scope(multi)
    out = [f"  filter    : {filter_string(multi)}"
           f"{' (default)' if multi == default_filter() else ''}"]

    path = _snapshot_path(multi)
    previous = read_snapshot(multi)
    out.append(f"  snapshot  : {path} — "
               + (f"{len((previous or {}).get('mrs') or {})} MR(s)"
                  if previous is not None else "absent (cold start next run)"))

    pid = feed_pid(scope)
    err = feed_error(scope)
    out.append(f"  feed      : scope {scope!r}, pid "
               f"{pid or 'none recorded'}{f' — last error: {err}' if err else ''}")
    for other in other_feed_scopes(scope):
        out.append(f"  feed ALSO : scope {other!r} is live and is NOT on this board")

    watched = sorted(mrs._watched_iids(transport.STATE_DIR))
    out.append(f"  watchers  : {', '.join('!' + i for i in watched) or 'none'}")

    exclusions, problems = read_exclusions()
    out.append(f"  exclusions: {len(exclusions)} configured"
               f"{f', {len(problems)} refused' if problems else ''}")
    return out
