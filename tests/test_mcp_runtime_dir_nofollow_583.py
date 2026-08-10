"""The runtime dir that is checked must be the runtime dir that is used (#583).

`presets/mcp/_paths.py`'s `runtime_dir()` reached the same directory by path four
times over — `mkdir`, `chmod`, `stat` for ownership, `stat` for mode (#568) — and
then returned a **string** that `list_pidfiles`, `stop.py` and the daemon's
`bind` resolve a fifth, sixth and seventh time. `os.chmod` and `os.stat` follow
symlinks. So with `SUPERTOOL_RUNTIME_DIR` pointing at a symlink to a directory:

    base.mkdir(mode=0o700, parents=True, exist_ok=True)   # succeeds on the link
    os.chmod(base, 0o700)                                 # tightens the target
    st = os.stat(base)                                    # describes the target
    ...
    return str(base)                                      # still the link

Every check passes and every check describes the *target*, which is the right
object to describe — the target is what gets used. That is worth being precise
about, because it is not the bug: reading through the link is not by itself a
wrong answer.

The bug is that nothing ties the seven resolutions together. Each one re-asks
the filesystem where `base` points, and between any two of them the link can be
repointed. The last resolution is the one that matters and it happens in a
different process (the daemon's `bind`), arbitrarily later, against a path that
still goes through the link. A validated `0700` directory we own is no statement
at all about the directory the socket lands in.

The fix holds one `os.open(resolved, O_DIRECTORY | O_NOFOLLOW)` fd across the
whole validation and answers every question with `fchmod`/`fstat` on it, and
returns the **symlink-free** path — so the string handed to callers cannot be
redirected by repointing a link, because it does not traverse one.

**Symlinks are not banned.** Pointing `SUPERTOOL_RUNTIME_DIR` at a symlink is a
reasonable thing to have done on purpose, and the tests below require it to keep
working: the link is resolved once, deliberately, and the object on the far end
is what gets validated and named. What is refused is a path that changes shape
*while* being validated, and a platform that cannot open a directory fd at all.

**Where the guarantee stops.** `socket.bind()` takes no `dir_fd`, and the daemon
is a separate process that receives a path, so the final open of the socket is
still by path. What these tests pin is that the path it receives is one no
symlink swap can redirect, and that everything `_paths.py` itself asserts about
that directory is asserted against a held fd. The residual window — replacing
the resolved leaf directory itself — needs write access to a parent supertool
owns, and is stated in `docs/mcp-integration.md` rather than claimed closed.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

import pytest

from _symlink import require_symlink

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


def _ident(path) -> tuple:
    """(st_dev, st_ino) — the only honest way to say 'the same directory'."""
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def loose_umask():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture
def linked(tmp_path, monkeypatch, loose_umask):
    """`SUPERTOOL_RUNTIME_DIR` is a symlink to `good`; `evil` is the swap target.

    Both are directories this uid owns, so nothing here depends on a second
    account. `evil` is `0o777` — the state the mode check exists to refuse —
    which is what makes a redirect onto it visible rather than merely different.
    """
    require_symlink()
    good = tmp_path / "good"
    good.mkdir(mode=0o700)
    evil = tmp_path / "evil"
    evil.mkdir()
    _REAL_CHMOD(evil, 0o777)
    link = tmp_path / "rt"
    link.symlink_to(good)
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(link))

    class Setup:
        pass

    setup = Setup()
    setup.link, setup.good, setup.evil = link, good, evil
    setup.repoint = lambda: (link.unlink(), link.symlink_to(evil))
    return setup


@pytest.fixture
def no_chmod(monkeypatch):
    """Make every way of chmod-ing the runtime dir fail — the exFAT/SMB case.

    Keyed on `(st_dev, st_ino)` rather than on the path string, because the fix
    tightens the directory through a held fd (`os.fchmod`) and an fd carries no
    path. A fixture that only patched `os.chmod` by name would stop simulating
    anything the moment the implementation stopped calling it — the mode tests
    would go green because the chmod started working, not because the check did.
    """
    targets: set = set()
    real_chmod, real_fchmod = os.chmod, os.fchmod

    def _fail_if_target(path):
        try:
            st = os.stat(path)
        except OSError:
            return False
        return (st.st_dev, st.st_ino) in targets

    def _chmod(path, mode, *a, **kw):
        if _fail_if_target(path):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_chmod(path, mode, *a, **kw)

    def _fchmod(fd, mode):
        if _fail_if_target(fd):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_fchmod(fd, mode)

    monkeypatch.setattr(os, "chmod", _chmod)
    monkeypatch.setattr(os, "fchmod", _fchmod)
    return targets


@posix_only
@needs_dir_fd
class TestTheReturnedPathCannotBeRedirected:
    """The string callers re-resolve must not traverse a link."""

    def test_the_returned_path_is_the_resolved_directory(self, linked):
        import _paths  # noqa: PLC0415

        got = _paths.runtime_dir()

        assert _ident(got) == _ident(linked.good)
        assert Path(got) == linked.good, (
            f"returned {got!r} — the daemon binds inside this path in another "
            f"process, so it must already be the object we validated, not a "
            f"link to it"
        )

    def test_the_returned_path_has_no_symlink_components(self, linked):
        import _paths  # noqa: PLC0415

        got = _paths.runtime_dir()

        assert os.path.realpath(got) == got, (
            f"{got!r} still resolves to {os.path.realpath(got)!r} — a path with "
            f"a link in it is a path someone else can re-aim"
        )

    def test_repointing_the_link_afterwards_cannot_move_the_directory(self, linked):
        """The window that matters: the swap happens after every check passed."""
        import _paths  # noqa: PLC0415

        got = _paths.runtime_dir()
        linked.repoint()

        assert _ident(got) == _ident(linked.good), (
            f"{got!r} now resolves to the 0o777 directory the mode check would "
            f"have refused — validated one object, handed back another"
        )
        assert _mode(got) == 0o700, oct(_mode(got))

    def test_socket_and_pid_paths_do_not_go_through_the_link(self, linked):
        import _paths  # noqa: PLC0415

        sock, pid = _paths.socket_pid_paths("/some/project", "cclsp")
        linked.repoint()

        assert Path(sock).parent == linked.good, sock
        assert Path(pid).parent == linked.good, pid


@posix_only
@needs_dir_fd
class TestASymlinkedRuntimeDirStillWorks:
    """The goal is one object, not a ban. A deliberate link must keep working."""

    def test_a_symlink_to_a_dir_we_own_is_accepted(self, linked):
        import _paths  # noqa: PLC0415

        assert _paths.runtime_dir()  # no SystemExit

    def test_a_loose_target_is_still_tightened_through_the_link(self, linked):
        import _paths  # noqa: PLC0415

        _REAL_CHMOD(linked.good, 0o755)

        _paths.runtime_dir()

        assert _mode(linked.good) == 0o700, oct(_mode(linked.good))

    def test_a_symlink_to_a_missing_dir_is_refused_with_a_sentence(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """A dangling link is `mkdir`'s EEXIST — a refusal, never a traceback."""
        import _paths  # noqa: PLC0415

        link = tmp_path / "rt"
        link.symlink_to(tmp_path / "nowhere")
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(link))

        with pytest.raises(SystemExit) as exc:
            _paths.runtime_dir()

        assert isinstance(exc.value.code, str), repr(exc.value.code)
        assert str(link) in str(exc.value.code)


@posix_only
@needs_dir_fd
class TestTheChecksDescribeTheObjectTheyValidated:
    """A refusal has to name the directory that is wrong, not the link to it."""

    def test_a_foreign_owner_refusal_names_the_resolved_dir(self, linked, monkeypatch):
        import _paths  # noqa: PLC0415

        mine = os.stat(linked.good).st_uid
        monkeypatch.setattr(os, "geteuid", lambda: mine + 1)

        with pytest.raises(SystemExit) as exc:
            _paths.runtime_dir()
        reason = str(exc.value.code)

        assert isinstance(exc.value.code, str), repr(exc.value.code)
        assert str(linked.good) in reason, (
            f"named {reason!r} — `chmod`/`chown` on the link path is not what "
            f"the operator has to fix"
        )

    def test_a_loose_target_that_cannot_be_tightened_names_the_resolved_dir(
        self, linked, no_chmod
    ):
        import _paths  # noqa: PLC0415

        _REAL_CHMOD(linked.good, 0o755)
        no_chmod.add(_ident(linked.good))

        with pytest.raises(SystemExit) as exc:
            _paths.runtime_dir()
        reason = str(exc.value.code)

        assert "755" in reason, reason
        assert str(linked.good) in reason, reason


@posix_only
@needs_dir_fd
class TestAPathThatChangesShapeMidValidationIsRefused:
    """Resolve once, pin the result. If the pin fails, say so — never re-resolve."""

    def test_a_leaf_that_became_a_symlink_after_the_resolve_is_refused(
        self, linked, monkeypatch
    ):
        """The attacker acts between the resolve and the open.

        Simulated by having the resolve hand back a path whose leaf *is* a link,
        which is exactly the state a swap in that window leaves behind. The
        `O_NOFOLLOW` open must fail and the failure must be a sentence — falling
        back to a following open would put the whole exercise back where it
        started.
        """
        import _paths  # noqa: PLC0415

        real = os.path.realpath
        monkeypatch.setattr(
            os.path,
            "realpath",
            lambda p, *a, **kw: str(linked.link)
            if str(p) == str(linked.link)
            else real(p, *a, **kw),
        )

        with pytest.raises(SystemExit) as exc:
            _paths.runtime_dir()

        assert isinstance(exc.value.code, str), repr(exc.value.code)
        assert str(linked.link) in str(exc.value.code)


