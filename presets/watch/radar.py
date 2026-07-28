#!/usr/bin/env python3
"""radar — reconcile watch coverage against live GitLab, then report.

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
"everything is green". That is the failure this op exists to remove, so state
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
  4. report       full board on cold start, delta-only afterwards.

The population is an argument, in the same filter vocabulary as `gl-mrs`:

    radar                            defaults.DEFAULT_FILTER
    radar:author=modular.system      what the automated agent opened
    radar:author=@me,author=x        two queries, unioned by iid

One filter, one population, one board. `live_open_mrs`, `heal` and the feed
poller are three views of a single resolved filter and are never derived from
different ones — a board that omits MRs it is actively receiving events for
renders exactly like a board where those MRs are fine, which is the same
silent incompleteness this op exists to remove.

The snapshot is keyed by that filter for the same reason. Two populations
sharing one snapshot file would report every MR of the first as new and every
MR of the second as gone; a lying delta is worse than no delta.

Idempotent, so it is safe on every session start and on a loop. That includes
the feed: a live PID short-circuits the spawn, so N radar runs still leave one
feed poller and one copy of every discovery event.

"Nothing moved" means: the set of open MRs is unchanged, no MR changed
pipeline status / pipeline id / draft / conflict flag, and radar itself took
no action (nothing pruned, nothing healed, no drift). Standing failures and
conflicts are re-printed even when unchanged — an unfixed red is a current
fact, not history. When nothing moved radar still prints one summary line
rather than nothing at all: total silence is indistinguishable from a radar
that failed to run.

Standing exclusions
-------------------

Some MRs are red for a reason nobody intends to fix soon, and re-printing
that row every run is how a reader learns to skim a board — which is how a
real red gets missed. `ops.radar.radar_exclusions` in `.supertool.json` moves
that suppression out of the reader's memory and into the tool.

An exclusion is the one thing in this op that can hide a failure, so it is
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
              filter says what radar is responsible for, and radar does not
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
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent

sys.path.insert(0, str(_HERE))
import defaults  # noqa: E402
import transport  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mrs = _load("radar_gitlab_mrs", _HERE.parents[1] / "presets" / "gitlab" / "mrs.py")
dispatcher = _load("radar_watch_dispatcher", _HERE / "dispatcher.py")

SOURCE = defaults.DEFAULT_SOURCE
FEED_SOURCE = defaults.DEFAULT_FEED_SOURCE
FEED_SCOPE = defaults.DEFAULT_FEED_SCOPE
SNAPSHOT_PREFIX = "supertool-radar"

# ops.radar.radar_exclusions in .supertool.json, JSON-encoded into the env by
# the op runner — the same route ops.gl-job.job_patterns takes.
EXCLUSIONS_ENV = "SUPERTOOL_RADAR_EXCLUSIONS"

_FIX_HINT = "remove it from ops.radar.radar_exclusions in .supertool.json"

FEED_LABEL = {"alive": "feed ok", "spawned": "feed respawned", "failed": "feed DOWN"}


class RadarError(RuntimeError):
    """Live GitLab could not be reached. Never degrade to 'all green'."""


def default_filter() -> dict[str, list[str]]:
    """The population a bare `radar` covers, read from defaults.py.

    Not a literal here: the whole point of that module is that the shell
    supervisor and this op cannot describe different populations.
    """
    return mrs._parse_multi(defaults.DEFAULT_FILTER)[0]


def resolve_filter(argv: list[str] | None = None) -> dict[str, list[str]]:
    """The one filter every other step reads. gl-mrs vocabulary, or default.

    A key may repeat — `author=@me,author=modular.system` — because GitLab
    takes one author per query and the union is the population the caller
    described.
    """
    arg = argv[1].strip() if argv and len(argv) > 1 and argv[1] else ""
    multi = mrs._parse_multi(arg)[0] if arg else {}
    return multi or default_filter()


def filter_string(multi: dict[str, list[str]]) -> str:
    """The filter back in gl-mrs arg form — what the user typed, normalised."""
    return ",".join(f"{k}={v}" for k, vals in multi.items() for v in vals)


def filter_key(multi: dict[str, list[str]]) -> str:
    """Stable short hash of the filter, insensitive to key and value order.

    `author=a,author=b` and `author=b,author=a` are the same population and
    must share a snapshot; `author=a` and `author=b` must not.
    """
    norm = {k: sorted(set(v)) for k, v in sorted(multi.items())}
    blob = json.dumps(norm, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


def _snapshot_path(multi: dict[str, list[str]]) -> str:
    return os.path.join(transport.STATE_DIR,
                        f"{SNAPSHOT_PREFIX}.{filter_key(multi)}.snapshot.json")


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
    watcher may legitimately follow an MR this radar's filter never returns —
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

def heal(open_iids: list[str], watched: set[str]) -> tuple[list[str], list[str]]:
    """Respawn a watcher for every open MR without a live poller.

    Returns (healed, still_uncovered). The event filter comes from defaults.py
    so a healed watcher is identical to one watch-mine.sh would have spawned.
    """
    gaps = [iid for iid in open_iids if iid not in watched]
    if not gaps:
        return [], []
    if dispatcher._load_source(SOURCE) is None:
        return [], gaps
    only = [e for e in defaults.DEFAULT_ONLY.split(",") if e]
    healed: list[str] = []
    failed: list[str] = []
    for iid in gaps:
        try:
            pid = dispatcher._spawn_poller(SOURCE, iid, only)
        except OSError:
            pid = 0
        (healed if pid else failed).append(iid)
    return healed, failed


# ---------------------------------------------------------------------------
# 3b. feed — the tier that discovers MRs radar has never seen
# ---------------------------------------------------------------------------

def feed_scope(multi: dict[str, list[str]] | None = None) -> str:
    """The feed watcher id covering this population.

    The feed source accepts either one of its aliases or a literal gl-mrs
    filter string as its id, so the board's filter reaches the discovery tier
    unchanged. An alias is preferred when it expands to the same filter: the
    id is the pid filename, so `@me` and `author=@me,state=opened` would
    otherwise be two pollers over one population, i.e. two copies of every
    mr_opened.
    """
    multi = default_filter() if multi is None else multi
    poller = dispatcher._load_source(FEED_SOURCE)
    aliases = getattr(poller, "ALIASES", None) or {}
    for alias, expansion in aliases.items():
        if mrs._parse_multi(expansion)[0] == multi:
            return alias
    return filter_string(multi)


def feed_pid(scope: str = FEED_SCOPE) -> int:
    """PID recorded for the feed poller, or 0 when there is no readable file."""
    try:
        raw = Path(transport.pid_path(FEED_SOURCE, scope)).read_text(encoding="utf-8")
    except OSError:
        return 0
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def ensure_feed(scope: str = FEED_SCOPE) -> str:
    """Guarantee exactly one live feed poller. "alive" | "spawned" | "failed".

    Radar is idempotent and run on a loop, so the feed must be too: a live PID
    short-circuits before any spawn. Without that check every radar run would
    stack another feed poller, and n pollers over one filter means n copies of
    every mr_opened.
    """
    pid = feed_pid(scope)
    if pid and transport._pid_alive(pid):
        return "alive"
    if dispatcher._load_source(FEED_SOURCE) is None:
        return "failed"
    only = [e for e in defaults.DEFAULT_FEED_ONLY.split(",") if e]
    try:
        spawned = dispatcher._spawn_poller(FEED_SOURCE, scope, only)
    except OSError:
        spawned = 0
    return "spawned" if spawned else "failed"


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
    """The facts radar reports about one MR. Delta is computed over these."""
    return {
        "pipeline": str(m.get("_pipeline") or ""),
        "pipeline_id": str(m.get("_pipeline_id") or ""),
        "draft": bool(m.get("draft")),
        "conflict": bool(m.get("has_conflicts") or m.get("detailed_merge_status") == "conflict"),
    }


def read_snapshot(multi: dict[str, list[str]] | None = None) -> dict[str, Any] | None:
    """Previous board for this filter, or None on cold start.

    Keyed by filter: comparing one population against another's snapshot
    reports every row of each as a change, which is a delta column that lies.
    """
    multi = default_filter() if multi is None else multi
    try:
        with open(_snapshot_path(multi), encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("mrs"), dict):
        return None
    return loaded


def write_snapshot(entries: dict[str, dict],
                   multi: dict[str, list[str]] | None = None) -> None:
    path = _snapshot_path(default_filter() if multi is None else multi)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"mrs": entries}, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _marks(iid: str, drifted: dict[str, tuple[str, str]],
           healed: set[str], uncovered: set[str]) -> str:
    """The two novel signals, appended to the shared gl-mrs row format."""
    out = []
    if iid in drifted:
        was, now = drifted[iid]
        out.append(f"[drift: {was}→{now}]")
    if iid in healed:
        out.append("[healed]")
    elif iid in uncovered:
        out.append("[unwatched]")
    return ("  " + " ".join(out)) if out else ""


def _is_standing_problem(m: dict) -> bool:
    """Unresolved red or conflict — a current fact, so never delta-suppressed."""
    return bool(_problem_label(m))


def _problem_label(m: dict) -> str:
    """"failed", "conflict", "failed+conflict", or "" when the MR is fine.

    Printed on the exclusion line, so a suppressed MR that picks up a second
    problem changes what the board says about it without un-suppressing it.
    """
    bits = []
    if str(m.get("_pipeline") or "") == "failed":
        bits.append("failed")
    if bool(m.get("has_conflicts") or m.get("detailed_merge_status") == "conflict"):
        bits.append("conflict")
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
            excluded: int = 0) -> str:
    """Tallies over the board that was printed, plus what was kept off it.

    `open_mrs` here is the *shown* population. A footer counting the full one
    would report a failure with no row behind it, which sends the reader
    hunting for something that was deliberately removed — the exclusion
    restores the total as its own token instead.

    `healed` / `unwatched` / `pruned` / `drift` stay over the whole
    population: they report what radar did, and radar acts on excluded MRs
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
        parts.append(f"{gone} no longer open")
    if excluded:
        parts.append(f"{excluded} excluded")
    parts.append(FEED_LABEL.get(feed, feed))
    return " | ".join(parts)


