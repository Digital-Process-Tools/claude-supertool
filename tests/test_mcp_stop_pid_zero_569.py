"""#569: `stop.py` would signal its own process group instead of a daemon.

`stop_by_pidfile` parsed the pidfile with a bare `int(...)`, and `int("0")`
raises nothing — so the `except (OSError, ValueError)` arm never fired.
`stop_pid` had no guard either, so the value went straight to
`os.kill(pid, signal.SIGTERM)`, where every non-positive value is not a
process id at all but a *broadcast selector*:

    pid  > 0    one process — the only thing a pidfile can mean
    pid == 0    every process in the caller's own process group
    pid == -1   every process the caller is permitted to signal
    pid  < -1   every process in process group `-pid`

A pidfile holding `0` is reachable without an attacker: a truncated write, a
zeroed file, a daemon that wrote before it knew its pid, an `EIO` that left
zeros behind. That turned `mcp_stop` into a SIGTERM of everything sharing the
caller's process group, which in a Claude Code session plausibly includes the
session itself.

Two further details, both asserted below, because both are worse than the
one-line reading suggests:

- The SIGKILL escalation is *unreachable* for these values — `_proc.pid_alive`
  has rejected `pid <= 0` since #429, so the wait loop exits on its first
  iteration. One SIGTERM goes out, and then
- `stop_pid` returns `True`. The tool reports `stopped pid=0`, exits `EXIT_OK`,
  and unlinks the pidfile. If the caller survives its own SIGTERM (it is in the
  group it just signalled) it is told the daemon was stopped, and the only
  evidence of the corruption is deleted.

`status.py` has rejected `pid <= 0` since #549 — "not a process id". The
surface that only *reads* was guarded; the surface that *sends signals* was
not. #263's lesson: the abstraction already existed and the call site had not
adopted it.

Every test here patches `os.kill` and asserts on what it was *called with*. A
test that genuinely signalled its own process group would take the runner down
with it — which is also the reason this needs a test rather than a manual
check.
"""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets" / "mcp"))
sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))

# stop.py reaches _paths.runtime_dir(), which refuses outright where
# os.geteuid does not exist (#544).
pytestmark = pytest.mark.skipif(
    not hasattr(os, "geteuid"),
    reason="stop.py's runtime dir is ownership-checked; os.geteuid is required.",
)


@pytest.fixture
def stop_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))
    import stop  # noqa: PLC0415

    return stop


@pytest.fixture
def kills(monkeypatch):
    """Record every `os.kill` and deliver none of them.

    Returning `None` is what a *successful* signal looks like, so the recorded
    call list is the whole observable: the guard has to stop the call being
    made, not rely on it failing.
    """
    calls = []

    def _record(pid, sig):
        calls.append((pid, sig))

    monkeypatch.setattr(os, "kill", _record)
    return calls


def _pidfile(stop_mod, name: str, body: str) -> Path:
    _sock, pid_path = stop_mod.socket_pid_paths(os.path.abspath(os.getcwd()), name)
    p = Path(pid_path)
    p.write_text(body, encoding="utf-8")
    return p


NON_PIDS = [
    ("0", "the caller's own process group"),
    ("-1", "every process the user may signal"),
    ("-4242", "process group 4242"),
    ("  0\n", "zero with the whitespace a real write leaves"),
    ("+0", "zero that int() accepts with a sign"),
]


