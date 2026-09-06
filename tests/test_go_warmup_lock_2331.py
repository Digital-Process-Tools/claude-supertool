"""Cross-process serialization for go-vet's cold-cache warm-up (#2331).

`--durations` on one CI leg showed 48s landing on a single test that pays a
cold `go vet` standard-library compile, and a second, module-scoped warm-up
fixture elsewhere in the suite paying a similar cost independently. Profiled
locally with a cold `GOCACHE`: under `-n auto`, two xdist workers landing on
a go-vet test at the same moment each pay the full compile concurrently,
because a pytest fixture's scope stops at its own worker's session -- it is
not shared across the worker processes `-n auto` actually runs.
`tests/_go_warmup_lock.py` fixes the *scheduling*, not the cache itself
(`GOCACHE` is already shared, on disk, machine-wide): a lock file turns the
race into a queue, so only the first caller pays the cold cost and everyone
after it runs against an already-warm cache.

These tests exercise `serialize_once` directly with a fake `fn`, rather than
a real `go vet` spawn: the property under test is the scheduling guarantee,
not go's own behaviour, and a real toolchain spawn would make this file
depend on Go being installed for a fact that has nothing to do with Go.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

import _go_warmup_lock as lock_mod


def test_two_racing_calls_do_not_overlap(tmp_path: Path) -> None:
    """The hazard this exists to remove: without serialization, concurrent
    callers all start `fn` at once, and their [start, end] windows overlap --
    the same shape as two xdist workers both compiling into an empty
    `GOCACHE` at the same time."""
    windows = []
    guard = threading.Lock()

    def fn():
        start = time.monotonic()
        time.sleep(0.1)
        end = time.monotonic()
        with guard:
            windows.append((start, end))
        return "done"

    threads = [threading.Thread(
        target=lock_mod.serialize_once, args=(tmp_path, "go_warmup", fn, 5.0))
        for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(windows) == 3, windows
    windows.sort()
    for (_s1, e1), (s2, _e2) in zip(windows, windows[1:]):
        assert s2 >= e1, (
            "two callers' fn windows overlapped -- serialization did not "
            "hold: %r" % (windows,))


def test_a_caller_after_release_still_gets_a_correct_isolated_result(
        tmp_path: Path) -> None:
    """Releasing the lock must not leave callers unable to acquire it again,
    and each caller's own `fn` result must reach that caller -- sharing the
    lock is not the same thing as sharing, or losing, the answer."""
    calls = []

    def fn(tag):
        calls.append(tag)
        return tag

    first = lock_mod.serialize_once(tmp_path, "go_warmup", lambda: fn("a"), 5.0)
    second = lock_mod.serialize_once(tmp_path, "go_warmup", lambda: fn("b"), 5.0)

    assert first == "a"
    assert second == "b"
    assert calls == ["a", "b"], calls
    assert not (tmp_path / "go_warmup.lock").exists()


def test_a_stale_lock_falls_back_to_running_fn_after_the_timeout(
        tmp_path: Path) -> None:
    """A lock file left behind by a crashed holder must not deadlock every
    later caller forever -- it degrades to "no serialization", not to a hang
    or a swallowed answer.

    Its mtime here is "now" -- freshly written, not old enough to be treated
    as abandoned by the staleness check below -- so this still exercises the
    per-call timeout path, distinct from `test_an_abandoned_lock_is_broken_...`
    which exercises the age-based path instead of waiting it out."""
    stale = tmp_path / "go_warmup.lock"
    stale.write_text("", encoding="utf-8")

    called = []
    result = lock_mod.serialize_once(
        tmp_path, "go_warmup", lambda: called.append(1) or "ran anyway", 0.2)

    assert result == "ran anyway"
    assert called == [1]


def test_an_abandoned_lock_older_than_the_timeout_is_broken_immediately(
        tmp_path: Path) -> None:
    """Auditor finding: without an age check, one leaked lock file forced
    every later caller -- in this run, and in every run after it that reuses
    the same lock_dir -- to sit out the full `timeout_s` again, forever, which
    is worse than paying the cold cost this function exists to avoid paying
    twice. A lock older than `timeout_s` must be broken and re-acquired
    quickly rather than waited out."""
    lock_path = tmp_path / "go_warmup.lock"
    lock_path.write_text("", encoding="utf-8")
    old = time.time() - 1000
    os.utime(lock_path, (old, old))

    called = []
    start = time.monotonic()
    result = lock_mod.serialize_once(
        tmp_path, "go_warmup", lambda: called.append(1) or "ran", 5.0)
    elapsed = time.monotonic() - start

    assert result == "ran"
    assert called == [1]
    assert elapsed < 1.0, (
        "an abandoned lock forced the full timeout instead of being broken "
        "immediately: %.3fs" % elapsed)


def test_a_release_failure_is_reported_not_swallowed(
        tmp_path: Path, monkeypatch) -> None:
    """Auditor finding: a `finally`-block unlink failure was silently
    swallowed, so a leaked lock (Windows AV holding the just-closed handle,
    for instance) left no trace anywhere a reader could see it. `fn`'s own
    result must still reach the caller -- a release failure is not `fn`
    failing -- but it must be visible."""
    def _boom(self):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(lock_mod.Path, "unlink", _boom)

    with pytest.warns(UserWarning, match="could not remove its own lock"):
        result = lock_mod.serialize_once(
            tmp_path, "go_warmup", lambda: "ran", 5.0)

    assert result == "ran"


def test_a_timed_out_wait_that_then_raises_does_not_double_call_fn(
        tmp_path: Path) -> None:
    """Self-review finding: an earlier draft nested the timeout-fallback
    `fn()` call inside the same `try` whose sibling `except OSError` was
    meant for the lock file itself -- so an `OSError` `fn` raised on this
    path was caught by that handler and `fn` was silently invoked a second
    time before the error finally propagated. `fn` must run exactly once,
    and its own exception must reach this caller unchanged."""
    stale = tmp_path / "go_warmup.lock"
    stale.write_text("", encoding="utf-8")

    calls = []

    def fn():
        calls.append(1)
        raise OSError("boom, e.g. the tool fn wraps vanished mid-run")

    with pytest.raises(OSError, match="boom"):
        lock_mod.serialize_once(tmp_path, "go_warmup", fn, 0.2)

    assert calls == [1], (
        "fn ran more than once on the timeout-fallback path: %r" % calls)


def test_an_exception_while_holding_the_lock_still_releases_it(
        tmp_path: Path) -> None:
    """Auditor finding: the docstring promises release "even if `fn` raises",
    and nothing exercised the acquired-lock branch of that promise -- every
    existing raising test goes through a fallback path where the lock was
    never actually held by this caller. A caller that raises while genuinely
    holding the lock must still leave it releasable, or the next caller
    would be stuck behind a lock nobody will ever free."""
    def fn():
        raise ValueError("boom while holding the lock")

    with pytest.raises(ValueError, match="boom while holding the lock"):
        lock_mod.serialize_once(tmp_path, "go_build_cache", fn, 5.0)

    assert not (tmp_path / "go_build_cache.lock").exists(), (
        "the lock survived a raising fn -- the next caller would be "
        "permanently blocked behind a lock nobody will ever free")

    # And a normal caller right after really can acquire it immediately.
    called = []
    result = lock_mod.serialize_once(
        tmp_path, "go_build_cache", lambda: called.append(1) or "ran", 5.0)
    assert result == "ran"
    assert called == [1]


def test_an_unwritable_lock_dir_falls_back_to_running_fn(
        tmp_path: Path, monkeypatch) -> None:
    """Coordination must never gate correctness: if the lock file itself
    cannot be created for a reason that is not "someone else holds it", `fn`
    still runs, once, rather than the caller reporting a coordination
    failure as if it were `fn`'s own failure."""
    def _boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(lock_mod.os, "open", _boom)
    called = []
    result = lock_mod.serialize_once(
        tmp_path, "go_warmup", lambda: called.append(1) or "ran", 1.0)
    assert result == "ran"
    assert called == [1]


def test_shared_worker_root_strips_the_popen_gw_segment() -> None:
    class _Factory:
        def getbasetemp(self):
            return Path("/tmp/pytest-of-x/pytest-3/popen-gw2")

    assert lock_mod.shared_worker_root(_Factory()) == Path(
        "/tmp/pytest-of-x/pytest-3")


def test_shared_worker_root_is_unchanged_outside_xdist() -> None:
    class _Factory:
        def getbasetemp(self):
            return Path("/tmp/pytest-of-x/pytest-3")

    assert lock_mod.shared_worker_root(_Factory()) == Path(
        "/tmp/pytest-of-x/pytest-3")
