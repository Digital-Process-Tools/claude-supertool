"""`watch:SOURCE:ID:reload` never tells the caller that dispatcher.py itself
is not reloaded by this signal (#2518).

`cmd_reload` signals SIGHUP to a live poller, and the running process's own
next tick re-imports only that source's `poller.py` -- `dispatcher.py` (the
shared back-off/retry/wait machinery every poller runs under, including
`_retry_after_seconds`, `_wait_interruptible` and `MAX_RETRY_AFTER_SECONDS`)
is already imported by the running process and is never swapped by this
signal. #2518's maintainer reloaded three pollers expecting a
dispatcher.py-level fix (#2515) to take effect, and it did not, because the
fix lived in the wrong file for what `reload` actually swaps -- and nothing
in the receipt said so.

The fix here is a static, unconditional caveat printed on every successful
reload, not a dynamic diff of the two files: `cmd_reload` has no way to
know which file changed. The must-fire case below is paired with the
existing `test_reload_signals_the_tracked_pid_and_never_spawns` in
`test_watch_reload_2212.py`, which is the must-still-work control -- a
successful reload must still report success and the swap confirmation
exactly as it does today; this is additive text only.
"""
from __future__ import annotations

import importlib.util
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


dispatcher = _load("watch_dispatcher_2518", WATCH_DIR / "dispatcher.py")

SOURCE = "gitlab-mr"
WATCHER = "2518"


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    for mod in (transport, dispatcher.transport):
        monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path))
    return tmp_path


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
def test_reload_receipt_says_dispatcher_py_is_not_reloaded(
        machine, monkeypatch, capsys) -> None:
    """The must-fire case: a real, successful reload's own printed receipt
    must say -- in the receipt itself, not only in the docstring -- that
    dispatcher.py is not what this signal swaps."""
    machine.add_poller(4242, SOURCE, WATCHER)
    transport.record_pid(SOURCE, WATCHER, 4242)
    monkeypatch.setattr(dispatcher, "start_poller",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("reload must never spawn")))

    assert dispatcher.cmd_watch([SOURCE, WATCHER, "reload"]) == 0

    out = capsys.readouterr().out
    assert "dispatcher.py" in out, (
        "the receipt never names dispatcher.py as the file this signal "
        "does not reach"
    )
    assert "poller.py" in out


@pytest.mark.skipif(dispatcher.RELOAD_SIGNAL is None, reason="requires SIGHUP")
def test_reload_still_reports_success_and_swap_confirmation(
        machine, monkeypatch, capsys) -> None:
    """The must-still-work control: the existing success line and swap
    confirmation must survive unchanged alongside the new caveat."""
    machine.add_poller(4242, SOURCE, WATCHER)
    transport.record_pid(SOURCE, WATCHER, 4242)
    monkeypatch.setattr(dispatcher, "start_poller",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("reload must never spawn")))

    assert dispatcher.cmd_watch([SOURCE, WATCHER, "reload"]) == 0

    out = capsys.readouterr().out
    assert "Signalled PID 4242" in out
    assert f"{dispatcher.RELOAD_FAILED_EVENT}" in out
    assert f"{dispatcher.RELOAD_EVENT}" in out


def test_the_change_is_findable():
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(2518)
