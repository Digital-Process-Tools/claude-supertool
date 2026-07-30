"""The daemon must write into the directory that was validated (#598).

#583 / PR #597 closed the *validation* window: `_paths._open_runtime_dir()`
resolves the configured runtime dir once, holds it as an
`O_DIRECTORY | O_NOFOLLOW` descriptor for the whole of `fchmod`/`fstat`, and
hands back a symlink-free path. Nothing inside `_paths.py` can be asked about
one directory and answer about another.

It stops at the daemon, and `docs/mcp-integration.md` says so rather than
claiming otherwise. `daemon.py` is a separate process handed **strings**, and
every one of its own opens re-resolves them:

    server.bind(sock_path)                     # socket.bind takes no dir_fd
    os.chmod(sock_path, 0o700)
    os.open(pid_path, O_CREAT | O_EXCL | ...)   # O_NOFOLLOW guards the *file*
    os.open(f"{sock_path}.stderr", ...)         # ...not the directory holding it
    os.open(f"{sock_path}.log", ...)
    os.unlink(sock_path); os.unlink(pid_path)

`O_NOFOLLOW` on those opens is about the leaf file. It says nothing about the
directory component, so replacing the *directory* between the validation and
the daemon's use of it redirects every one of them at once, and every check
#583 and #568 perform still passed — about a directory that is no longer there.

**These are swap tests, not symlink tests.** A symlinked runtime dir already
works correctly (#583 resolves it once, deliberately). What is untied is the
validated inode from the used inode, so the fixture below performs a real
rename swap at the exact moment validation completes and asserts the decoy is
never touched. Asked the way that matters — "would this pass if the fix did
nothing?" — the answer must be no: today every file lands in the decoy.

**Reachability, stated rather than assumed.** The runtime dir is `0700` and
ownership-checked, so an attacker needs write access to its *parent* to rename
it. That is not always a directory supertool owns: `_runtime_base()` accepts
`$XDG_RUNTIME_DIR` on trust, and an attacker who pre-creates
`$XDG_RUNTIME_DIR/supertool` mode `0777` owns the parent of the leaf we then
create, own and validate. Write plus execute on a non-sticky directory permits
`rename()` of any entry regardless of who owns it. See
`TestTheAncestryOfTheRuntimeDirIsUnchecked` — the hole both #583 and #598
describe as "needs write access to a directory supertool owns" is reachable
without that being true.
"""
from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import time
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

needs_dir_fd = pytest.mark.skipif(
    not (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")),
    reason="O_DIRECTORY/O_NOFOLLOW are POSIX-only; the fd path declines without them.",
)

needs_relative_ops = pytest.mark.skipif(
    not (os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd),
    reason="descriptor-relative opens are POSIX-only.",
)


def _ident(path) -> tuple:
    """(st_dev, st_ino) — the only honest way to say 'the same directory'."""
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


@pytest.fixture
def loose_umask():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture
def swapped(tmp_path, monkeypatch, loose_umask):
    """Replace the runtime dir with a decoy the instant validation finishes.

    The seam is `_paths._verify_runtime_dir`, which is the last thing
    `_open_runtime_dir` does before returning. Patching it to run the real
    verification and *then* swap puts the attacker in exactly the window this
    issue is about — every check has passed, nothing has been written yet — and
    it is a seam that exists in the code as it stands today, so the test is
    shaped by the defect rather than by the fix.

    The swap is a rename, not a symlink: `real` is moved aside to `real.moved`
    (still the validated inode, still open on any held descriptor) and `decoy`
    is renamed into its place. A daemon working from the *path* writes into
    `decoy`. A daemon working from a held descriptor writes into `real.moved`.
    """
    import _paths  # noqa: PLC0415

    real = tmp_path / "rt"
    decoy = tmp_path / "decoy"
    decoy.mkdir(mode=0o700)
    moved = tmp_path / "rt.moved"
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(real))

    state = {"swaps": 0}
    real_verify = _paths._verify_runtime_dir

    def verify_then_swap(fd, resolved, base, geteuid):
        real_verify(fd, resolved, base, geteuid)
        if state["swaps"] == 0 and os.path.isdir(real) and not moved.exists():
            state["swaps"] += 1
            os.rename(real, moved)
            os.rename(decoy, real)

    monkeypatch.setattr(_paths, "_verify_runtime_dir", verify_then_swap)

    class Setup:
        pass

    setup = Setup()
    setup.real, setup.decoy, setup.validated = real, real, moved
    setup.state = state
    return setup


def _names(d: Path) -> set:
    return {p.name for p in d.iterdir()} if d.is_dir() else set()


