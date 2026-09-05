"""Serialize an expensive one-time spawn across pytest-xdist workers (#2331).

`-n auto` runs the suite across N worker processes on one machine. A
module-scoped or even session-scoped pytest fixture is scoped to the
*worker's own session*, not to the run as a whole, so a fixture that pays a
real, external cold-cache cost -- like `go vet`'s standard-library compile on
an empty `GOCACHE` -- pays it again in every worker whose first go-vet test
lands before another worker's has finished. Measured directly on this
machine with a cold `GOCACHE`: two workers racing a cold cache each paid the
full compile concurrently (~6.2s each) rather than one benefiting from the
other's work, which is exactly the shape #2331 profiled as 48s landing on one
test in a 138s durations list.

`GOCACHE` itself is shared, on disk, machine-wide -- it is the *timing*, not
the cache, that is per-worker. Serializing access with a lock file turns the
race into a queue: whichever caller gets there first pays the real cost, and
every caller after it finds an already-warm cache and finishes fast on its
own, without needing to know what the first caller found.

This is a scheduling fix, not a caching layer: it never remembers an answer
and it never turns a call into a no-op. A caller denied the lock still runs
`fn` itself, once the lock is free -- so a lock file left behind by a
crashed holder degrades to "no serialization", never to a wrong or a
swallowed result.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def shared_worker_root(tmp_path_factory) -> Path:
    """The one temp directory every xdist worker in this session was handed.

    `tmp_path_factory.getbasetemp()` for worker `gw3` is
    `.../pytest-of-<user>/pytest-<N>/popen-gw3`; every worker's own base temp
    is a sibling under that same `pytest-<N>`, which the *controller* created
    before any worker existed. Outside `-n auto` (or under `-n0`) there is no
    `popen-gwN` segment and the base temp is already the only process there
    is, so it is returned unchanged.
    """
    base = tmp_path_factory.getbasetemp()
    return base.parent if base.name.startswith("popen-gw") else base


def serialize_once(lock_dir: Path, name: str, fn: Callable[[], T],
                    timeout_s: float) -> T:
    """Run `fn` with at most one caller inside it at a time, across processes.

    Blocks other callers on the same `name` under `lock_dir` until the
    holder releases the lock (its `fn` returns, or raises) or `timeout_s`
    elapses. A caller that times out waiting runs `fn` itself rather than
    reporting a coordination failure -- this exists to make `fn` fast for
    everyone *after* the first caller, never to gate correctness on the lock
    working. The same fallback covers an `OSError` from the lock file itself
    (an unwritable `lock_dir`, for instance): correctness never depends on
    this succeeding.
    """
    lock_path = lock_dir / (name + ".lock")
    deadline = time.time() + timeout_s
    fd = None
    try:
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.time() >= deadline:
                    return fn()
                time.sleep(0.05)
    except OSError:
        return fn()

    try:
        return fn()
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass
