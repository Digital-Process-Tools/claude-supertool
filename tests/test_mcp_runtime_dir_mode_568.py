"""The runtime dir must *be* owner-only, not merely have been asked to be (#568).

`presets/mcp/_paths.py`'s `runtime_dir()` created the daemon's runtime directory
at the umask default and tightened it afterwards:

    base.mkdir(parents=True, exist_ok=True)
    # Tighten perms — directory must be owner-only.
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass

Two defects, and the comment states the requirement that neither of them meets.

**Created at the wrong mode.** `Path.mkdir` with no `mode=` uses
`0o777 & ~umask` — `0o755` under the common `umask 022`. On every first run there
is a window between `mkdir` and `chmod` in which the runtime dir is group- and
world-readable and traversable.

**The tightening is allowed to fail in silence.** `except OSError: pass` was the
only handling, and the function then continued whether or not the dir ended up
owner-only. The ownership check immediately below catches a dir owned by another
uid; it never looked at the mode, so a dir we own at `0o755` passed every gate
and was returned as trusted.

That is not cosmetic, because this is the directory #148 exists to create. From
the module docstring: *"on Linux, parent-dir perms (1777) gate connect, so the
chmod barely matters"* — the argument for why the parent directory's mode is the
real access control, and this is the parent directory. Left at `0o755` it
restores co-tenant `connect()` to the daemon socket and co-tenant enumeration of
the pidfiles, which is the vector #148 closed.

**Why this refuses rather than warning.** `os.stat` answers the question, so this
is a finding, not the absence of one — `skipped` is for a question that cannot be
asked (`st_uid` on Windows, #544), not for an answer we dislike. Warning and
proceeding is the shape #544 names: a security check that never stops anything is
indistinguishable from one that keeps passing. The precedents both refuse —
the ownership check next door, and `_publish_safety.check_token_file_mode`, which
mirrors `ssh` declining an insecure key.

The tests are written so that a `chmod` which *cannot work* is the ordinary case
rather than a fault: a filesystem with no POSIX modes (exFAT/FAT32/SMB) is the
reachable cause, and it is simulated by making `os.chmod` fail for this path
only. Under that condition a *newly created* dir must still come out `0o700`
(which is only true if `mkdir` asked for it) and must NOT be refused, while a
pre-existing loose dir must be. That pair is what a half-implementation fails:
fixing only the `mkdir` mode leaves the existing-dir case exposed, and fixing
only the verification makes the no-chmod filesystem unusable for a dir supertool
created itself.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

import pytest

import supertool  # noqa: F401  (ensures the repo root is importable, as siblings do)

sys.path.insert(0, str(Path(__file__).parent.parent / "presets" / "mcp"))
sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))

_REAL_CHMOD = os.chmod

posix_only = pytest.mark.skipif(
    not hasattr(os, "geteuid"),
    reason="the runtime dir is ownership-checked; os.geteuid is required.",
)


@pytest.fixture
def loose_umask():
    """Pin `umask 022` for the test.

    Without this the assertions read the developer's own umask: on a machine
    set to `077` the unfixed `mkdir` already produces `0o700` and every test
    here goes green against the bug.
    """
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture
def runtime(tmp_path, monkeypatch, loose_umask):
    """A runtime dir path under `SUPERTOOL_RUNTIME_DIR`, not yet created."""
    base = tmp_path / "rt"
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(base))
    return base


@pytest.fixture
def no_chmod(monkeypatch, runtime):
    """Make `os.chmod` fail for the runtime dir only — the exFAT/SMB case.

    Narrow on purpose: patching `os.chmod` wholesale would break unrelated
    machinery reached from the same call, and the real failure is per-path.
    """
    real = os.chmod

    def _fail(path, mode, *a, **kw):
        if str(path) in (str(runtime), str(runtime.resolve())):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real(path, mode, *a, **kw)

    monkeypatch.setattr(os, "chmod", _fail)
    return runtime


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


@posix_only
class TestTheDirIsCreatedOwnerOnly:
    """No window between `mkdir` and the tightening — the mode is asked for."""

    def test_a_fresh_dir_is_0700_even_when_chmod_cannot_work(self, no_chmod):
        """The mode must come from `mkdir`, not from a follow-up that may fail."""
        import _paths  # noqa: PLC0415

        got = _paths.runtime_dir()

        assert Path(got) == no_chmod
        assert _mode(no_chmod) == 0o700, (
            f"created at {oct(_mode(no_chmod))} — with chmod unavailable the "
            f"mode can only have come from mkdir(mode=0o700)"
        )

    def test_a_fresh_dir_on_a_modeless_filesystem_is_not_refused(self, no_chmod):
        """The regression to avoid: refusing a dir that is already correct.

        This is the exFAT / SMB / NFS-noacl `SUPERTOOL_RUNTIME_DIR` the issue
        worries about. A dir supertool creates itself lands owner-only without
        the chmod, so the failing chmod must not be an error by itself.
        """
        import _paths  # noqa: PLC0415

        assert _paths.runtime_dir() == str(no_chmod)

    def test_a_fresh_dir_is_0700_on_the_ordinary_path_too(self, runtime):
        import _paths  # noqa: PLC0415

        _paths.runtime_dir()

        assert _mode(runtime) == 0o700, oct(_mode(runtime))


@posix_only
class TestALooseExistingDirIsTightenedOrRefused:
    """`mode=` is ignored for a dir that already exists, so the chmod has a job."""

    def test_an_existing_loose_dir_is_tightened_and_accepted(self, runtime):
        """The heal path must keep working — do not refuse what we can fix."""
        import _paths  # noqa: PLC0415

        runtime.mkdir(parents=True)
        os.chmod(runtime, 0o755)

        got = _paths.runtime_dir()

        assert got == str(runtime)
        assert _mode(runtime) == 0o700, oct(_mode(runtime))

    def test_a_loose_dir_that_cannot_be_tightened_is_refused(self, no_chmod):
        """The bug: this used to be returned as a trusted directory."""
        import _paths  # noqa: PLC0415

        no_chmod.mkdir(parents=True)
        _force_mode(no_chmod, 0o755)

        with pytest.raises(SystemExit) as exc:
            _paths.runtime_dir()

        assert isinstance(exc.value.code, str), (
            f"a refusal must carry a sentence, not {exc.value.code!r} — "
            f"stop.py only relabels a stated reason as EXIT_REFUSED"
        )

    def test_the_refusal_names_the_mode_and_the_way_out(self, no_chmod):
        import _paths  # noqa: PLC0415

        no_chmod.mkdir(parents=True)
        _force_mode(no_chmod, 0o755)

        with pytest.raises(SystemExit) as exc:
            _paths.runtime_dir()
        reason = str(exc.value.code)

        assert "755" in reason, f"mode not named: {reason!r}"
        assert str(no_chmod) in reason, f"path not named: {reason!r}"
        assert "SUPERTOOL_RUNTIME_DIR" in reason, f"no way out offered: {reason!r}"

    @pytest.mark.parametrize("mode", [0o750, 0o705, 0o701, 0o770, 0o777])
    def test_any_group_or_other_bit_is_refused(self, no_chmod, mode):
        """`0o077`, not `!= 0o700` — a group-read-only dir is still not ours."""
        import _paths  # noqa: PLC0415

        no_chmod.mkdir(parents=True)
        _force_mode(no_chmod, mode)

        with pytest.raises(SystemExit):
            _paths.runtime_dir()

    @pytest.mark.parametrize("mode", [0o700, 0o600, 0o500])
    def test_an_owner_only_dir_is_accepted_unchanged(self, no_chmod, mode):
        """Do not invent a finding: no group/other bits means nothing to say.

        `0o600` and `0o500` are owner-only too. They are not what supertool
        asks for and the daemon would fail on them for its own reasons, but
        this check's question is exposure to other users, and the answer is no.
        """
        import _paths  # noqa: PLC0415

        no_chmod.mkdir(parents=True)
        _force_mode(no_chmod, mode)

        assert _paths.runtime_dir() == str(no_chmod)


@posix_only
class TestAnUnusableRuntimeDirGetsASentence:
    """`mkdir` was unguarded — the same #544 shape, one line up."""

    def test_a_path_that_is_a_regular_file_is_refused_not_crashed(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """`exist_ok=True` does not tolerate a non-directory: FileExistsError."""
        import _paths  # noqa: PLC0415

        target = tmp_path / "not-a-dir"
        target.write_text("", encoding="utf-8")
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(target))

        with pytest.raises(SystemExit) as exc:
            _paths.runtime_dir()

        assert isinstance(exc.value.code, str), (
            f"a traceback out of a library helper is not a refusal: "
            f"{exc.value.code!r}"
        )
        assert str(target) in str(exc.value.code)

    def test_an_uncreatable_path_is_refused_not_crashed(
        self, tmp_path, monkeypatch, loose_umask
    ):
        import _paths  # noqa: PLC0415

        parent = tmp_path / "ro"
        parent.mkdir()
        os.chmod(parent, 0o500)
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(parent / "rt"))
        try:
            with pytest.raises(SystemExit) as exc:
                _paths.runtime_dir()
            assert isinstance(exc.value.code, str), repr(exc.value.code)
        finally:
            os.chmod(parent, 0o700)


@posix_only
class TestTheRefusalReachesTheSurfaces:
    """A reason nothing renders is the silence this replaces, in a new coat."""

    def test_stop_all_reports_exit_refused(self, no_chmod, capsys):
        import stop  # noqa: PLC0415

        no_chmod.mkdir(parents=True)
        _force_mode(no_chmod, 0o755)

        rc = stop.main(["stop.py", "--all"])
        err = capsys.readouterr().err

        assert rc == stop.EXIT_REFUSED, f"expected EXIT_REFUSED, got {rc}"
        assert "755" in err, f"reason not surfaced: {err!r}"

    def test_stop_all_does_not_claim_nothing_was_running(self, no_chmod, capsys):
        import stop  # noqa: PLC0415

        no_chmod.mkdir(parents=True)
        _force_mode(no_chmod, 0o755)

        stop.main(["stop.py", "--all"])

        assert "No daemons running." not in capsys.readouterr().out


def _force_mode(path: Path, mode: int) -> None:
    """`os.chmod` the real way, bypassing the `no_chmod` fixture's patch.

    The fixture patches the module attribute; the C function is still reachable
    through `os.__dict__`'s original, which we captured at import time.
    """
    _REAL_CHMOD(path, mode)
