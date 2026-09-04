"""`watch:SOURCE:ID:reload` re-imports a poller's module without losing its
state (#2212).

`dispatcher._load_source` imports a source's `poller.py` once and
`_run_poll_loop` reuses that module object for the process's life; `INTERVAL`
is read once at spawn too. So a merged fix does nothing until the operator
runs `unwatch` then `watch` -- which spawns a fresh process with an empty
`state`, so the very first tick re-announces everything the old process
already knew about as new.

The fix here is a signal, not a config knob and not an automatic per-tick
reimport (the issue names three shapes and states a preference for this one,
the second of the three, precisely because it does not lose the baseline and
does not risk an automatic reload killing every watcher on a broken edit).
`RELOAD_SIGNAL` (SIGHUP where the platform has one) is handled inside the
SAME poll loop that already owns `state`; the dispatcher process that sends
it never touches `state` at all -- only which module the loop calls changes,
in the poller's own process, in its own memory.

Every "must reload" case here has a "must not fire" partner: a run where
reload is never requested must never call `_load_source` a second time and
must never emit either reload event, or a fix that always reloads regardless
of the signal would pass the same assertions a correct one does.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import sys
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

import transport  # noqa: E402  (the same module object dispatcher imports)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatcher = _load("watch_dispatcher_2212", WATCH_DIR / "dispatcher.py")

SOURCE = "gitlab-mr"
WATCHER = "2212"


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    for mod in (transport, dispatcher.transport):
        monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(dispatcher, "_silence_stdio", lambda: None)
    monkeypatch.setattr(transport, "_pid_alive", lambda pid: pid == os.getpid())
    monkeypatch.setattr(dispatcher.time, "sleep", lambda _s: None)


@pytest.fixture
def emitted(monkeypatch):
    """Every `emit_event` call the loop makes, without touching a real socket."""
    calls: list[dict] = []

    def _emit(source, watcher_id, event_key, payload, **kw):
        calls.append({"source": source, "id": watcher_id,
                      "event": event_key, "payload": payload, **kw})

    monkeypatch.setattr(dispatcher.transport, "emit_event", _emit)
    return calls


class _Poller:
    """A source whose poll outcome is scripted, and which counts its polls.

    `script(n)` is consulted for poll `n` (1-based) and returns the state
    dict to hand back -- or the literal string `"stop"` to end the loop by
    reaching a terminal state.
    """

    INTERVAL = 0
    LABEL = "old"

    def __init__(self, script) -> None:
        self.script = script
        self.polls = 0
        self.seen_states: list[dict] = []

    def poll(self, state, ctx):
        self.polls += 1
        self.seen_states.append(dict(state))
        if self.polls > 200:
            raise AssertionError("the loop ran unbounded -- nothing stopped it")
        outcome = self.script(self.polls)
        return [], outcome

    def is_terminal(self, state):
        return state.get("done") is True


def _run(monkeypatch, load_source) -> None:
    monkeypatch.setattr(dispatcher, "_load_source", load_source)
    dispatcher._run_poll_loop(SOURCE, WATCHER, [])


# ---------------------------------------------------------------------------
# the reload swaps the module, and keeps the state
# ---------------------------------------------------------------------------

def test_a_requested_reload_swaps_the_poller_module_and_keeps_state(
        monkeypatch, emitted) -> None:
    """After the flag is raised mid-run, the NEXT tick's `poll` is the new
    module's, not the one the loop started with -- and it is handed the
    state the OLD module's last tick produced, never an empty one."""
    ticks: list[str] = []

    class _Old:
        """Never reaches a terminal state on its own -- if the reload is not
        picked up, this loop must be stopped by the hard cap below, not run
        until the test runner kills it."""
        INTERVAL = 0

        def poll(self, state, ctx):
            ticks.append("old")
            n = len(ticks)
            if n > 200:
                raise AssertionError(
                    "the old module is still being polled after 200 ticks -- "
                    "the reload was never picked up")
            if n == 2:
                dispatcher._RELOAD_FLAG["reload"] = True
            return [], {"seen": n}

        def is_terminal(self, state):
            return False

    class _New:
        INTERVAL = 0

        def poll(self, state, ctx):
            ticks.append("new")
            self.state_at_first_tick = dict(state)
            return [], {"seen": state.get("seen"), "done": True}

        def is_terminal(self, state):
            return state.get("done") is True

    old, new = _Old(), _New()
    load_calls = {"n": 0}

    def load_source(_name):
        load_calls["n"] += 1
        return old if load_calls["n"] == 1 else new

    monkeypatch.setattr(dispatcher, "_load_source", load_source)
    dispatcher._run_poll_loop(SOURCE, WATCHER, [])

    assert load_calls["n"] == 2, "the reload must re-import, not reuse the old module"
    assert ticks == ["old", "old", "new"], (
        f"expected two ticks on the old module then one on the new one, got {ticks}")
    assert new.state_at_first_tick == {"seen": 2}, (
        "the reloaded module must inherit the running loop's own state, "
        "not start from nothing")
    assert [e["event"] for e in emitted] == [dispatcher.RELOAD_EVENT]