@posix_only
@needs_dir_fd
@needs_relative_ops
class TestTheDaemonWritesWhereItWasValidated:
    """Everything the daemon creates must land in the inode that passed the checks."""

    def test_the_pidfile_claim_lands_in_the_validated_directory(self, swapped):
        import daemon  # noqa: PLC0415

        fd, base = _open_and_claim(daemon, "cclsp")

        assert swapped.state["swaps"] == 1, "the fixture never got to swap"
        pids = {n for n in _names(swapped.validated) if n.endswith(".pid")}
        assert pids, (
            f"no pidfile in the validated directory — it went to "
            f"{sorted(_names(Path(swapped.decoy)))} instead, a directory nothing "
            f"inspected"
        )
        assert not any(n.endswith(".pid") for n in _names(Path(swapped.decoy))), (
            f"pidfile created in the swapped-in decoy: "
            f"{sorted(_names(Path(swapped.decoy)))}"
        )
        os.close(fd)

    def test_serve_creates_nothing_in_a_directory_swapped_in_after_validation(
        self, swapped
    ):
        """End-to-end: the socket, the logs and the fingerprint, all at once.

        `.stderr` and `.log` are the durable evidence — `_spawn.cleanup` unlinks
        the socket, pidfile and fingerprint on the way out but never touches the
        logs, so whatever directory they are in is where the daemon was really
        working.
        """
        import daemon  # noqa: PLC0415

        spec = {"cmd": [sys.executable, "-c", "raise SystemExit(0)"], "idle_timeout": 1}
        daemon.serve("cclsp", spec)

        assert swapped.state["swaps"] == 1, "the fixture never got to swap"
        assert _names(Path(swapped.decoy)) == set(), (
            f"the daemon wrote {sorted(_names(Path(swapped.decoy)))} into a "
            f"directory that was substituted after every check passed — the "
            f"socket a co-tenant would connect to is in there"
        )
        left = _names(swapped.validated)
        assert any(n.endswith(".stderr") for n in left), (
            f"validated dir holds {sorted(left)} — the daemon never worked here"
        )
        assert any(n.endswith(".log") for n in left), sorted(left)

    def test_the_socket_binds_inside_the_validated_directory(self, swapped):
        import daemon  # noqa: PLC0415

        seen = _serve_and_capture_socket(daemon)

        assert seen is not None, "no socket was ever bound"
        assert _ident(Path(seen).parent) == _ident(swapped.validated), (
            f"socket bound in {Path(seen).parent} — not the directory that was "
            f"checked for ownership and mode"
        )


@posix_only
@needs_dir_fd
@needs_relative_ops
class TestABindThatCannotBeShortenedByResolving:
    """A relative bind also buys back the sun_path budget #583 spent."""

    def test_a_runtime_dir_too_long_for_sun_path_still_binds(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """`sockaddr_un.sun_path` is 104 bytes on macOS, 108 on Linux.

        #583 made this reachable rather than hypothetical: it resolves the
        configured path, and resolving can only ever *lengthen* it, so a short
        symlink deliberately used to duck the cap no longer ducks it. Binding
        the basename from inside the directory spends none of the budget on the
        directory at all.
        """
        import daemon  # noqa: PLC0415

        deep = tmp_path / ("d" * 60) / ("e" * 60)
        deep.mkdir(parents=True)
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(deep / "rt"))
        assert len(str(deep / "rt")) > 108, len(str(deep / "rt"))

        seen = _serve_and_capture_socket(daemon)

        assert seen is not None, (
            "the daemon could not bind at all — an absolute bind spends the "
            "whole sun_path budget on the directory"
        )


@posix_only
@needs_dir_fd
class TestAFailedLogOpenDoesNotOrphanTheServer:
    """The #148 guard firing must not leave the heavy child running (found, not briefed).

    `_safe_open` is the `O_NOFOLLOW` defence against a squatted `.stderr`. It
    runs *after* `subprocess.Popen`, and its `OSError` is not caught anywhere:
    it unwinds out of `_serve_owned`, past `serve`'s `finally` (which only calls
    `_spawn.cleanup`), and out of `main`. `proc.terminate()` is in the `finally`
    of a `try` the exception never entered. So the guard whose whole purpose is
    to refuse an attack instead produces the stray MCP server child that
    `_spawn.py`'s docstring exists to prevent — four `phpstan-warm` daemons, the
    oldest thirteen hours old.

    This is the louder bug hiding inside the quiet one: not a silent fallback,
    a fatal `OSError` that leaks a process.
    """

    def test_a_squatted_stderr_symlink_does_not_leave_the_subprocess_running(
        self, tmp_path, monkeypatch, loose_umask
    ):
        import _paths  # noqa: PLC0415
        import daemon  # noqa: PLC0415

        rt = tmp_path / "rt"
        rt.mkdir(mode=0o700)
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(rt))
        cwd = os.path.abspath(os.getcwd())
        sock_path, _pid = _paths.socket_pid_paths(cwd, "cclsp")
        victim = tmp_path / "victim"
        victim.write_text("", encoding="utf-8")
        os.symlink(victim, f"{sock_path}.stderr")

        spawned = []
        real_popen = subprocess.Popen

        def recording_popen(*a, **kw):
            proc = real_popen(*a, **kw)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(daemon.subprocess, "Popen", recording_popen)
        spec = {
            "cmd": [sys.executable, "-c", "import time; time.sleep(30)"],
            "idle_timeout": 1,
        }

        with pytest.raises(OSError):
            daemon.serve("cclsp", spec)

        assert spawned, "nothing was spawned — the test is not exercising the window"
        proc = spawned[0]
        for _ in range(40):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        alive = proc.poll() is None
        if alive:
            proc.kill()
            proc.wait(timeout=5)
        assert not alive, (
            "the MCP server subprocess outlived the daemon that spawned it — a "
            "squatted .stderr turns the #148 symlink guard into the #451 stray "
            "child it was supposed to have no part in"
        )


@posix_only
@needs_dir_fd
@needs_relative_ops
class TestStopUnlinksInsideTheDirectoryItListed:
    """`stop.py` is the one surface that *writes*, so its unlink must be pinned.

    `list_pidfiles` already enumerates through a validated descriptor (#583) —
    and then closes it and returns joined strings, so every unlink re-resolved
    the directory. A swap between the listing and the removal has `stop.py`
    deleting a file of someone else's choosing, under the euid of whoever ran
    `mcp_stop`.
    """

    def test_the_pidfile_removed_is_the_pidfile_that_was_listed(self, swapped):
        import _paths  # noqa: PLC0415
        import stop  # noqa: PLC0415

        cwd = os.path.abspath(os.getcwd())
        _sock_name, pid_name = _paths.socket_pid_names(cwd, "cclsp")
        fd, _base = _paths.open_runtime_dir()
        try:
            # Both directories hold a pidfile with the same name. Only the
            # validated one may lose it.
            (swapped.validated / pid_name).write_text("999999", encoding="utf-8")
            (Path(swapped.decoy) / pid_name).write_text("999999", encoding="utf-8")

            ok, _msg = stop.stop_by_pidfile(pid_name, dir_fd=fd)
        finally:
            os.close(fd)

        assert ok, "the stop itself did not happen — the test proves nothing"
        assert not (swapped.validated / pid_name).exists(), (
            "the validated directory kept its pidfile — stop.py removed some "
            "other file"
        )
        assert (Path(swapped.decoy) / pid_name).exists(), (
            "stop.py deleted a file in a directory substituted after validation"
        )


@posix_only
@needs_dir_fd
class TestAPlatformThatCannotDoThisSaysSo:
    """`skipped` with a reason, never `ok` — the three-state contract (#544/#551).

    `dir_fd` is POSIX-only and `os.fchdir` does not exist on Windows, so the
    guarantee genuinely cannot be offered everywhere. The repo's recurring
    defect is an absence produced by the tool read as an absence in the world,
    so the one thing this may not do is fall back to path-based opens and report
    success — that is the code this replaces, wearing the fix's name.
    """

    def test_a_missing_at_syscall_declines_with_a_sentence(
        self, tmp_path, monkeypatch, loose_umask
    ):
        import _paths  # noqa: PLC0415

        monkeypatch.setitem(_paths._RELATIVE_OPS, "os.unlink(dir_fd=)", False)

        with pytest.raises(SystemExit) as exc:
            _paths.require_relative_ops(str(tmp_path))

        assert isinstance(exc.value.code, str), (
            f"exited {exc.value.code!r} — a bare number is a crash, and #582's "
            f"rule is that only a stated reason gets relabelled as a refusal "
            f"rather than killing the caller's invocation"
        )
        reason = str(exc.value.code)
        assert "os.unlink(dir_fd=)" in reason, reason
        assert "#598" in reason, reason

    def test_a_platform_with_every_at_syscall_is_silent(self, tmp_path):
        import _paths  # noqa: PLC0415

        assert _paths.require_relative_ops(str(tmp_path)) is None

    def test_the_probe_is_taken_at_import_not_per_call(self, monkeypatch, tmp_path):
        """Patching `os.open` must not be readable as a platform verdict.

        `os.supports_dir_fd` holds the *original* function objects, so a
        membership test taken after a test double is installed reports the
        double's absence from the set as a missing syscall. #583 learned this
        with `os.listdir`; the same trap is one line away here.
        """
        import _paths  # noqa: PLC0415

        monkeypatch.setattr(os, "open", lambda *a, **kw: (_ for _ in ()).throw(OSError))

        assert _paths.require_relative_ops(str(tmp_path)) is None


@posix_only
class TestTheAncestryOfTheRuntimeDirIsCheckedSince607:
    """The reachability both issues understate — found here, fixed in #607.

    #583 and #598 both describe the residual window as needing "write access to
    a directory supertool owns". That is true of the default locations, whose
    parents are inside `$HOME`. It was not true of `$XDG_RUNTIME_DIR`, which
    `_runtime_base()` accepted on the sole evidence that it `is_dir()`.

    An attacker who pre-creates `$XDG_RUNTIME_DIR/supertool` mode `0777` (no
    sticky bit) owns the parent of the leaf supertool then creates, owns and
    validates. POSIX grants `rename()` over any entry in a writable, non-sticky
    directory regardless of the entry's own owner or mode — so `0700` on the
    leaf was never the defence, and the swap the tests above simulate was
    performable by another uid.

    Recorded here as a finding when this file was written, deliberately not
    fixed in that PR — tightening the ancestry is a different change from
    making the daemon descriptor-relative, and the descriptor closes *this*
    file's hole regardless of who owns the parent. #607 did the tightening; the
    first test below is the same scenario, inverted, and stays here so the
    provenance survives. The full behaviour is pinned in
    `test_mcp_runtime_dir_ancestry_607.py`.
    """

    def test_a_world_writable_xdg_runtime_dir_is_now_refused(
        self, tmp_path, monkeypatch, loose_umask
    ):
        import _paths  # noqa: PLC0415

        xdg = tmp_path / "xdg"
        xdg.mkdir()
        _REAL_CHMOD(xdg, 0o777)
        monkeypatch.delenv("SUPERTOOL_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        with pytest.raises(SystemExit) as excinfo:
            _paths.runtime_dir()

        assert isinstance(excinfo.value.code, str), (
            "a refusal must carry a sentence, not a bare exit code"
        )
        assert str(xdg) in excinfo.value.code

    def test_a_writable_non_sticky_parent_lets_a_stranger_rename_the_leaf(
        self, tmp_path, loose_umask
    ):
        """The permission semantics, demonstrated rather than asserted from memory.

        Run as one uid, so this shows the *mechanism*: the mode of the leaf is
        irrelevant to whether an entry can be renamed out from under it. Only
        the parent's write bit and sticky bit decide, and nothing checks either.
        """
        parent = tmp_path / "attacker-owned"
        parent.mkdir()
        _REAL_CHMOD(parent, 0o777)
        leaf = parent / "mcp"
        leaf.mkdir(mode=0o700)
        before = _ident(leaf)

        os.rename(leaf, parent / "moved")
        (parent / "decoy").mkdir(mode=0o700)
        os.rename(parent / "decoy", leaf)

        assert _ident(leaf) != before, (
            "a 0700 directory we own was replaced through its parent — the leaf "
            "mode is not what gates this"
        )


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

def _open_and_claim(daemon_mod, name: str):
    """Validate the runtime dir, then claim the pidfile — the first daemon write.

    Written against whichever shape `daemon.py` has: today `claim_pidfile` takes
    a path, after the fix it takes a name plus the descriptor. Both are driven
    here so the assertion is about *where the file landed*, never about the
    signature.
    """
    import _paths  # noqa: PLC0415

    cwd = os.path.abspath(os.getcwd())
    try:
        fd, base = _paths.open_runtime_dir()
    except AttributeError:
        fd, base = _paths._open_runtime_dir()
    try:
        sock_name, pid_name = _paths.socket_pid_names(cwd, name)
        daemon_mod.claim_pidfile(pid_name, dir_fd=fd)
    except (AttributeError, TypeError):
        _sock, pid_path = _paths.socket_pid_paths(cwd, name)
        daemon_mod.claim_pidfile(pid_path)
    return fd, base


def _serve_and_capture_socket(daemon_mod):
    """Run a short daemon and report the path of the socket it actually bound.

    `socket.socket.bind` is wrapped rather than the daemon inspected, because
    the question is what the kernel was asked for, not what the code intended.
    A relative bind is resolved against the cwd in force at the time, which is
    what makes `os.getcwd()` the right thing to join it to.
    """
    seen = []
    real_bind = socket.socket.bind

    def recording_bind(self, address, *a, **kw):
        result = real_bind(self, address, *a, **kw)
        if isinstance(address, str):
            seen.append(os.path.join(os.getcwd(), address))
        return result

    socket.socket.bind = recording_bind
    try:
        spec = {"cmd": [sys.executable, "-c", "raise SystemExit(0)"], "idle_timeout": 1}
        daemon_mod.serve("cclsp", spec)
    finally:
        socket.socket.bind = real_bind
    return seen[0] if seen else None
