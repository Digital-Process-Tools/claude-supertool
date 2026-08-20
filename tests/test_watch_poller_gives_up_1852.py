"""A poller whose `poll()` keeps raising has to stop eventually (#1852).

On a working machine, 22 `dispatcher.py` pollers were alive at once and one had
been running eight days against an MR that stopped being interesting long before
that. The normal path is fine -- `_run_poll_loop` breaks when the source says
the state is terminal. The error branch never consulted it:

    except Exception as e:
        ...write last_error...
        time.sleep(interval)
        continue

`continue` returns to the top without a terminal check, and it cannot make one,
because a failed poll produced no new state. An expired token, a deleted MR, a
renamed project or a source module that no longer imports therefore polled
forever, writing `last_error` each round for nobody, holding a pidfile that only
a reboot or a manual `unwatch` would release.

**The assertion here is half a silence, so every silence is paired.** "The
poller stopped" is observed as an absence of further polling, and an absence is
what a harness that never started the poller produces too. So each "must stop"
case below has a "must keep going" twin driven through the same fixture: a
poller that keeps succeeding is polled well past the bound, and a run of
failures broken by one success starts its count again. A fix that ended every
poller would pass the first half and fail the second, which is the point.

**Nothing here is a wall-clock assertion.** `time.sleep` is a no-op recorder, so
"the loop waited an interval" is a recorded duration rather than an elapsed one.

**What this deliberately does not test, because it must never be built.**
`watches` computes `EMIT_NO_LISTENER` per row and refuses to conclude anything
about the poller from it: a session started without the channel server binds no
reader at all, and that is the expected state rather than a fault. #511 records
that acting on it cost two live watchers. The bound below counts *this poller's
own failures* and reads nothing about the socket.
"""
from __future__ import annotations

import importlib.util
import json
import os
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


dispatcher = _load("watch_dispatcher_1852", WATCH_DIR / "dispatcher.py")

SOURCE = "gitlab-mr"
WATCHER = "1852"

#: A poll count no correct implementation can reach. A loop that is still
#: unbounded raises this out of `_run_poll_loop` -- it is a `BaseException`, so
#: the loop's own `except Exception` cannot swallow it -- and the test fails
#: naming the count instead of hanging until the runner kills it.
class _Runaway(BaseException):
    pass