def test_a_reload_that_is_never_requested_never_reimports(monkeypatch, emitted) -> None:
    """The positive control: with no signal, `_load_source` is called exactly
    once (at spawn) and no reload event is ever emitted."""
    poller = _Poller(lambda n: {"done": n >= 3})
    calls = {"n": 0}

    def load_source(_name):
        calls["n"] += 1
        return poller

    monkeypatch.setattr(dispatcher, "_load_source", load_source)
    dispatcher._run_poll_loop(SOURCE, WATCHER, [])

    assert calls["n"] == 1, "no reload was requested -- _load_source must not be re-run"
    reload_events = {e["event"] for e in emitted} & {
        dispatcher.RELOAD_EVENT, dispatcher.RELOAD_FAILED_EVENT}
    assert reload_events == set()


# ---------------------------------------------------------------------------
# `_reload_poller` in isolation -- success and both failure shapes
# ---------------------------------------------------------------------------

def test_reload_poller_returns_the_new_module_and_emits_reloaded(monkeypatch, emitted) -> None:
    old, new = object(), object()
    monkeypatch.setattr(dispatcher, "_load_source", lambda _n: new)
    result = dispatcher._reload_poller(SOURCE, WATCHER, old)
    assert result is new
    assert [e["event"] for e in emitted] == [dispatcher.RELOAD_EVENT]


def test_reload_poller_keeps_the_old_module_when_import_raises(monkeypatch, emitted) -> None:
    old = object()

    def load_source(_n):
        raise SyntaxError("broken edit")

    monkeypatch.setattr(dispatcher, "_load_source", load_source)
    result = dispatcher._reload_poller(SOURCE, WATCHER, old)
    assert result is old, "a broken edit must not end the watcher's own module"
    assert [e["event"] for e in emitted] == [dispatcher.RELOAD_FAILED_EVENT]
    assert "SyntaxError" in emitted[0]["payload"]["error"]


def test_reload_poller_keeps_the_old_module_when_the_source_no_longer_resolves(
        monkeypatch, emitted) -> None:
    """The other failure shape: `_load_source` returns None rather than
    raising -- e.g. the search path changed under the running watcher."""
    old = object()
    monkeypatch.setattr(dispatcher, "_load_source", lambda _n: None)
    result = dispatcher._reload_poller(SOURCE, WATCHER, old)
    assert result is old
    assert [e["event"] for e in emitted] == [dispatcher.RELOAD_FAILED_EVENT]


# ---------------------------------------------------------------------------
# `watch:SOURCE:ID:reload` -- signals the live poller(s), does not spawn
# ---------------------------------------------------------------------------

