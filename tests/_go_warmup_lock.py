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
import warnings
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
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # A lock file older than `timeout_s` is presumed abandoned by a
            # holder that never reached its own `finally` -- killed outright,
            # or an unlink that itself failed (see the warning below). Without
            # breaking it here, an orphaned lock is permanent: nothing else in
            # this function ever removes one, so every later caller for this
            # `name`, in this run and in every run after it that reuses the
            # same `lock_dir`, would sit out the full `timeout_s` forever
            # instead of the one-time cost this function exists to bound
            # (#2331 self-review -- an auditor finding, not observed failing
            # in production, since no CI leg kills a worker mid-hold today).
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0  # gone already, or unreadable -- just retry below
            if age > timeout_s:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            # `fn()` here is outside every `except OSError` in this function,
            # on purpose: an `OSError` `fn` itself raises must propagate to
            # *this* caller once, not be mistaken for a second
            # lock-acquisition failure and rerun `fn` a second time (#2331
            # self-review) -- an earlier draft nested this `return fn()`
            # inside a `try` whose sibling `except OSError:` below caught
            # exactly that, and a real `fn` failure on this path silently ran
            # twice before finally propagating.
            if time.time() >= deadline:
                return fn()
            time.sleep(0.05)
        except OSError:
            # The lock file itself could not be created for some other reason
            # (an unwritable lock_dir, for instance) -- not "someone else has
            # it". `fn` has not been called yet on this branch, so calling it
            # once here cannot double-invoke anything.
            return fn()

    try:
        return fn()
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError as exc:
            # Best-effort, and not fatal to this call -- the staleness check
            # above is what actually bounds the cost of a lock this process
            # could not clean up after itself (a Windows AV scan transiently
            # holding the just-closed handle is the case tests/conftest.py
            # already names for a different lock, around a `git fsmonitor`
            # daemon). But silence here is exactly what let a leaked lock go
            # unnoticed until every later caller paid for it, so it is
            # reported rather than swallowed (#2331 self-review, auditor).
            warnings.warn(
                "serialize_once could not remove its own lock file %s after "
                "running %r: %s" % (lock_path, name, exc), stacklevel=2)
