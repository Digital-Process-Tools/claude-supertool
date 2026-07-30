"""`list_pidfiles` must not report "could not look" as "nothing is there" (#551).

`presets/mcp/_paths.py`'s enumerator caught `OSError` from `os.listdir` and
returned `[]`. An empty list has exactly one meaning to both of its callers —
*there are no daemons* — so a runtime dir we failed to read was rendered as a
runtime dir with nothing in it. The house defect: an absence produced by the
tool, read as an absence in the world.

The two surfaces, and why the second one is the sharp one:

- `mcp_status` printed `No supertool MCP daemons running.` — a confident answer
  to a question it never managed to ask. Same defect #549 fixed one row down,
  now at the level of the whole table.
- `mcp_stop_all` printed `No daemons running.` and exited `EXIT_OK`, which
  #547's contract documents as *"nothing stale can come from nothing"*. That
  sentence is only true when we looked. A warm daemon holding an index from
  before the edit would still be there, and the op that exists to retire it
  reported that it had.

Reachability is low and stated as such: `runtime_dir()` runs first and chmods
the directory back to `0700`, so a `chmod 000` heals rather than raising. What
remains is an `ENOENT` race against the `mkdir`, `EIO` on a failing volume, or
`EMFILE` under fd pressure. So the honest reproduction is a forced `OSError`,
not a staged filesystem — which is what these tests do.

The other half, asserted here too because it is the half that is easy to lose:
a genuinely empty runtime dir must *still* read as empty, and `--all` must
still exit `0` for it. #547 kept that code deliberately so the `mcp_stop_all`
op does not read as FAIL on an ordinary day. Trading the wrong answer for a
blanket refusal would swap a silent bug for a noisy one.
"""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

import supertool

sys.path.insert(0, str(Path(__file__).parent.parent / "presets" / "mcp"))
sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))

# Both scripts reach _paths.runtime_dir(), which refuses outright where
# os.geteuid does not exist (#544).
posix_only = pytest.mark.skipif(
    not hasattr(os, "geteuid"),
    reason="the runtime dir is ownership-checked; os.geteuid is required.",
)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """An empty, owned runtime dir — the state where `[]` is the truth."""
    base = tmp_path / "rt"
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(base))
    return base


@pytest.fixture
def blind(monkeypatch, runtime):
    """Make `os.listdir` fail for the runtime dir only, and nothing else.

    Patching `os.listdir` wholesale would break unrelated machinery inside the
    same call; the failure has to be as narrow as the real one would be.

    Since #583 the enumerator is handed the validated directory as a descriptor
    rather than as a path, so the runtime dir is matched by `(st_dev, st_ino)`
    as well as by name. Matching on the name alone would quietly stop matching
    anything, and these tests would pass because the listdir succeeded — the
    opposite of what they assert.
    """
    real = os.listdir

    def _is_runtime(target) -> bool:
        try:
            st, want = os.stat(target), os.stat(runtime)
        except OSError:
            return False
        return (st.st_dev, st.st_ino) == (want.st_dev, want.st_ino)

    def _fail(path=".", *a, **kw):
        if str(path) in (str(runtime), str(runtime.resolve())) or _is_runtime(path):
            raise OSError(errno.EIO, "Input/output error")
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "listdir", _fail)
    return runtime


def _plant(runtime, name: str, body: str = "4242\n") -> Path:
    runtime.mkdir(parents=True, exist_ok=True)
    p = runtime / f"supertool-mcp-{name}.pid"
    p.write_text(body, encoding="utf-8")
    return p


@posix_only
class TestEnumeratorStatesWhenItCouldNotLook:
    """`list_pidfiles` returns `(pidfiles, reason)`, mirroring #549's read_pid."""

    def test_reason_is_empty_when_the_listing_succeeded(self, runtime):
        import _paths  # noqa: PLC0415

        _plant(runtime, "aaaaaaaaaaaa")
        pidfiles, reason = _paths.list_pidfiles()

        assert reason == "", f"a successful listing must state no reason: {reason!r}"
        assert [os.path.basename(p) for p in pidfiles] == [
            "supertool-mcp-aaaaaaaaaaaa.pid"
        ]

    def test_an_unlistable_dir_returns_a_reason(self, blind):
        import _paths  # noqa: PLC0415

        pidfiles, reason = _paths.list_pidfiles()

        assert reason, "an OSError from listdir must not be rendered as no reason"
        assert pidfiles == [], "no paths were enumerated, so none may be claimed"

    def test_the_reason_names_the_errno_text(self, blind):
        """A stated refusal with no cause is a worse answer, not a better one."""
        import _paths  # noqa: PLC0415

        _pidfiles, reason = _paths.list_pidfiles()

        assert "Input/output error" in reason, f"cause lost: {reason!r}"