class _Machine:
    """A fake process table, like #511's -- `os.kill` is recorded, nothing real."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, list[str]]] = []
        self.alive: set[int] = set()
        self.signalled: list[tuple[int, int]] = []
        self.scan_ok = True

    def add_poller(self, pid: int, source: str, watcher_id: str) -> int:
        self.rows.append((pid, transport.poller_argv(source, watcher_id, [])))
        self.alive.add(pid)
        return pid

    def ps_rows(self):
        if not self.scan_ok:
            return None
        return [(pid, argv) for pid, argv in self.rows if pid in self.alive]

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive

    def kill(self, pid: int, sig: int) -> None:
        self.signalled.append((pid, sig))
        if pid not in self.alive:
            raise ProcessLookupError(f"no such process {pid}")

    def install(self, monkeypatch) -> "_Machine":
        monkeypatch.setattr(transport, "_ps_rows", self.ps_rows)
        monkeypatch.setattr(transport, "_pid_alive", self.pid_alive)
        monkeypatch.setattr(dispatcher.os, "kill", self.kill)
        return self


@pytest.fixture
def machine(monkeypatch) -> _Machine:
    return _Machine().install(monkeypatch)


@pytest.mark.skipif(dispatcher.RELOAD_SIGNAL is None, reason="requires SIGHUP")
def test_reload_signals_the_tracked_pid_and_never_spawns(machine, monkeypatch) -> None:
    machine.add_poller(4242, SOURCE, WATCHER)
    transport.record_pid(SOURCE, WATCHER, 4242)
    spawned = []
    monkeypatch.setattr(dispatcher, "start_poller",
                        lambda *a, **k: spawned.append(a) or ("spawned", 1))
    assert dispatcher.cmd_watch([SOURCE, WATCHER, "reload"]) == 0
    assert machine.signalled == [(4242, dispatcher.RELOAD_SIGNAL)]
    assert spawned == [], "a reload must never spawn a poller"


def test_reload_with_nothing_running_reports_and_does_not_spawn(machine, monkeypatch) -> None:
    """The must-not-fire twin: no live poller for this slot -- nothing signalled."""
    spawned = []
    monkeypatch.setattr(dispatcher, "start_poller",
                        lambda *a, **k: spawned.append(a) or ("spawned", 1))
    assert dispatcher.cmd_watch([SOURCE, WATCHER, "reload"]) == 1
    assert machine.signalled == []
    assert spawned == []


@pytest.mark.skipif(dispatcher.RELOAD_SIGNAL is None, reason="requires SIGHUP")
def test_reload_signals_every_live_poller_on_the_slot(machine, monkeypatch) -> None:
    """A slot can hold more than one poller (#511's own shape) -- reload
    reaches all of them, the same breadth `unwatch` already commits to."""
    # A reload must never spawn -- guarded here too, not only in the
    # single-PID test, so a regression that fell through to the spawn path
    # cannot fork a REAL process out of this test run.
    monkeypatch.setattr(dispatcher, "start_poller",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("reload must never spawn")))
    machine.add_poller(101, SOURCE, WATCHER)
    machine.add_poller(102, SOURCE, WATCHER)
    transport.record_pid(SOURCE, WATCHER, 101)
    assert dispatcher.cmd_watch([SOURCE, WATCHER, "reload"]) == 0
    assert {pid for pid, _sig in machine.signalled} == {101, 102}


@pytest.mark.skipif(dispatcher.RELOAD_SIGNAL is None, reason="requires SIGHUP")
def test_reload_uses_sighup_not_sigterm(machine, monkeypatch) -> None:
    """The signal must not be one that already means something else to the
    loop -- SIGTERM/SIGINT both mean stop."""
    monkeypatch.setattr(dispatcher, "start_poller",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("reload must never spawn")))
    assert dispatcher.RELOAD_SIGNAL not in (signal.SIGTERM, signal.SIGINT)
    machine.add_poller(555, SOURCE, WATCHER)
    transport.record_pid(SOURCE, WATCHER, 555)
    dispatcher.cmd_watch([SOURCE, WATCHER, "reload"])
    assert machine.signalled == [(555, dispatcher.RELOAD_SIGNAL)]


def test_the_change_is_findable():
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(2212)
