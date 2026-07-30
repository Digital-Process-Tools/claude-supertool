"""A crash inside `stop.py` must not be read as "no daemon was running" (#574).

CPython exits `1` on an uncaught exception. `_MCP_STOP_CODES` gave `1` the
meaning `("no-daemon", True)` — so a `stop.py` that died before it looked at
anything arrived at `_mcp_stop_server` spelled exactly like its most
reassuring answer, and `ok=True` is the field the new-file invalidation path
(#239) reads as *nothing stale can remain*.

The fix moves `EXIT_NO_DAEMON` off `1` and leaves `1` out of the map
entirely, so it falls to the map's own default — `("crashed", False)` — which
is what the comment above the map claimed all along.

These tests make `stop.py` genuinely raise rather than stubbing an exit code.
A stub proves nothing about a collision whose whole mechanism is that the
interpreter, not the script, chooses the number.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import supertool

_MCP_DIR = Path(__file__).parent.parent / "presets" / "mcp"

# The real stop.py reaches _paths.runtime_dir(), which refuses outright where
# os.geteuid does not exist (#544).
posix_only = pytest.mark.skipif(
    not hasattr(os, "geteuid"),
    reason="stop.py's runtime dir is ownership-checked; os.geteuid is required.",
)


def _crashing_stop_script(tmp_path: Path) -> str:
    """A launcher that runs the *real* `stop.main` with one dependency broken.

    Not a fake exiting `1`: the traceback has to come out of `stop.py` itself,
    through CPython's own handler, or the test asserts nothing about the
    collision it exists to pin. `open_runtime_dir` is the first *callable* thing
    `main` reaches on the named-server path (#598 moved it there; `socket_pid_names`
    now precedes it but is a pure hash that touches no filesystem and cannot
    fail), and `None` is not callable — the same shape as any unanticipated
    `TypeError`/`AttributeError` in there.
    """
    script = tmp_path / "crashing_stop.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(_MCP_DIR)!r})\n"
        "import stop\n"
        "stop.open_runtime_dir = None\n"
        "sys.exit(stop.main(sys.argv))\n",
        encoding="utf-8",
    )
    return str(script)


@posix_only
def test_a_crash_in_stop_py_really_does_exit_1(tmp_path, monkeypatch) -> None:
    """The precondition: this is CPython's number, not one stop.py chose."""
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))

    proc = subprocess.run(
        [sys.executable, _crashing_stop_script(tmp_path), "php-lsp"],
        capture_output=True, timeout=30, check=False,
    )

    assert proc.returncode == 1
    assert b"Traceback (most recent call last):" in proc.stderr


@posix_only
def test_crash_is_not_reported_as_a_successful_invalidation(
    tmp_path, monkeypatch
) -> None:
    """The bug: a traceback out of stop.py must never carry `ok=True`.

    `ok` is what tells the invalidation path that no daemon can still answer
    the next validator from an index captured before the file changed.
    """
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(
        supertool, "_MCP_STOP_SCRIPT", _crashing_stop_script(tmp_path)
    )

    outcome = supertool._mcp_stop_server("php-lsp")

    assert outcome.ok is False
    assert outcome.code != "no-daemon"
    assert outcome.code == "crashed"
    # The tail, not the head: `detail` keeps the last _MCP_STOP_DETAIL_CAP
    # bytes, so the `Traceback (most recent call last):` banner is trimmed and
    # the exception line is what survives — which is the half worth keeping.
    assert "TypeError: 'NoneType' object is not callable" in outcome.detail
    assert "stop.py" in outcome.detail


@posix_only
def test_a_real_no_daemon_run_is_still_benign(tmp_path, monkeypatch) -> None:
    """End to end against the real stop.py: nothing running is still `ok`.

    Pins the two halves against each other. `stop.py`'s `EXIT_NO_DAEMON` and
    `_MCP_STOP_CODES`' benign entry are two files apart, and a fix that moved
    only one of them would leave the common path — every new file, no warm
    daemon — reporting a failed invalidation forever.
    """
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))

    outcome = supertool._mcp_stop_server("never-started-574")

    assert outcome.ok is True
    assert outcome.code == "no-daemon"


@posix_only
def test_exit_1_is_absent_from_the_map(tmp_path, monkeypatch) -> None:
    """No deliberate outcome may reuse the interpreter's crash code.

    Stated as a property of the map rather than of one entry, because the
    defect is not "1 meant the wrong thing" — it is that any meaning at all
    on `1` is unreadable, and the next code added is the one at risk.
    """
    assert 1 not in supertool._MCP_STOP_CODES

    import stop  # noqa: PLC0415

    deliberate = [
        v for k, v in vars(stop).items()
        if k.startswith("EXIT_") and isinstance(v, int)
    ]
    assert 1 not in deliberate
    assert len(deliberate) == len(set(deliberate)), "exit codes must be distinct"


@pytest.fixture(autouse=True)
def _stop_on_path():
    """`presets/mcp` is not a package; the suite imports `stop` by path."""
    added = str(_MCP_DIR)
    if added not in sys.path:
        sys.path.insert(0, added)
    yield