def _feed_warnings(feed: str, feed_err: str) -> list[str]:
    """A blind radar must say so. A dead or erroring feed discovers nothing,
    and the symptom of that is a board that simply stops gaining rows — which
    is exactly what an all-quiet day looks like."""
    if feed == "failed":
        return ["radar: WARNING — MR feed poller is down. New MRs will not be "
                "discovered until the next radar run."]
    if feed_err:
        return [f"radar: WARNING — MR feed poller is failing to poll: {feed_err}"]
    return []


def render(open_mrs: list[dict], covered: set[str], healed: list[str],
           drifted: dict[str, tuple[str, str]], pruned: list[str],
           uncovered: list[str], previous: dict[str, Any] | None,
           feed: str = "alive", feed_err: str = "", label: str = "",
           excluded: set[str] | None = None, notes: list[str] | None = None) -> str:
    """Full board on cold start; changed + standing-problem rows afterwards.

    `label` names the population when it is not the default, because two
    radars over different filters in one window otherwise print two boards
    that are indistinguishable from one board printed twice.

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
    for m in sorted(board_mrs, key=mrs._sort_key):
        iid = str(m.get("iid", "?"))
        moved = prev_entries.get(iid) != _snap_entry(m)
        notable = iid in drifted or iid in healed_set or iid in uncovered_set
        if cold or moved or notable or _is_standing_problem(m):
            marks = _marks(iid, drifted, healed_set, uncovered_set)
            shown.append(mrs._row(m, covered, True, marks))

    gone = len([i for i in prev_entries if i not in {str(m.get("iid")) for m in open_mrs}])
    footer = _footer(board_mrs, covered, healed, drifted, pruned, uncovered, gone,
                     feed, label, len(excluded))

    lines = _feed_warnings(feed, feed_err)
    if cold:
        lines.append("radar: cold start — no prior snapshot, full board")
    if shown:
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
    else:
        lines.append(f"radar: no change | {footer}")
    lines.extend(notes or [])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    multi = resolve_filter(argv)
    try:
        open_mrs = live_open_mrs(multi)
    except RadarError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    open_iids = [str(m.get("iid")) for m in open_mrs if m.get("iid") is not None]
    watched = mrs._watched_iids(transport.STATE_DIR)

    states = read_state_files()
    pruned = prune_terminal(states, watched)
    drifted = drift({i: s for i, s in states.items() if i not in set(pruned)})

    healed, uncovered = heal(open_iids, watched)
    covered = watched | set(healed)

    scope = feed_scope(multi)
    feed = ensure_feed(scope)
    feed_err = feed_error(scope) if feed == "alive" else ""

    exclusions, excl_problems = read_exclusions()
    excluded, excl_lines = resolve_exclusions(open_mrs, exclusions, covered)

    label = "" if multi == default_filter() else filter_string(multi)
    previous = read_snapshot(multi)
    print(render(open_mrs, covered, healed, drifted, pruned, uncovered, previous,
                 feed, feed_err, label, excluded, excl_problems + excl_lines))
    # The snapshot records the whole population, excluded rows included:
    # keyed on what is true, not on what was printed. Otherwise the run after
    # an exclusion is lifted reports a months-old MR as new.
    write_snapshot(
        {str(m.get("iid")): _snap_entry(m) for m in open_mrs if m.get("iid") is not None},
        multi,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
