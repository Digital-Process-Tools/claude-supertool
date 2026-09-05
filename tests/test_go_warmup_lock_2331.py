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

import threading
import time
from pathlib import Path

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
    or a swallowed answer."""
    stale = tmp_path / "go_warmup.lock"
    stale.write_text("", encoding="utf-8")

    called = []
    result = lock_mod.serialize_once(
        tmp_path, "go_warmup", lambda: called.append(1) or "ran anyway", 0.2)

    assert result == "ran anyway"
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
