"""`mcp_status` must not report `dead` for a pidfile it could not read (#549).

`status.py` caught `(OSError, ValueError)` around the pidfile read and set
`pid = 0`. Zero is falsy, so the liveness probe was skipped and the row printed
**dead** — a specific, confident claim about a daemon nothing had asked about.

Why this surface and not the general run of the pattern: `status` is the op a
human runs *specifically to find out whether a daemon is alive*. A live daemon
holding a stale index that reads as `dead` is not restarted, and goes on
answering from a reflection captured before the file changed — #239, arrived at
through the tool built to prevent it. It is also the first place anyone looks
to confirm #547's `FAILED to stop`, so the two surfaces have to agree:
`stop.py` already calls an unreadable pidfile a failure rather than a success.

The other half of the fix is the half that is easy to lose: a genuinely dead
daemon must still read `dead`, loudly. Trading the wrong answer for a blanket
`unknown` would swap a loud bug for a quiet one, so the dead and alive paths
are asserted here too, and they are asserted on the *same* fixture that the
unknown cases use.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets" / "mcp"))
sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))

# status.py reaches _paths.runtime_dir(), which refuses outright where
# os.geteuid does not exist (#544).
posix_only = pytest.mark.skipif(
    not hasattr(os, "geteuid"),
    reason="status.py's runtime dir is ownership-checked; os.geteuid is required.",
)

pytestmark = posix_only


@pytest.fixture
def status_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))
    import status  # noqa: PLC0415

    return status


def _pidfile(status_mod, name: str, body: str) -> Path:
    """Plant a pidfile the enumerator will find, with a chosen body."""
    from _paths import runtime_dir  # noqa: PLC0415

    p = Path(runtime_dir()) / f"supertool-mcp-{name}.pid"
    p.write_text(body, encoding="utf-8")
    return p


def _row(out: str) -> str:
    """The single daemon row, i.e. the line after the header."""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) >= 2, f"expected a header and a row, got: {out!r}"
    return lines[1]


class TestUnreadablePidfileIsNotDead:
    """The bug: an absence produced by the tool, reported as an absence in the world."""

    def test_garbage_pidfile_reads_unknown_not_dead(self, status_mod, capsys) -> None:
        """A pidfile that does not parse says nothing about the process."""
        _pidfile(status_mod, "aaaaaaaaaaaa", "not-a-pid\n")

        status_mod.main()
        row = _row(capsys.readouterr().out)

        assert "unknown" in row, f"unparsable pidfile must read unknown: {row!r}"
        assert "dead" not in row, f"must not claim the daemon is dead: {row!r}"

    def test_truncated_pidfile_reads_unknown(self, status_mod, capsys) -> None:
        """A daemon caught mid-write leaves an empty pidfile, not a dead process."""
        _pidfile(status_mod, "bbbbbbbbbbbb", "")

        status_mod.main()
        row = _row(capsys.readouterr().out)

        assert "unknown" in row
        assert "dead" not in row

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root can read a 0o000 file, so permission denied cannot be staged.",
    )
    def test_permission_denied_pidfile_reads_unknown(self, status_mod, capsys) -> None:
        """The OSError arm, distinct from the ValueError arm — both are unknowable."""
        p = _pidfile(status_mod, "cccccccccccc", "4242\n")
        os.chmod(p, 0o000)
        try:
            status_mod.main()
        finally:
            os.chmod(p, 0o600)
        row = _row(capsys.readouterr().out)

        assert "unknown" in row
        assert "dead" not in row

    def test_pid_zero_is_unknown_not_dead(self, status_mod, capsys) -> None:
        """`0` parses as an int and is not a process — it is garbage, not a verdict."""
        _pidfile(status_mod, "dddddddddddd", "0\n")

        status_mod.main()
        row = _row(capsys.readouterr().out)

        assert "unknown" in row, f"pid 0 is not a daemon that died: {row!r}"
        assert "dead" not in row

    def test_unknown_row_states_why(self, status_mod, capsys) -> None:
        """`unknown` with no reason trades a wrong answer for an unhelpful one.

        The reason has to name the pidfile as the thing that failed, so the
        reader's next action is to look at the file rather than at the daemon.
        """
        _pidfile(status_mod, "eeeeeeeeeeee", "not-a-pid\n")

        status_mod.main()
        out = capsys.readouterr().out

        assert "pidfile" in out.lower(), f"no reason given for unknown: {out!r}"

    def test_unknown_row_does_not_print_a_fabricated_pid(
        self, status_mod, capsys
    ) -> None:
        """Printing `0` in the PID column invents the one number we do not have."""
        _pidfile(status_mod, "ffffffffffff", "not-a-pid\n")

        status_mod.main()
        row = _row(capsys.readouterr().out)

        assert " 0 " not in row, f"fabricated pid 0 in row: {row!r}"


class TestRealVerdictsSurvive:
    """Do not trade the loud bug for the quiet one."""

    def test_stale_pidfile_of_a_gone_process_still_reads_dead(
        self, status_mod, monkeypatch, capsys
    ) -> None:
        """The common, valuable signal: readable pid, no such process."""
        _pidfile(status_mod, "111111111111", "4242\n")
        monkeypatch.setattr(status_mod._proc, "pid_alive", lambda pid: False)

        status_mod.main()
        row = _row(capsys.readouterr().out)

        assert "dead" in row, f"a genuinely dead daemon must stay loud: {row!r}"
        assert "unknown" not in row
        assert "4242" in row

    def test_running_daemon_still_reads_alive(
        self, status_mod, monkeypatch, capsys
    ) -> None:
        _pidfile(status_mod, "222222222222", "4242\n")
        monkeypatch.setattr(status_mod._proc, "pid_alive", lambda pid: True)

        status_mod.main()
        row = _row(capsys.readouterr().out)

        assert "alive" in row
        assert "unknown" not in row

    def test_an_unknown_row_does_not_hide_its_neighbours(
        self, status_mod, monkeypatch, capsys
    ) -> None:
        """One unreadable pidfile must not cost the reader the other verdicts."""
        _pidfile(status_mod, "111111111111", "4242\n")
        _pidfile(status_mod, "aaaaaaaaaaaa", "not-a-pid\n")
        monkeypatch.setattr(status_mod._proc, "pid_alive", lambda pid: True)

        status_mod.main()
        out = capsys.readouterr().out

        assert "alive" in out
        assert "unknown" in out

    def test_empty_runtime_dir_is_unchanged(self, status_mod, capsys) -> None:
        """No pidfiles at all is a real answer and must not become `unknown`."""
        status_mod.main()

        assert "No supertool MCP daemons running." in capsys.readouterr().out