@posix_only
class TestStopAllDoesNotSucceedAtNothing:
    """The contract change: `--all` cannot exit `0` for a dir it never read."""

    @pytest.fixture
    def stop_mod(self, runtime):
        import stop  # noqa: PLC0415

        return stop

    def test_unlistable_dir_is_not_exit_ok(self, stop_mod, blind, capsys):
        rc = stop_mod.main(["stop.py", "--all"])

        assert rc != stop_mod.EXIT_OK, (
            "EXIT_OK means 'nothing stale can come from nothing' — "
            "but nothing was looked at"
        )
        assert rc == stop_mod.EXIT_REFUSED, f"expected EXIT_REFUSED, got {rc}"

    def test_unlistable_dir_does_not_claim_nothing_was_running(
        self, stop_mod, blind, capsys
    ):
        stop_mod.main(["stop.py", "--all"])
        captured = capsys.readouterr()

        assert "No daemons running." not in captured.out, (
            f"claimed an absence it did not establish: {captured.out!r}"
        )

    def test_the_refusal_states_its_reason_on_stderr(self, stop_mod, blind, capsys):
        """The exit status carries the outcome (#547); the reason carries the why."""
        stop_mod.main(["stop.py", "--all"])
        err = capsys.readouterr().err

        assert err.strip(), "a refusal with no message is not a refusal"
        assert "Input/output error" in err, f"cause lost: {err!r}"

    def test_a_genuinely_empty_dir_still_exits_ok(self, stop_mod, runtime, capsys):
        """#547 kept this at `0` on purpose — `mcp_stop_all` must not read FAIL."""
        rc = stop_mod.main(["stop.py", "--all"])
        out = capsys.readouterr().out

        assert rc == stop_mod.EXIT_OK, f"back-compat broken: {rc}"
        assert "No daemons running." in out


@posix_only
class TestStatusDoesNotClaimAnEmptyTable:
    """An enumeration failure is the whole table missing, not a row's verdict."""

    @pytest.fixture
    def status_mod(self, runtime):
        import status  # noqa: PLC0415

        return status

    def test_unlistable_dir_does_not_print_the_empty_report(
        self, status_mod, blind, capsys
    ):
        status_mod.main()
        out = capsys.readouterr().out

        assert "No supertool MCP daemons running." not in out, (
            f"claimed an absence it did not establish: {out!r}"
        )

    def test_the_message_reaches_stdout(self, status_mod, blind, capsys):
        """`mcp_status` exits 0, so the custom-op runner only surfaces stdout.

        A message written to stderr on a zero exit is discarded by
        `_run_custom_op` — the same silence, in a new coat.
        """
        status_mod.main()
        out = capsys.readouterr().out

        assert "Input/output error" in out, f"reason not on stdout: {out!r}"

    def test_status_still_exits_zero(self, status_mod, blind, capsys):
        """It is a human report, not a check (#549) — that does not change."""
        assert status_mod.main() == 0

    def test_a_genuinely_empty_dir_still_reports_no_daemons(
        self, status_mod, runtime, capsys
    ):
        status_mod.main()
        out = capsys.readouterr().out

        assert "No supertool MCP daemons running." in out

    def test_a_readable_dir_still_renders_its_rows(
        self, status_mod, runtime, monkeypatch, capsys
    ):
        """Do not trade the wrong answer for a blanket decline."""
        _plant(runtime, "111111111111", "4242\n")
        monkeypatch.setattr(status_mod._proc, "pid_alive", lambda pid: True)

        status_mod.main()
        out = capsys.readouterr().out

        assert "alive" in out
        assert "4242" in out


class TestRefusedStaysUnsuccessfulToTheCaller:
    """The load-bearing mapping: `ok` decides whether staleness was prevented."""

    def test_exit_refused_maps_to_not_ok(self):
        label, ok = supertool._MCP_STOP_CODES[4]

        assert ok is False, (
            "EXIT_REFUSED mapping to ok=True would move the bug, not fix it"
        )
        assert label == "refused"

    def test_no_daemon_is_still_a_success(self):
        """The distinction the fix rests on: 'none' is fine, 'unknown' is not.

        Keyed on `stop.EXIT_NO_DAEMON` rather than on a literal, because the
        number moved once already (#574 vacated `1`, which is what CPython
        exits with on a traceback) and this assertion is about the meaning.
        """
        import stop  # noqa: PLC0415

        label, ok = supertool._MCP_STOP_CODES[stop.EXIT_NO_DAEMON]

        assert ok is True
        assert label == "no-daemon"