@posix_only
class TestAPlatformThatCannotOpenADirectoryFdSaysSo:
    """Three states: ok, finding, declined. Missing flags are declined (#544)."""

    @pytest.mark.parametrize("flag", ["O_DIRECTORY", "O_NOFOLLOW"])
    def test_a_missing_flag_is_a_stated_refusal(
        self, linked, monkeypatch, flag
    ):
        import _paths  # noqa: PLC0415

        monkeypatch.delattr(os, flag, raising=False)

        with pytest.raises(SystemExit) as exc:
            _paths.runtime_dir()
        reason = str(exc.value.code)

        assert isinstance(exc.value.code, str), (
            f"a refusal must carry a sentence, not {exc.value.code!r} — "
            f"stop.py relabels only a stated reason as EXIT_REFUSED, and "
            f"MCPClient degrades only on one (#582)"
        )
        assert flag in reason, f"the missing capability is not named: {reason!r}"
        assert "SUPERTOOL_RUNTIME_DIR" in reason, f"no way out offered: {reason!r}"

    def test_the_refusal_does_not_silently_check_the_target_instead(
        self, linked, monkeypatch
    ):
        """Declining is the point: no fallback to a path-following check."""
        import _paths  # noqa: PLC0415

        monkeypatch.delattr(os, "O_DIRECTORY", raising=False)

        with pytest.raises(SystemExit):
            _paths.runtime_dir()


@posix_only
@needs_dir_fd
class TestEnumerationUsesTheValidatedObject:
    """`list_pidfiles` is the one consumer that can be handed the fd itself."""

    def test_pidfiles_are_listed_from_the_resolved_dir(self, linked):
        import _paths  # noqa: PLC0415

        (linked.good / "supertool-mcp-aaaaaaaaaaaa.pid").write_text("1234", encoding="utf-8")
        (linked.evil / "supertool-mcp-bbbbbbbbbbbb.pid").write_text("1234", encoding="utf-8")

        pidfiles, reason = _paths.list_pidfiles()

        assert reason == "", reason
        assert [Path(p).name for p in pidfiles] == ["supertool-mcp-aaaaaaaaaaaa.pid"]
        assert all(Path(p).parent == linked.good for p in pidfiles), (
            f"{pidfiles} — stop.py opens and unlinks these paths, so a link in "
            f"them is a link in the kill path"
        )

    def test_repointing_the_link_cannot_change_what_was_enumerated(self, linked):
        import _paths  # noqa: PLC0415

        (linked.good / "supertool-mcp-aaaaaaaaaaaa.pid").write_text("1234", encoding="utf-8")
        pidfiles, _ = _paths.list_pidfiles()
        linked.repoint()

        assert pidfiles, "the pidfile was there to be found"
        assert all(os.path.exists(p) for p in pidfiles), (
            f"{pidfiles} — repointing the link made the enumerated pidfiles "
            f"vanish, which is stop.py being handed rows it cannot act on and "
            f"a path that now names something else entirely"
        )
