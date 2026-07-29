#!/usr/bin/env python3
"""Dispatcher for the `watch` preset — handles watch/unwatch/watches sub-ops.

Each `watch:SOURCE:ID[:only=...]` invocation forks a poller child for
SOURCE/ID. The child detaches (setsid + double-fork) and runs the source's
`poller.poll()` function in a loop until terminal or until killed.

The dispatcher does NOT contain source-specific logic. It only:
  - Resolves the source's poller module from presets/watch/sources/
  - Manages PID files
  - Spawns + kills children
  - Renders the `watches` table

A source plugin lives at `presets/watch/sources/<NAME>/poller.py` and exposes:
  - INTERVAL: int — seconds between polls
  - poll(state: dict, ctx: dict) -> tuple[list[dict], dict]
        returns (events_to_emit, new_state)
  - is_terminal(state: dict) -> bool
        True when the watcher should stop on its own (merged/closed/finished)

Each event is a dict {event: str, payload: dict, notify_title?: str,
notify_message?: str}. The dispatcher passes them to transport.emit_event.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Allow importing transport as a sibling module when launched via `python3 dispatcher.py`.
sys.path.insert(0, str(Path(__file__).parent))
import transport  # noqa: E402

SOURCES_DIR = Path(__file__).parent / "sources"


def _load_source(name: str):
    """Import the poller module for SOURCE from sources/<name>/poller.py."""
    poller_path = SOURCES_DIR / name / "poller.py"
    if not poller_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(f"watch_source_{name}", poller_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_args(parts: list[str]) -> tuple[str, str, list[str]]:
    """Parse SOURCE ID [only=ev1,ev2 ...] from positional argv segments.

    Supertool's {args} placeholder explodes the colon-separated op into one
    argv element per segment, so we receive ["gitlab-mr", "21803", "only=..."]
    rather than a single colon-joined string.

    Returns (source, id, allowed_event_keys_or_empty_meaning_all).
    """
    if len(parts) < 1 or not parts[0]:
        raise ValueError("missing SOURCE")
    if len(parts) < 2 or not parts[1]:
        raise ValueError(f"missing ID for source {parts[0]!r}")
    source, watcher_id = parts[0], parts[1]
    # PID/state filenames use `__` to separate source and id (see transport.py).
    # Allowing it inside either field would make `list_active_pids` ambiguous.
    if "__" in source or "__" in watcher_id:
        raise ValueError("SOURCE and ID must not contain '__' (reserved as filename separator)")
    # Both fields are interpolated straight into a /tmp path. Feed sources take
    # a filter string as their id, so this is now reachable from ordinary use.
    if "/" in source or "/" in watcher_id:
        raise ValueError("SOURCE and ID must not contain '/' (they are filename components)")
    only: list[str] = []
    for p in parts[2:]:
        if p.startswith("only="):
            only = [e for e in p[len("only="):].split(",") if e]
    return source, watcher_id, only


def cmd_watch(parts: list[str]) -> int:
    try:
        source, watcher_id, only = _parse_args(parts)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1
    poller = _load_source(source)
    if poller is None:
        avail = ", ".join(sorted(p.name for p in SOURCES_DIR.iterdir() if p.is_dir())) or "(none)"
        print(f"ERROR: unknown source {source!r}. Available: {avail}")
        return 1
    status, pid = start_poller(source, watcher_id, only)
    if status == "alive":
        # Never silent, and never rendered like a clean start: an operator who
        # cannot tell "started" from "refused" learns nothing from running the
        # op twice, which is how the duplicates in #476 went unnoticed for a
        # day. Say what was found, and which live process holds the slot.
        print(f"Already watching {source}:{watcher_id} (PID {pid}) — "
              f"not starting a second. "
              f"Use ./supertool 'unwatch:{source}:{watcher_id}' to stop it.")
        return 0
    if status == "failed":
        print(f"ERROR: could not spawn a poller for {source}:{watcher_id}")
        return 1
    print(f"Watching {source}:{watcher_id} (PID {pid})")
    if only:
        print(f"Filter: {','.join(only)}")
    print(f"State: {transport.state_path(source, watcher_id)}")
    return 0


def cmd_unwatch(parts: list[str]) -> int:
    try:
        source, watcher_id, _ = _parse_args(parts)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1
    pid_file = transport.pid_path(source, watcher_id)
    if not os.path.exists(pid_file):
        print(f"No active watcher for {source}:{watcher_id}.")
        return 0
    try:
        pid = int(Path(pid_file).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        try:
            os.unlink(pid_file)
        except OSError:
            pass
        print(f"Stale PID file removed for {source}:{watcher_id}.")
        return 0
    if transport._pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.2)
            if transport._pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except OSError as e:
            print(f"ERROR: could not stop PID {pid}: {e}")
            return 1
    try:
        os.unlink(pid_file)
    except OSError:
        pass
    print(f"Stopped watcher for {source}:{watcher_id} (PID {pid}).")
    return 0


def cmd_list() -> int:
    rows = transport.list_active_pids()
    if not rows:
        print("No active watchers.")
        return 0
    widths = {
        "source": max(6, max(len(r["source"]) for r in rows)),
        "id": max(2, max(len(r["id"]) for r in rows)),
        "pid": max(5, max(len(str(r["pid"])) for r in rows)),
        "started": 20,
        "last_event": max(10, max(len(r["last_event"] or "-") for r in rows)),
    }
    header = (
        f"{'SOURCE':<{widths['source']}}  "
        f"{'ID':<{widths['id']}}  "
        f"{'PID':<{widths['pid']}}  "
        f"{'STARTED':<{widths['started']}}  "
        f"{'LAST_EVENT':<{widths['last_event']}}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['source']:<{widths['source']}}  "
            f"{r['id']:<{widths['id']}}  "
            f"{r['pid']:<{widths['pid']}}  "
            f"{r['started']:<{widths['started']}}  "
            f"{(r['last_event'] or '-'):<{widths['last_event']}}"
        )
    return 0


def start_poller(source: str, watcher_id: str, only: list[str]) -> tuple[str, int]:
    """Claim the (source, id) slot, then spawn its poller. ("alive"|"spawned"|"failed", pid).

    The one door to a new poller, for every tier — `watch` and radar's feed
    both come through here, because two spawn sites with two copies of the
    "is one already running?" question is how they came to disagree (#476).

    Ordering is #451's and is load-bearing: the slot is claimed *before* the
    fork, so losing the race costs nothing — no detached child to reap, no
    pidfile to unwind, nothing to clean up. The claim is written with this
    process's PID and only repointed at the grandchild once that PID is known,
    so the slot is never momentarily ownerless.

    A spawn that fails gives the slot back. A claim left behind by a poller
    that never started would refuse every future start for that id, and a
    refusal nobody asked for renders as a watcher quietly not existing.
    """
    owner = transport.claim_pidfile(source, watcher_id)
    if owner:
        return "alive", owner
    try:
        pid = _spawn_poller(source, watcher_id, only)
    except OSError:
        pid = 0
    if not pid:
        transport.release_pidfile(source, watcher_id, os.getpid())
        return "failed", 0
    transport.record_pid(source, watcher_id, pid)
    return "spawned", pid


def _spawn_poller(source: str, watcher_id: str, only: list[str]) -> int:
    """Detach a poller child (double-fork) and return its PID to the parent."""
    r, w = os.pipe()
    pid = os.fork()
    if pid != 0:
        # Parent: wait for child to report grandchild PID, then return.
        os.close(w)
        try:
            grand_pid = int(os.read(r, 32).decode().strip() or "0")
        except (OSError, ValueError):
            grand_pid = 0
        os.close(r)
        os.waitpid(pid, 0)
        return grand_pid
    # First child: detach session, fork grandchild, report PID, exit.
    os.close(r)
    os.setsid()
    pid2 = os.fork()
    if pid2 != 0:
        os.write(w, str(pid2).encode())
        os.close(w)
        os._exit(0)
    # Grandchild: close inherited fd, run the loop.
    os.close(w)
    _run_poll_loop(source, watcher_id, only)
    os._exit(0)
    return 0  # unreachable


def _run_poll_loop(source: str, watcher_id: str, only: list[str]) -> None:
    """Grandchild's poll loop. Writes PID file, runs until terminal or signal."""
    # Redirect stdio to /dev/null so the poller can't pollute the parent's terminal.
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)
    except OSError:
        pass

    # The slot was already claimed by the caller in start_poller(); this only
    # repoints it at the PID that is actually going to poll.
    transport.record_pid(source, watcher_id, os.getpid())

    poller = _load_source(source)
    if poller is None:
        transport.release_pidfile(source, watcher_id, os.getpid())
        return

    # Publish the event filter next to the state. `only` decides which of the
    # source's events this poller will ever emit, and it otherwise lived only
    # in this process's memory — so another tier could see the poller was alive
    # and still have no way to tell one that will announce a merge from one
    # filtered away from saying so. `gitlab-mr-feed` asks exactly that (#434),
    # in a place where guessing wrong means a transition nobody reports.
    published = transport.read_state(source, watcher_id)
    published["only"] = list(only)
    transport.write_state(source, watcher_id, published)

    state: dict[str, Any] = transport.read_state(source, watcher_id).get("source_state", {}) or {}
    # No prior state means this watcher knows nothing yet, so whatever its
    # first poll emits describes what it found rather than what changed. That
    # emission is deliberate — it is how a fresh watcher reports an already-red
    # MR, and gitlab-mr-feed leans on it — but the consumer has to be able to
    # tell it apart from a live transition (#464). Keyed on state, not on
    # process age: a poller restarted with its state intact is not bootstrapping.
    first_tick = not state
    ctx = {"source": source, "id": watcher_id, "only": only}
    interval = int(getattr(poller, "INTERVAL", 30))
    stop_flag = {"stop": False}
    reached_terminal = False

    def _handle_sigterm(*_a):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    try:
        while not stop_flag["stop"]:
            try:
                events, new_state = poller.poll(state, ctx)
            except Exception as e:  # noqa: BLE001 — never crash, log to state
                full = transport.read_state(source, watcher_id)
                full["last_error"] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                       "message": str(e)}
                transport.write_state(source, watcher_id, full)
                time.sleep(interval)
                continue

            for ev in events:
                if only and ev.get("event") not in only:
                    continue
                transport.emit_event(
                    source, watcher_id,
                    ev.get("event", "unknown"),
                    ev.get("payload", {}),
                    notify_title=ev.get("notify_title"),
                    notify_message=ev.get("notify_message"),
                    first_tick=first_tick,
                )

            full = transport.read_state(source, watcher_id)
            full["source_state"] = new_state
            full.pop("last_error", None)
            transport.write_state(source, watcher_id, full)
            state = new_state
            first_tick = False

            if hasattr(poller, "is_terminal") and poller.is_terminal(new_state):
                reached_terminal = True
                break

            for _ in range(interval):
                if stop_flag["stop"]:
                    break
                time.sleep(1)
    finally:
        # Only if this process still owns the slot. A poller shutting down
        # slowly, whose slot was meanwhile reclaimed, must not unlink its
        # successor's claim on the way out (#476).
        transport.release_pidfile(source, watcher_id, os.getpid())
        # A terminal watcher leaves no live process, so its state file is not a
        # record of anything current. Kept, it makes consumers that glob the
        # state files report merged MRs as active watches.
        if reached_terminal:
            transport.clear_state(source, watcher_id)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("ERROR: usage: dispatcher.py {watch|unwatch|list} [ARG]")
        return 1
    sub = argv[1]
    rest = argv[2:]
    if sub == "watch":
        return cmd_watch(rest)
    if sub == "unwatch":
        return cmd_unwatch(rest)
    if sub == "list":
        return cmd_list()
    print(f"ERROR: unknown sub-op {sub!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
