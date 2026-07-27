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
"""
from __future__ import annotations

import glob
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
SNAPSHOT_NAME = "supertool-radar.snapshot.json"

FEED_LABEL = {"alive": "feed ok", "spawned": "feed respawned", "failed": "feed DOWN"}


class RadarError(RuntimeError):
    """Live GitLab could not be reached. Never degrade to 'all green'."""


def _snapshot_path() -> str:
    return os.path.join(transport.STATE_DIR, SNAPSHOT_NAME)


# ---------------------------------------------------------------------------
# 1. live truth
# ---------------------------------------------------------------------------

def live_open_mrs() -> list[dict]:
    """Every open MR of mine, pipeline-enriched. One gl-mrs query.

    Raises RadarError on any failure rather than returning an empty list — an
    empty board and an unreachable GitLab must never render the same.
    """
    cfg = mrs._get_config()
    cmd = mrs._build_list_cmd({"author": "@me", "state": "opened"}, cfg["per_page"])
    try:
        result = mrs._run(cmd)
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
    watcher may legitimately follow an MR the `author=@me` query never returns.
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

def feed_pid() -> int:
    """PID recorded for the feed poller, or 0 when there is no readable file."""
    try:
        raw = Path(transport.pid_path(FEED_SOURCE, FEED_SCOPE)).read_text()
    except OSError:
        return 0
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def ensure_feed() -> str:
    """Guarantee exactly one live feed poller. "alive" | "spawned" | "failed".

    Radar is idempotent and run on a loop, so the feed must be too: a live PID
    short-circuits before any spawn. Without that check every radar run would
    stack another feed poller, and n pollers over one filter means n copies of
    every mr_opened.
    """
    pid = feed_pid()
    if pid and transport._pid_alive(pid):
        return "alive"
    if dispatcher._load_source(FEED_SOURCE) is None:
        return "failed"
    only = [e for e in defaults.DEFAULT_FEED_ONLY.split(",") if e]
    try:
        spawned = dispatcher._spawn_poller(FEED_SOURCE, FEED_SCOPE, only)
    except OSError:
        spawned = 0
    return "spawned" if spawned else "failed"


def feed_error() -> str:
    """Last error the feed poller recorded, or "" when it is polling cleanly.

    A feed that is alive but erroring every tick discovers nothing while
    looking healthy in `watches` — the same silence as a dead one, so it gets
    the same report. The dispatcher clears this key on a successful poll, so a
    message here is current rather than a scar.
    """
    state = transport.read_state(FEED_SOURCE, FEED_SCOPE)
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


def read_snapshot() -> dict[str, Any] | None:
    """Previous board, or None on cold start (no snapshot on disk)."""
    try:
        with open(_snapshot_path(), encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("mrs"), dict):
        return None
    return loaded


def write_snapshot(entries: dict[str, dict]) -> None:
    path = _snapshot_path()
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
    if str(m.get("_pipeline") or "") == "failed":
        return True
    return bool(m.get("has_conflicts") or m.get("detailed_merge_status") == "conflict")


def _footer(open_mrs: list[dict], covered: set[str], healed: list[str],
            drifted: dict[str, tuple[str, str]], pruned: list[str],
            uncovered: list[str], gone: int, feed: str) -> str:
    counts: dict[str, int] = {}
    for m in open_mrs:
        counts[str(m.get("_pipeline") or "none")] = counts.get(str(m.get("_pipeline") or "none"), 0) + 1
    parts = [f"{len(open_mrs)} open"]
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
           feed: str = "alive", feed_err: str = "") -> str:
    """Full board on cold start; changed + standing-problem rows afterwards."""
    cold = previous is None
    prev_entries: dict[str, Any] = (previous or {}).get("mrs", {}) or {}
    healed_set, uncovered_set = set(healed), set(uncovered)

    shown = []
    for m in sorted(open_mrs, key=mrs._sort_key):
        iid = str(m.get("iid", "?"))
        moved = prev_entries.get(iid) != _snap_entry(m)
        notable = iid in drifted or iid in healed_set or iid in uncovered_set
        if cold or moved or notable or _is_standing_problem(m):
            marks = _marks(iid, drifted, healed_set, uncovered_set)
            shown.append(mrs._row(m, covered, True, marks))

    gone = len([i for i in prev_entries if i not in {str(m.get("iid")) for m in open_mrs}])
    footer = _footer(open_mrs, covered, healed, drifted, pruned, uncovered, gone, feed)

    lines = _feed_warnings(feed, feed_err)
    if cold:
        lines.append("radar: cold start — no prior snapshot, full board")
    if shown:
        lines.extend(shown)
        lines.append("")
        lines.append(footer)
    elif cold:
        lines.append("No open MRs.")
        lines.append("")
        lines.append(footer)
    else:
        lines.append(f"radar: no change | {footer}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        open_mrs = live_open_mrs()
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

    feed = ensure_feed()
    feed_err = feed_error() if feed == "alive" else ""

    previous = read_snapshot()
    print(render(open_mrs, covered, healed, drifted, pruned, uncovered, previous,
                 feed, feed_err))
    write_snapshot({str(m.get("iid")): _snap_entry(m) for m in open_mrs if m.get("iid") is not None})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
