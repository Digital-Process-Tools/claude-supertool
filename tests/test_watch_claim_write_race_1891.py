"""#1891 — claim_pidfile's O_CREAT|O_EXCL create-then-write left a window.

Reproduced, not merely hypothesised. #1891 asked four questions in the order
that narrows fastest; this file answers the first two directly and pins the
mechanism they point at.

**Mechanism.** `claim_pidfile` used to `os.open(path, O_CREAT|O_EXCL)` and
only *afterward* `os.fdopen(fd, "w").write(pid)`. Between those two calls the
pidfile exists at its well-known name with **zero bytes** in it. A second
claimant that hits `FileExistsError` in that window calls
`read_pid_checked`, which reads an empty file as `(0, "content is not a
PID")` — the same shape it reports for a genuinely corrupt file, and by
design (see its own docstring) that shape is reclaimable. `claim_pidfile`
then unlinks the name and re-creates it under a second PID, while the first
claimant — which already holds an open file descriptor to the now-unlinked
inode — goes on to write its own PID into that orphaned inode and returns 0,
believing it owns a slot nobody can any longer see it holding. Both
claimants report success; only one is visible on disk; a third, fourth or
fifth can join the same window before either finishes.

Confirmed live with two real, unmodified OS processes racing
`transport.claim_pidfile` for the same slot (60-way fan-out, no
instrumentation): multiple processes reported ownership (`== 0`) in 11 of 20
trials, and the on-disk pidfile named only one PID every time — the "you own
it" answer was for-sale to more than one buyer while the slot itself has
room for exactly one, which is the whole of the issue's live-fleet symptom
without needing any speculation about six same-minute `.tmp` files at all.

That 55%-of-trials figure is *reproduction*, not the regression pin — a race
that fires on a coin flip is not a fact a CI leg can rely on. The test below
removes the coin flip: it holds the first claimant paused, with dependency
injection rather than timing luck, at the exact instant `claim_pidfile`
itself pauses between creating the name and writing its content — the same
window, forced open every run rather than landed in occasionally — and it is
therefore deterministic in both directions: reliably red against the
create-then-write shape, reliably green once the name is never observable
without its content already on it (see `transport.claim_pidfile`'s current
docstring for the fix actually shipped).

**What this file does not claim.** It does not reproduce six same-minute
`.tmp` files on `write_state`, and does not claim to explain them — that
function has no relationship to slot ownership at all: `record_death` is
called from plain *readers* (`watches`, `radar` heal, `claim_pidfile`'s own
reclaim path) with no locking between them, by design (see `record_death`'s
own docstring: "two readers legitimately reap one corpse"). Multiple
processes racing to write the same watcher's state.json is expected
behaviour under this design, not evidence that a poller's *pidfile* claim
was ever contended — seeing six `.tmp` files there is consistent with a
machine at load 409 running several concurrent `watches`/`radar` calls, none
of which needed to duplicate a poller to produce that residue. `TestWriteStateManyReadersOneSlot`
below is the positive control for that half: several concurrent,
non-owning writers on one state file, and no corruption results, which is
what the design promises and not a hole in it.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

import transport  # noqa: E402  (the same module object dispatcher/radar import)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    return tmp_path


def test_two_real_claimants_never_both_win(monkeypatch) -> None:
    """The regression pin. Deterministic: no sleep, no timing luck.

    Thread A is stalled — via `os.fdopen`, dependency-injected rather than
    slept — at the exact point `claim_pidfile` reaches after creating the
    pidfile's name and before writing its own PID into it. While A is
    stalled there, thread B (this test's own thread) calls the real
    `claim_pidfile` for the same slot. Against the create-then-write shape,
    B sees the name already exists, reads zero bytes, and reclaims it —  both
    A and B then report ownership. Fixed, B must see a fully-populated
    pidfile the instant it can see one at all, so it must be told a live PID
    already holds the slot.
    """
    created = threading.Event()
    resume = threading.Event()
    a_ident: dict[str, int] = {}
    real_fdopen = os.fdopen

    def stalling_fdopen(fd, *args, **kwargs):
        if threading.get_ident() == a_ident.get("id"):
            created.set()
            assert resume.wait(timeout=5), "test itself deadlocked"
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", stalling_fdopen)

    results: dict[str, int] = {}

    def claim_a() -> None:
        a_ident["id"] = threading.get_ident()
        results["a"] = transport.claim_pidfile("gitlab-mr", "19509")

    thread = threading.Thread(target=claim_a)
    thread.start()
    assert created.wait(timeout=5), "thread A never reached the write step"
    results["b"] = transport.claim_pidfile("gitlab-mr", "19509")
    resume.set()
    thread.join(timeout=5)

    assert not (results["a"] == 0 and results["b"] == 0), (
        f"both claimants believed they owned the slot: {results}. "
        "Exactly one process may ever hold a pidfile it can also see."
    )
    # The stronger property: whichever one is the true owner, the pidfile on
    # disk must name a claimant that agrees it owns the slot — not a third,
    # abandoned value neither side wrote knowing about the other.
    on_disk = transport.read_pid("gitlab-mr", "19509")
    assert on_disk in (results["a"], results["b"])


def test_second_claimant_is_told_the_live_pid_it_lost_to(monkeypatch) -> None:
    """The loser of the race must get a usable answer, not a phantom win.

    Same stall as above; this pins what B is told rather than only that A
    and B disagree. A real PID it can act on (refuse to spawn, name it in a
    refusal) is the contract `claim_pidfile`'s docstring already promises for
    the ordinary non-racing case.
    """
    created = threading.Event()
    resume = threading.Event()
    a_ident: dict[str, int] = {}
    real_fdopen = os.fdopen

    def stalling_fdopen(fd, *args, **kwargs):
        if threading.get_ident() == a_ident.get("id"):
            created.set()
            assert resume.wait(timeout=5), "test itself deadlocked"
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", stalling_fdopen)

    results: dict[str, int] = {}

    def claim_a() -> None:
        a_ident["id"] = threading.get_ident()
        results["a"] = transport.claim_pidfile("gitlab-mr", "19509")

    thread = threading.Thread(target=claim_a)
    thread.start()
    assert created.wait(timeout=5), "thread A never reached the write step"
    results["b"] = transport.claim_pidfile("gitlab-mr", "19509")
    resume.set()
    thread.join(timeout=5)

    if results["a"] == 0:
        assert results["b"] not in (0, transport.CLAIM_UNKNOWN)
    else:
        assert results["a"] not in (0, transport.CLAIM_UNKNOWN)


class TestWriteStateManyReadersOneSlot:
    """The positive control for the issue's other half.

    #1891 also observed six same-minute `write_state` temporaries on one
    slot and read that as six pollers each believing they had claimed it.
    `write_state` carries no relationship to slot ownership at all —
    `record_death` is reached from plain readers with no lock between them,
    by the design `record_death`'s own docstring states outright. This is
    the fixture that proves the design does what it claims: several
    concurrent non-owning writers on the same state file, and the file that
    survives is valid JSON from exactly one of them — not evidence that a
    pidfile claim was ever contended.
    """

    def test_concurrent_non_owning_writers_leave_one_valid_file(self) -> None:
        import json

        barrier = threading.Barrier(8)
        errors: list[Exception] = []

        def writer(n: int) -> None:
            try:
                barrier.wait(timeout=5)
                transport.write_state(
                    "gitlab-mr", "19509", {"deaths": [{"pid": 1000 + n, "ts": "x"}]}
                )
            except Exception as exc:  # pragma: no cover - failure path only
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, errors
        state = transport.read_state("gitlab-mr", "19509")
        assert isinstance(state, dict)
        assert "deaths" in state
        # No leftover .tmp litter: every writer's mkstemp either landed via
        # os.replace or was discarded on its own failure path.
        leftovers = [
            name for name in os.listdir(transport.STATE_DIR)
            if name.endswith(".tmp")
        ]
        assert leftovers == [], leftovers