HARD_STOP = 5000


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    for mod in (transport, dispatcher.transport):
        monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Record what the loop asked to wait for; never actually wait."""
    slept: list[float] = []
    monkeypatch.setattr(dispatcher.time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(dispatcher, "_silence_stdio", lambda: None)
    monkeypatch.setattr(transport, "_pid_alive", lambda pid: pid == os.getpid())


@pytest.fixture
def emitted(monkeypatch):
    """Every `emit_event` the loop makes, without touching a real socket."""
    calls: list[dict] = []

    def _emit(source, watcher_id, event_key, payload, **kw):
        calls.append({"source": source, "id": watcher_id,
                      "event": event_key, "payload": payload, **kw})

    monkeypatch.setattr(dispatcher.transport, "emit_event", _emit)
    return calls


class _Poller:
    """A source whose poll outcome is scripted, and which counts its polls.

    `script(n)` is consulted for poll `n` (1-based) and returns either the
    string "fail" or the state dict to hand back.
    """

    INTERVAL = 0

    def __init__(self, script, terminal_state=None) -> None:
        self.script = script
        self.terminal_state = terminal_state
        self.polls = 0

    def poll(self, state, ctx):
        self.polls += 1
        if self.polls > HARD_STOP:
            raise _Runaway(
                f"the poll loop reached {self.polls} polls without stopping. "
                f"Every one of them raised; nothing bounds the error branch.")
        outcome = self.script(self.polls)
        if outcome == "fail":
            raise RuntimeError("glab: the project could not be found")
        return [{"event": "pipeline_failed", "payload": {"n": self.polls}}], outcome

    def is_terminal(self, state):
        return (self.terminal_state is not None
                and state == self.terminal_state)


def _run(monkeypatch, poller, only=None) -> None:
    monkeypatch.setattr(dispatcher, "_load_source", lambda _n: poller)
    dispatcher._run_poll_loop(SOURCE, WATCHER, only or [])


def _bound(poller=None) -> int:
    """The number of consecutive failures this poller is allowed."""
    default = getattr(dispatcher, "MAX_CONSECUTIVE_POLL_FAILURES", None)
    assert default is not None, (
        "dispatcher exposes no MAX_CONSECUTIVE_POLL_FAILURES, so nothing "
        "bounds the error branch and a failing poller runs until reboot")
    return int(getattr(poller, "MAX_CONSECUTIVE_FAILURES", default))


# ---------------------------------------------------------------------------
# the bound is finite, and it is generous
# ---------------------------------------------------------------------------

def test_the_shipped_bound_is_finite_and_not_a_hair_trigger() -> None:
    """A network blip must not end a watcher, and neither must forever."""
    bound = _bound()
    assert bound > 10, (
        f"{bound} consecutive failures is close enough to a blip that a "
        f"GitLab maintenance window would end live watchers")
    assert bound < HARD_STOP


# ---------------------------------------------------------------------------
# must stop -- and its twin, must not stop
# ---------------------------------------------------------------------------

def test_a_poller_whose_poll_always_raises_stops(monkeypatch) -> None:
    poller = _Poller(lambda _n: "fail")
    _run(monkeypatch, poller)
    assert poller.polls == _bound(poller), (
        f"stopped after {poller.polls} polls, expected the bound "
        f"{_bound(poller)}")


def test_a_poller_whose_polls_keep_working_is_not_stopped_by_that_bound(
        monkeypatch) -> None:
    """The positive control. Without it, "the poller stopped" is satisfied by
    a fix that stops every poller, and by a harness that never started one."""
    bound = _bound()
    done = {"state": "merged"}
    poller = _Poller(
        lambda n: done if n > bound + 5 else {"state": f"running-{n}"},
        terminal_state=done)
    _run(monkeypatch, poller)
    assert poller.polls == bound + 6, (
        f"a succeeding poller stopped after {poller.polls} polls; the "
        f"consecutive-failure bound is counting successes")


def test_one_success_starts_the_count_again(monkeypatch) -> None:
    """"Consecutive" is the whole guarantee. A flaky forge that answers one
    poll in ten is a watcher that keeps working, not one that gives up."""
    bound = _bound()
    done = {"state": "merged"}

    def script(n: int):
        if n > 4 * bound:
            return done
        # A success every (bound - 1) failures: never a full run of `bound`.
        # The success is a *non*-terminal state, or the loop would leave by the
        # terminal branch and this would assert nothing about the bound.
        return {"state": f"running-{n}"} if n % (bound - 1) == 0 else "fail"

    poller = _Poller(script, terminal_state=done)
    _run(monkeypatch, poller)
    assert poller.polls == 4 * bound + 1, (
        f"stopped after {poller.polls} polls; a run of failures broken by a "
        f"success is still being counted as consecutive")


def test_a_source_may_raise_or_lower_the_bound_for_itself(monkeypatch) -> None:
    class _Impatient(_Poller):
        MAX_CONSECUTIVE_FAILURES = 3

    poller = _Impatient(lambda _n: "fail")
    _run(monkeypatch, poller)
    assert poller.polls == 3


# ---------------------------------------------------------------------------
# what giving up leaves behind
# ---------------------------------------------------------------------------

def test_giving_up_releases_the_pidfile(monkeypatch) -> None:
    """The symptom the issue was filed for: 22 processes holding slots.

    A give-up leaves by the same `finally` a terminal exit leaves by, so the
    slot is handed back and radar can re-arm it.
    """
    _run(monkeypatch, _Poller(lambda _n: "fail"))
    assert not os.path.exists(transport.pid_path(SOURCE, WATCHER))


def test_a_terminal_exit_still_releases_it_too(monkeypatch) -> None:
    """The control for the assertion above: an absent pidfile proves nothing
    unless the ordinary exit is producing one and removing it."""
    done = {"state": "merged"}
    _run(monkeypatch, _Poller(lambda _n: done, terminal_state=done))
    assert not os.path.exists(transport.pid_path(SOURCE, WATCHER))


def test_giving_up_keeps_the_state_file_and_says_why(monkeypatch) -> None:
    """A terminal exit clears the state, because there is nothing to explain.
    A give-up is the opposite case: the record of why coverage ended is the
    only thing that lets the board offer a re-arm rather than a mystery."""
    _run(monkeypatch, _Poller(lambda _n: "fail"))
    state = transport.read_state(SOURCE, WATCHER)
    assert state, "the give-up cleared the state file, deleting the reason"
    assert state.get("last_error", {}).get("message")
    assert state.get("gave_up"), (
        "nothing in the state file distinguishes a poller that gave up from "
        "one that was killed, which is the row the board has to render")


def test_a_terminal_exit_still_clears_its_state(monkeypatch) -> None:
    """The control: keeping the give-up state must not have been bought by
    keeping every state."""
    done = {"state": "merged"}
    _run(monkeypatch, _Poller(lambda _n: done, terminal_state=done))
    assert transport.read_state(SOURCE, WATCHER) == {}


# ---------------------------------------------------------------------------
# somebody has to be told
# ---------------------------------------------------------------------------

def test_giving_up_emits_an_event(monkeypatch, emitted) -> None:
    _run(monkeypatch, _Poller(lambda _n: "fail"))
    keys = [c["event"] for c in emitted]
    assert dispatcher.GAVE_UP_EVENT in keys, (
        f"the poller stopped and said nothing on the channel; emitted {keys}")


def test_the_give_up_event_survives_an_only_filter(monkeypatch, emitted) -> None:
    """`only=` names a *source's* vocabulary. A watcher that filtered its own
    obituary away would stop, hold nothing, and report nothing -- the same
    silence the issue is about, one layer further in."""
    _run(monkeypatch, _Poller(lambda _n: "fail"), only=["mr_merged"])
    assert dispatcher.GAVE_UP_EVENT in [c["event"] for c in emitted]


def test_only_still_filters_the_source_events_it_is_for(
        monkeypatch, emitted) -> None:
    """The control for the exemption above: `only=` has to keep working."""
    done = {"state": "merged"}
    _run(monkeypatch, _Poller(lambda _n: done, terminal_state=done),
         only=["mr_merged"])
    assert "pipeline_failed" not in [c["event"] for c in emitted]


def test_the_give_up_event_is_not_in_any_sources_declared_vocabulary() -> None:
    """It is the dispatcher's, not a source's. A key in an `events.json` is a
    promise that this source can emit it and that `only=` can select it;
    neither is true here, and both source suites assert that file is exactly
    what their poller emits."""
    declared = set()
    for events_json in (WATCH_DIR / "sources").glob("*/events.json"):
        body = json.loads(events_json.read_text(encoding="utf-8"))
        declared |= {e["key"] for e in body["events"]}
    assert declared, "found no events.json at all -- this test proved nothing"
    assert dispatcher.GAVE_UP_EVENT not in declared


# ---------------------------------------------------------------------------
# the error branch waits the way the success branch waits
# ---------------------------------------------------------------------------

def test_the_error_backoff_is_interruptible(monkeypatch, no_real_sleep) -> None:
    """`unwatch` sends SIGTERM and the handler sets a flag. The success branch
    sleeps in one-second steps and checks the flag between them; the error
    branch used one `time.sleep(interval)`, and PEP 475 resumes an interrupted
    sleep for its remaining time -- so a *failing* poller ignored a stop for up
    to a full interval while a working one honoured it within a second. Same
    seven lines, so it goes with this fix rather than after it.
    """
    class _Slow(_Poller):
        INTERVAL = 5
        MAX_CONSECUTIVE_FAILURES = 3

    _run(monkeypatch, _Slow(lambda _n: "fail"))
    assert no_real_sleep, "the error branch did not wait at all between polls"
    assert max(no_real_sleep) <= 1, (
        f"the error branch slept {max(no_real_sleep)}s in one call, so a stop "
        f"arriving just after it starts is not noticed until it ends")
