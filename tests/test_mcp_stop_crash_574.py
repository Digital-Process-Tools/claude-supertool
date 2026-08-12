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
def test_detail_never_carries_terminal_escape_sequences(tmp_path, monkeypatch) -> None:
    """`detail` is text, not a terminal stream (#1333).

    CPython colourises its own tracebacks from 3.13 on, and `_colorize` honours
    `FORCE_COLOR` **before** it asks whether the stream is a tty — so a parent
    with `FORCE_COLOR` in its environment gets an escape-laden traceback out of
    a child whose stderr is a pipe. Not hypothetical: this repo's own agent
    harness exports `FORCE_COLOR=3`, and the assertion in the test above failed
    locally for exactly that reason while every CI leg (3.9-3.12, no colourising
    traceback machinery) stayed green. An environment two conditions wide, not a
    version range.

    `detail` is printed into a debug line and read by a human, so escapes out of
    a child process end up steering the reader's terminal. It is stripped rather
    than suppressed at the source: unsetting `FORCE_COLOR` for the child would
    have to enumerate every layer that might colour, and `stop.py` is not the
    only thing whose stderr lands here.

    Pinned with a script that colours its own stderr rather than relying on the
    interpreter to do it, so it asserts the same thing on every version.
    """
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))
    coloured = "\x1b[1;35mRuntimeError\x1b[0m: \x1b[35mrefused to stop\x1b[0m\n"
    script = tmp_path / "coloured_stop.py"
    script.write_text(
        "import sys\n"
        f"sys.stderr.write({coloured!r})\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(supertool, "_MCP_STOP_SCRIPT", str(script))

    outcome = supertool._mcp_stop_server("php-lsp")

    assert outcome.ok is False
    assert outcome.code == "failed"
    assert "\x1b" not in outcome.detail, outcome.detail
    assert outcome.detail == "RuntimeError: refused to stop"


@posix_only
def test_the_detail_cap_is_spent_on_text_not_escapes(tmp_path, monkeypatch) -> None:
    """The 500-character budget must buy 500 characters a reader can see (#1333).

    Arithmetic, with 120 lines of `Lnnn`: stripped they are 5 bytes each, so
    about 100 of them fit the cap. Coloured they are 14 bytes each and about 35
    fit — the cap keeps the tail, so two thirds of the context is evicted by
    bytes that render as nothing at all.
    """
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))
    script = tmp_path / "noisy_stop.py"
    script.write_text(
        "import sys\n"
        "for i in range(120):\n"
        "    sys.stderr.write('\\x1b[31mL%03d\\x1b[0m\\n' % i)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(supertool, "_MCP_STOP_SCRIPT", str(script))

    outcome = supertool._mcp_stop_server("php-lsp")

    assert "\x1b" not in outcome.detail, outcome.detail
    assert len(outcome.detail) <= supertool._MCP_STOP_DETAIL_CAP
    assert outcome.detail.count("\n") + 1 >= 90, outcome.detail


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
