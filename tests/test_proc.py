"""Tests for presets/_proc.py — the one process-liveness probe.

The probe is a *read-only* question. The tests that matter here are the ones a
destructive implementation would fail: `os.kill(pid, 0)` returns False for a
dead PID just as happily as the read-only probe does, so "returns False for a
bad pid" proves nothing. What proves something is asking about a **live**
process under Windows semantics and finding it still running afterwards.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PRESETS_DIR = Path(__file__).parent.parent / "presets"
sys.path.insert(0, str(PRESETS_DIR))

import _proc  # noqa: E402

_REAL_KILL = os.kill


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS_DIR / relpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# The post-condition: probing a live process must not end it
# ---------------------------------------------------------------------------

@pytest.fixture
def live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _simulate_windows(monkeypatch) -> None:
    """Make os.kill behave the way CPython documents it on Windows.

    Any signal other than CTRL_C_EVENT/CTRL_BREAK_EVENT is routed to
    TerminateProcess, and OpenProcess on a PID that does not exist fails with
    WinError 87. Both are what makes the POSIX idiom wrong there.
    """
    monkeypatch.setattr(_proc.sys, "platform", "win32")

    def _kill(pid: int, _sig: int) -> None:
        try:
            _REAL_KILL(pid, signal.SIGKILL)
        except ProcessLookupError:
            raise OSError(87, "The parameter is incorrect") from None

    monkeypatch.setattr(_proc.os, "kill", _kill)


def test_probing_a_live_process_under_windows_semantics_does_not_kill_it(
    monkeypatch, live_process
) -> None:
    """The whole point. `os.kill(pid, 0)` here would terminate the watcher."""
    _simulate_windows(monkeypatch)
    _fake_windows(monkeypatch, exit_code=_proc.WIN_STILL_ACTIVE)

    assert _proc.pid_alive(live_process.pid) is True

    time.sleep(0.2)
    assert live_process.poll() is None, "the probe terminated the process it was asked about"


def test_probing_a_dead_pid_under_windows_semantics_answers_rather_than_raises(
    monkeypatch,
) -> None:
    """WinError 87 is neither ProcessLookupError nor PermissionError (#422)."""
    _simulate_windows(monkeypatch)

    def _boom(_pid: int) -> bool:
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(_proc, "pid_alive_windows", _boom)
    assert _proc.pid_alive(4242) is False


def test_a_live_process_is_reported_alive_on_posix(live_process) -> None:
    assert _proc.pid_alive(live_process.pid) is True
    assert live_process.poll() is None


# ---------------------------------------------------------------------------
# The Windows probe itself, exercised from a POSIX runner
# ---------------------------------------------------------------------------

class _FakeKernel32:
    """Stubs the three read-only calls the probe is allowed to make.

    Any other attribute raises, so a probe that reached for TerminateProcess
    (what `os.kill(pid, 0)` does on Windows) fails loudly instead of silently
    killing the watcher it was asked about.
    """

    def __init__(self, handle=1234, exit_code=259, query_ok=True):
        self.handle = handle
        self.exit_code = exit_code
        self.query_ok = query_ok
        self.opened: list = []
        self.closed: list = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return self.handle

    def GetExitCodeProcess(self, handle, ptr):
        if not self.query_ok:
            return 0
        ptr._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def __getattr__(self, name):
        raise AssertionError(f"the liveness probe must not call {name}")


def _fake_windows(monkeypatch, **kw) -> _FakeKernel32:
    fake = _FakeKernel32(**kw)
    monkeypatch.setattr(_proc, "kernel32", lambda: fake)
    return fake


def test_the_stub_refuses_terminate_process() -> None:
    """Pins the seam's own guarantee — without it the tests above prove nothing."""
    with pytest.raises(AssertionError):
        # B018: the bare attribute access *is* the call under test — the stub
        # raises on __getattr__, so assigning the result would test nothing new.
        _FakeKernel32().TerminateProcess  # noqa: B018


def test_pid_alive_rejects_a_nonpositive_pid() -> None:
    assert _proc.pid_alive(0) is False
    assert _proc.pid_alive(-1) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX os.kill semantics")
def test_pid_alive_says_yes_for_our_own_process() -> None:
    assert _proc.pid_alive(os.getpid()) is True


def test_pid_alive_says_no_for_a_pid_that_cannot_exist() -> None:
    """The regression: on Windows this raised WinError 87 out of radar."""
    assert _proc.pid_alive(9999999) is False


def test_windows_probe_reports_a_running_process_as_alive(monkeypatch) -> None:
    _fake_windows(monkeypatch, exit_code=259)
    assert _proc.pid_alive_windows(4242) is True


def test_windows_probe_reports_an_exited_process_as_dead(monkeypatch) -> None:
    _fake_windows(monkeypatch, exit_code=0)
    assert _proc.pid_alive_windows(4242) is False


def test_windows_probe_reports_a_missing_process_as_dead(monkeypatch) -> None:
    _fake_windows(monkeypatch, handle=0)
    assert _proc.pid_alive_windows(4242) is False


def test_windows_probe_treats_an_unreadable_exit_code_as_dead(monkeypatch) -> None:
    _fake_windows(monkeypatch, query_ok=False)
    assert _proc.pid_alive_windows(4242) is False


def test_windows_probe_asks_only_for_query_rights(monkeypatch) -> None:
    """PROCESS_ALL_ACCESS (0x1F0FFF) would hand the probe the power to kill."""
    fake = _fake_windows(monkeypatch)
    _proc.pid_alive_windows(4242)
    access, _inherit, pid = fake.opened[0]
    assert access == 0x1000
    assert pid == 4242


def test_windows_probe_closes_the_handle(monkeypatch) -> None:
    fake = _fake_windows(monkeypatch, handle=77)
    _proc.pid_alive_windows(4242)
    assert fake.closed == [77]


def test_windows_probe_closes_the_handle_even_when_the_query_fails(monkeypatch) -> None:
    fake = _fake_windows(monkeypatch, handle=77, query_ok=False)
    _proc.pid_alive_windows(4242)
    assert fake.closed == [77]


def test_pid_alive_uses_the_windows_probe_on_win32(monkeypatch) -> None:
    monkeypatch.setattr(_proc.sys, "platform", "win32")
    seen: list[int] = []
    monkeypatch.setattr(_proc, "pid_alive_windows",
                        lambda pid: (seen.append(pid), True)[1])
    assert _proc.pid_alive(4242) is True
    assert seen == [4242]


def test_pid_alive_never_uses_os_kill_on_win32(monkeypatch) -> None:
    monkeypatch.setattr(_proc.sys, "platform", "win32")
    _fake_windows(monkeypatch)

    def _forbidden(*_a, **_k):
        raise AssertionError("os.kill must not be used for liveness on Windows")

    monkeypatch.setattr(_proc.os, "kill", _forbidden)
    assert _proc.pid_alive(4242) is True


def test_a_raising_windows_probe_resolves_to_not_alive(monkeypatch) -> None:
    """An unanswerable question must not propagate — radar would crash."""
    monkeypatch.setattr(_proc.sys, "platform", "win32")

    def _boom(_pid):
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(_proc, "pid_alive_windows", _boom)
    assert _proc.pid_alive(4242) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX os.kill semantics")
def test_an_unexpected_oserror_on_posix_resolves_to_not_alive(monkeypatch) -> None:
    def _boom(_pid, _sig):
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(_proc.os, "kill", _boom)
    assert _proc.pid_alive(4242) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX os.kill semantics")
def test_a_process_owned_by_someone_else_counts_as_alive(monkeypatch) -> None:
    def _denied(_pid, _sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(_proc.os, "kill", _denied)
    assert _proc.pid_alive(4242) is True


# ---------------------------------------------------------------------------
# One helper — a fourth copy must fail a test, not reappear silently
# ---------------------------------------------------------------------------

CONSUMERS = [
    ("gitlab_mrs", "gitlab/mrs.py"),
    ("github_prs", "github/prs.py"),
    ("watch_transport", "watch/transport.py"),
]


@pytest.mark.parametrize("name,relpath", CONSUMERS)
def test_every_consumer_uses_the_one_shared_probe(name: str, relpath: str) -> None:
    """Identity, not equality: a re-added local copy would pass any behavioural
    test on POSIX and still kill processes on Windows."""
    mod = _load(name, relpath)
    assert mod._pid_alive is _proc.pid_alive


def _defines_a_probe(path: Path) -> bool:
    """Read as UTF-8 explicitly. A bare read_text() decodes by locale, which is
    cp1252 on Windows — the seam #418 is about, and it made this very test red
    on all four Windows legs against preset sources holding glyphs."""
    src = path.read_text(encoding="utf-8")
    return "def pid_alive" in src or "def _pid_alive" in src


def _null_signal_kills(path: Path) -> bool:
    """True if the file really calls `os.kill(x, 0)`. UTF-8, not locale.

    Parsed rather than grepped, so the prose explaining the bug in the files
    that no longer contain it does not count as containing it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) != 2:
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "kill"):
            continue
        sig = node.args[1]
        if isinstance(sig, ast.Constant) and sig.value == 0:
            return True
    return False


def test_no_preset_probes_liveness_with_os_kill() -> None:
    """`os.kill(pid, 0)` anywhere outside _proc is another copy of the bug —
    on Windows it terminates the process instead of reporting on it."""
    offenders = sorted(
        str(path.relative_to(PRESETS_DIR))
        for path in PRESETS_DIR.rglob("*.py")
        if path.name != "_proc.py" and _null_signal_kills(path)
    )
    assert offenders == []


def test_proc_really_is_where_the_idiom_lives() -> None:
    """Guards the test above from passing because the check stopped working."""
    assert _null_signal_kills(PRESETS_DIR / "_proc.py") is True


def test_proc_is_the_only_definition_of_the_probe() -> None:
    """Counts definitions, so a copy under a different name still trips."""
    definitions = sorted(
        str(path.relative_to(PRESETS_DIR))
        for path in PRESETS_DIR.rglob("*.py")
        if _defines_a_probe(path)
    )
    assert definitions == ["_proc.py"]


def test_the_source_scans_do_not_decode_by_locale() -> None:
    """#418's seam. A bare `read_text()` decodes with
    `locale.getpreferredencoding()` — cp1252 on Windows — so these two scans
    died with `UnicodeDecodeError: 'charmap' codec` on every Windows leg,
    against preset sources that hold glyphs. Run under
    `-X warn_default_encoding` with EncodingWarning promoted to an error, so a
    reintroduced bare read is caught by this suite on any OS rather than
    discovered by CI on one.
    """
    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "import test_proc\n"
        "test_proc._null_signal_kills(test_proc.PRESETS_DIR / '_proc.py')\n"
        "test_proc._defines_a_probe(test_proc.PRESETS_DIR / '_proc.py')\n"
        % str(Path(__file__).parent)
    )
    r = subprocess.run(
        [sys.executable, "-X", "warn_default_encoding", "-W", "error::EncodingWarning",
         "-c", probe],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