class TestValuesThatAreNotProcessIds:
    """The hazard: `os.kill` reads these as selectors, not identities."""

    @pytest.mark.parametrize("body,meaning", NON_PIDS, ids=[b.strip() for b, _ in NON_PIDS])
    def test_no_signal_is_sent(self, stop_mod, kills, body, meaning) -> None:
        """Nothing may be signalled — `meaning` is what would have been hit."""
        _pidfile(stop_mod, "zeroed", body)

        stop_mod.main(["stop.py", "zeroed"])

        assert kills == [], f"signalled {meaning}: os.kill{kills}"

    @pytest.mark.parametrize("body,_meaning", NON_PIDS, ids=[b.strip() for b, _ in NON_PIDS])
    def test_exit_status_is_a_failure(self, stop_mod, kills, body, _meaning) -> None:
        """Nothing was stopped, so this is not `EXIT_OK`.

        `EXIT_STOP_FAILED` (3) rather than a new code: `docs/mcp-integration.md`
        already documents 3 as covering "a daemon was found and is still there,
        **or its pidfile was unreadable**", and both map to `ok=False` for the
        automatic caller. Nothing downstream would act on a finer distinction.
        """
        _pidfile(stop_mod, "zeroed", body)

        rc = stop_mod.main(["stop.py", "zeroed"])

        assert rc == stop_mod.EXIT_STOP_FAILED
        assert rc != stop_mod.EXIT_OK, "reported a stop that never happened"
        assert rc != stop_mod.EXIT_NO_DAEMON, "a corrupt pidfile is not an absent one"

    def test_the_reason_names_the_value(self, stop_mod, kills, capsys) -> None:
        """`invalid pidfile` covered four causes; the reader needs the one."""
        _pidfile(stop_mod, "zeroed", "0\n")

        stop_mod.main(["stop.py", "zeroed"])
        err = capsys.readouterr().err

        assert "0" in err
        assert "not a process id" in err, f"reason not named: {err!r}"

    def test_the_corrupt_pidfile_is_kept(self, stop_mod, kills) -> None:
        """Deliberate: we do not delete the only evidence of the bug that wrote it.

        Unlinking would also manufacture the benign reading this refuses to
        give — the next `mcp_stop` would report `EXIT_NO_DAEMON`, i.e. `ok`, for
        a daemon whose fate is still unknown. And `mcp_status` needs the file to
        render the `unknown` row the docs send the reader to.
        """
        p = _pidfile(stop_mod, "zeroed", "0\n")

        stop_mod.main(["stop.py", "zeroed"])

        assert p.exists(), "corrupt pidfile deleted — the evidence is gone"
        assert p.read_text(encoding="utf-8") == "0\n", "contents rewritten"

    def test_stop_pid_refuses_on_its_own(self, stop_mod, kills) -> None:
        """The guard belongs at the signal boundary, not only at the parse.

        `stop_pid` is module-level and reachable from any caller; a guard that
        lived only in `stop_by_pidfile` would protect the one path that happens
        to exist today.
        """
        assert stop_mod.stop_pid(0) is False
        assert stop_mod.stop_pid(-1) is False
        assert kills == [], f"stop_pid signalled directly: os.kill{kills}"

    def test_all_mode_is_guarded_too(self, stop_mod, kills, capsys) -> None:
        """`--all` walks pidfiles without going through `socket_pid_paths`."""
        _pidfile(stop_mod, "zeroed", "0\n")

        rc = stop_mod.main(["stop.py", "--all"])

        assert kills == [], f"--all signalled a process group: os.kill{kills}"
        assert rc == stop_mod.EXIT_STOP_FAILED


class TestRealStopsSurvive:
    """Do not trade the loud bug for a tool that stops nothing."""

    def test_a_real_pid_is_still_signalled(self, stop_mod, kills, monkeypatch) -> None:
        _pidfile(stop_mod, "live", "4242\n")
        monkeypatch.setattr(stop_mod._proc, "pid_alive", lambda pid: False)

        rc = stop_mod.main(["stop.py", "live"])

        assert kills == [(4242, signal.SIGTERM)]
        assert rc == stop_mod.EXIT_OK

    def test_no_pidfile_at_all_is_still_benign(self, stop_mod, kills) -> None:
        assert stop_mod.main(["stop.py", "never-started"]) == stop_mod.EXIT_NO_DAEMON


class TestTheTwoSurfacesShareOneReader:
    """#569's fix shape: one helper, so the two surfaces cannot drift again."""

    def test_stop_and_status_read_pidfiles_with_the_same_function(self, stop_mod) -> None:
        import _paths  # noqa: PLC0415
        import status  # noqa: PLC0415

        assert stop_mod.read_pid is _paths.read_pid
        assert status.read_pid is _paths.read_pid

    @pytest.mark.parametrize(
        "body,expected",
        [
            ("", "empty pidfile"),
            ("not-a-pid\n", "unparsable pidfile"),
            ("0\n", "not a process id"),
        ],
    )
    def test_each_cause_is_named_distinctly(
        self, stop_mod, kills, capsys, body, expected
    ) -> None:
        """Four causes, four fixes. `invalid pidfile` served none of them."""
        _pidfile(stop_mod, "garbled", body)

        stop_mod.main(["stop.py", "garbled"])

        assert expected in capsys.readouterr().err

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root can read a 0o000 file, so permission denied cannot be staged.",
    )
    def test_permission_denied_is_named_too(self, stop_mod, kills, capsys) -> None:
        p = _pidfile(stop_mod, "locked", "4242\n")
        os.chmod(p, 0o000)
        try:
            stop_mod.main(["stop.py", "locked"])
        finally:
            os.chmod(p, 0o600)

        assert "unreadable pidfile" in capsys.readouterr().err
