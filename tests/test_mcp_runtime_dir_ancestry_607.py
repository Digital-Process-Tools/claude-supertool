"""#607 — the runtime dir's *ancestry* is what gates who can replace it.

`_runtime_base()` accepts `$XDG_RUNTIME_DIR` on the sole evidence that it
`is_dir()`, and `_open_runtime_dir()` then checks the leaf it creates inside it:
owner, mode, held descriptor. Nothing has ever asked who owns the directories
*above* it.

#568, #583 and #598 each noticed the gap and each parked it as a metadata leak
rather than an access problem, on the reasoning that the leaf is `0700` so a
stranger cannot enter it. That reasoning is locally correct and globally wrong.
A stranger does not need to enter a directory to replace it: POSIX permits
`rename()` over any entry in a writable, non-sticky directory *regardless of
that entry's own owner or mode*. So the leaf's `0700` was never what gated this
— the parent's write bit and sticky bit were, and neither was looked at.

Measured on `master` before this was written, not taken from the issues:

    $XDG_RUNTIME_DIR = <tmp>/xdg              mode 0777   (attacker-writable)
    <tmp>/xdg/supertool                       mode 0777   (attacker pre-created)
    <tmp>/xdg/supertool/mcp                   mode 0700   accepted, exit 0, silent

The refusal here is deliberately not a relocation. Falling back to a private
directory supertool creates itself would move every warm daemon out from under
the clients still looking for it at the old path — a quiet failure traded for a
loud one, which is the shape this file exists to reject. The runtime dir either
verifies or the caller is told why, in a sentence, on the same terms as the
ownership refusal (#544) and the mode refusal (#568) next door.

**Would these tests pass if the check did nothing?** No. Every loud test builds
a real world-writable ancestor on disk and asserts a `SystemExit`; on `master`
each returns a path instead. Every silent test asserts *no output at all* on a
`/run/user/$UID`-shaped tree, which is the assertion a check that cries wolf
fails and the reason a real one survives contact with a desktop.
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
_REAL_OPEN = os.open

posix_only = pytest.mark.skipif(
    not hasattr(os, "geteuid"),
    reason="the runtime dir is ownership-checked; os.geteuid is required.",
)

needs_dir_fd = pytest.mark.skipif(
    not (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")),
    reason="O_DIRECTORY/O_NOFOLLOW are POSIX-only; the fd path declines without them.",
)

needs_relative_ops = pytest.mark.skipif(
    os.open not in os.supports_dir_fd,
    reason="walking up by `os.open('..', dir_fd=)` is POSIX-only.",
)


@pytest.fixture
def loose_umask():
    """Pin `umask 022`, as the sibling suites do.

    A developer on `umask 077` gets `0o700` from every `mkdir` here and would
    never see the modes these tests are about.
    """
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _chain(path) -> list:
    """Every directory from `path` up to the filesystem root, nearest first."""
    resolved = Path(os.path.realpath(str(path)))
    return [resolved, *resolved.parents]


def _requires_a_clean_chain(path) -> None:
    """Skip if the tree *above* the fixture is already loose.

    The silent tests assert that a well-formed runtime dir produces no output,
    and they can only own what they create. If the machine running them has a
    world-writable `/tmp/…` ancestor of its own, the correct outcome is a
    refusal and the test would be red for a true reason — which is a confusing
    way to learn about your own filesystem. Named rather than tolerated.
    """
    for component in _chain(path):
        st = os.stat(component)
        mode = stat.S_IMODE(st.st_mode)
        if st.st_uid not in (os.geteuid(), 0):
            pytest.skip(f"{component} is owned by uid {st.st_uid} on this machine")
        if mode & 0o022 and not mode & stat.S_ISVTX:
            pytest.skip(f"{component} is already {oct(mode)} on this machine")


def _refusal(callable_, *args, **kwargs) -> str:
    """Run something expected to refuse and return the sentence it refused with."""
    with pytest.raises(SystemExit) as excinfo:
        callable_(*args, **kwargs)
    message = excinfo.value.code
    assert isinstance(message, str), (
        f"a refusal must carry a sentence, not a bare exit code: {message!r}"
    )
    return message


def _assert_blames(message: str, component) -> None:
    """Assert *which* directory the refusal is about, not merely that it appears.

    `str(component) in message` is not that assertion and a mutant proved it:
    every ancestry refusal also names the runtime dir it protects, and every
    ancestor is a prefix of that path — so a substring check passes when the
    walk blames the wrong component, which is exactly the mutation
    ("sticky excuses a stranger-owned ancestor") that survived the first run.
    The blamed component is the one the sentence opens with.
    """
    expected = f"daemon: {os.path.realpath(str(component))} is "
    assert message.startswith(expected), (
        f"blamed something else — expected the sentence to open with "
        f"{expected!r}, got {message[:len(expected) + 40]!r}"
    )


# --------------------------------------------------------------------------
# The loud case — an ancestor a stranger can write
# --------------------------------------------------------------------------

@posix_only
@needs_dir_fd
@needs_relative_ops
class TestAWorldWritableAncestorIsRefused:
    """The attack the three issues each filed as a metadata leak."""

    def test_a_world_writable_xdg_runtime_dir_is_refused(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """`$XDG_RUNTIME_DIR` itself at `0777` — accepted silently on master."""
        import _paths  # noqa: PLC0415

        xdg = tmp_path / "xdg"
        xdg.mkdir()
        _REAL_CHMOD(xdg, 0o777)
        monkeypatch.delenv("SUPERTOOL_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        message = _refusal(_paths.runtime_dir)

        _assert_blames(message, xdg)
        assert "0o777" in message or "777" in message
        assert "chmod" in message, "an operator needs the command, not a diagnosis"

    def test_the_attackers_pre_created_parent_is_refused(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """The exact shape from the issue: `$XDG_RUNTIME_DIR/supertool` at `0777`.

        The leaf supertool creates inside it is `0700` and owned by us, and
        passes every check that existed before this. The parent is what lets a
        stranger `rename()` it away, and the parent is what is checked here.
        """
        import _paths  # noqa: PLC0415

        xdg = tmp_path / "xdg"
        xdg.mkdir(mode=0o700)
        squatted = xdg / "supertool"
        squatted.mkdir()
        _REAL_CHMOD(squatted, 0o777)
        monkeypatch.delenv("SUPERTOOL_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        message = _refusal(_paths.runtime_dir)

        _assert_blames(message, squatted)

    def test_a_loose_ancestor_further_up_is_refused(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """The walk does not stop at the parent — a grandparent is as good a lever."""
        import _paths  # noqa: PLC0415

        grand = tmp_path / "grand"
        grand.mkdir()
        base = grand / "a" / "b" / "rt"
        _REAL_CHMOD(grand, 0o777)
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(base))

        message = _refusal(_paths.runtime_dir)

        _assert_blames(message, grand)

    def test_a_group_writable_non_sticky_ancestor_is_refused(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """`ssh`'s `StrictModes` rule, and for the same reason.

        A group-writable directory is a lever for every member of that group,
        which is a smaller blast radius than `o+w` and not a zero one. `ssh`
        refuses a key under one; this refuses a runtime dir under one.
        """
        import _paths  # noqa: PLC0415

        parent = tmp_path / "shared"
        parent.mkdir()
        _REAL_CHMOD(parent, 0o775)
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(parent / "rt"))

        message = _refusal(_paths.runtime_dir)

        _assert_blames(message, parent)

    def test_an_ancestor_owned_by_a_stranger_is_refused(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """Ownership, not just mode — a stranger's `0755` is still their directory.

        A stranger-owned ancestor cannot be *created* by a test running as one
        uid, so the stranger is introduced from the other side: the effective
        uid this check compares against is moved instead. Ancestors owned by
        `root` stay acceptable (`/`, `/run`, `/Users` are all root-owned on a
        healthy machine), so the injected uid is neither ours nor 0.
        """
        import _paths  # noqa: PLC0415

        base = tmp_path / "rt"
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(base))
        base.mkdir(mode=0o700, parents=True)
        fd = _REAL_OPEN(str(base), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stranger = os.geteuid() + 4242
        try:
            message = _refusal(
                _paths._verify_ancestry, fd, os.path.realpath(base), lambda: stranger
            )
        finally:
            os.close(fd)

        assert "uid" in message
        _assert_blames(message, tmp_path)

    def test_the_sticky_exception_does_not_cover_a_strangers_directory(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """`1777` owned by someone else is not `/tmp`, and is not safe.

        The sticky bit stops *other* users renaming entries — it does not stop
        the directory's own owner, who may remove any entry in it. So the
        ownership rule has to hold independently of the sticky exception, and
        the two must not be written as alternatives.
        """
        import _paths  # noqa: PLC0415

        parent = tmp_path / "sticky"
        parent.mkdir()
        _REAL_CHMOD(parent, 0o1777)
        base = parent / "rt"
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(base))
        base.mkdir(mode=0o700, parents=True)
        fd = _REAL_OPEN(str(base), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stranger = os.geteuid() + 4242
        try:
            message = _refusal(
                _paths._verify_ancestry, fd, os.path.realpath(base), lambda: stranger
            )
        finally:
            os.close(fd)

        _assert_blames(message, parent)


# --------------------------------------------------------------------------
# The silent case — the one that decides whether anybody keeps the check on
# --------------------------------------------------------------------------

@posix_only
@needs_dir_fd
@needs_relative_ops
class TestTheOrdinaryDesktopStaysSilent:
    """`systemd` sets `$XDG_RUNTIME_DIR=/run/user/$UID`, `0700`, owned by you.

    If this check produces one false alarm there, the first person to hit it
    turns it off and it protects nobody. So the ordinary shapes are pinned as
    hard as the attack: no refusal, no stdout, no stderr.
    """

    def test_a_0700_user_owned_runtime_dir_produces_no_output(
        self, tmp_path, monkeypatch, loose_umask, capsys
    ):
        import _paths  # noqa: PLC0415

        _requires_a_clean_chain(tmp_path)
        run_user = tmp_path / "run" / "user" / str(os.geteuid())
        run_user.mkdir(parents=True)
        _REAL_CHMOD(run_user, 0o700)
        monkeypatch.delenv("SUPERTOOL_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_user))

        base = _paths.runtime_dir()

        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", ""), "the common case must be silent"
        assert Path(base).is_dir()

    def test_a_sticky_world_writable_ancestor_is_accepted(
        self, tmp_path, monkeypatch, loose_umask, capsys
    ):
        """`/tmp` is `1777` and always has been. Sticky is the whole difference.

        With the sticky bit set, only the entry's owner (or the directory's
        owner, or root) may rename or unlink it — so a `0700` directory we own
        inside `/tmp` cannot be swapped by a co-tenant, and refusing here would
        be the false alarm that gets the check disabled.
        """
        import _paths  # noqa: PLC0415

        _requires_a_clean_chain(tmp_path)
        shared = tmp_path / "shared-tmp"
        shared.mkdir()
        _REAL_CHMOD(shared, 0o1777)
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(shared / "rt"))

        base = _paths.runtime_dir()

        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")
        assert Path(base).is_dir()

    def test_the_walk_reaches_the_filesystem_root_without_complaining(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """Root-owned `0755` ancestors are the normal state of `/`, `/var`, `/home`.

        Pinned as its own test because "owner-or-root" is the half of the rule
        that a stricter "owner only" would break on every machine there is —
        the walk goes all the way up, so it passes through root-owned
        directories by construction.
        """
        import _paths  # noqa: PLC0415

        _requires_a_clean_chain(tmp_path)
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))

        base = _paths.runtime_dir()

        root_owned = [
            str(c) for c in _chain(base) if os.stat(c).st_uid == 0
        ]
        assert root_owned, "this machine has no root-owned ancestor — test is vacuous"
        assert Path(base).is_dir()


# --------------------------------------------------------------------------
# "The check failed" and "the check could not run" are different answers
# --------------------------------------------------------------------------

@posix_only
@needs_dir_fd
class TestTheCheckThatCannotRunSaysSo:
    """The three-state contract (`docs/validators.md`) at this call site.

    The vocabulary already exists in this module — `_require_dir_fd` and
    `require_relative_ops` both exit with a sentence that says the question is
    unanswerable here rather than answered badly. Nothing new is designed; a
    third member joins the family (#263's lesson: check the sibling call site
    before inventing).
    """

    def test_a_platform_without_dir_fd_declines_rather_than_skipping_the_check(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """Windows has no `*at` syscalls, so it cannot walk up from a descriptor.

        Unreachable in practice — `os.geteuid` is absent on the same platforms
        and #544 refuses first — and written independently anyway, for the same
        reason `require_relative_ops` is: a guard that is correct only because
        another guard happens to run earlier is a comment pretending to be code.
        """
        import _paths  # noqa: PLC0415

        monkeypatch.setattr(_paths, "_ANCESTRY_DIR_FD", False)
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))

        message = _refusal(_paths.runtime_dir)

        assert "dir_fd" in message
        assert "cannot" in message, "a decline says it could not ask, not that it failed"

    @needs_relative_ops
    def test_a_runtime_dir_with_no_search_bit_is_refused_on_its_own_terms(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """`0o600` — owner-only, and unwalkable. A behaviour change from #568.

        `#568`'s mode check has nothing to say about `0o600`: its question is
        exposure to other users and the answer is no, so it accepted it and
        `test_mcp_runtime_dir_mode_568.py` pinned that. The ancestry walk cannot
        start there — `..` needs search permission on the directory it is opened
        from — and neither could the daemon, which needs the same bit to open
        its own socket and pidfile. Reachable only where the tightening `fchmod`
        is a no-op (exFAT/SMB), which is why it took a sabotaged chmod to reach.

        Its own sentence rather than the generic one: "your runtime dir is
        missing `x`" and "something above your runtime dir is world-writable"
        send an operator to two different directories.
        """
        import _paths  # noqa: PLC0415

        base = tmp_path / "rt"
        base.mkdir(mode=0o700)
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(base))
        real_fchmod = os.fchmod
        monkeypatch.setattr(os, "fchmod", lambda *a, **kw: None)
        _REAL_CHMOD(base, 0o600)
        try:
            message = _refusal(_paths.runtime_dir)
        finally:
            monkeypatch.setattr(os, "fchmod", real_fchmod)
            _REAL_CHMOD(base, 0o700)

        assert "search permission" in message
        assert f"chmod 700 {os.path.realpath(base)}" in message

    @needs_relative_ops
    def test_an_unwalkable_ancestor_declines_instead_of_passing(
        self, tmp_path, monkeypatch, loose_umask
    ):
        """A component we cannot open is an absence of information, not a pass.

        Distinct wording from the finding, deliberately: an operator who reads
        "is world-writable" goes and runs `chmod`; one who reads "could not be
        checked" goes and looks at why. Collapsing the two would send them to
        the wrong place.
        """
        import _paths  # noqa: PLC0415

        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))

        def refusing_open(path, *args, **kwargs):
            if path == "..":
                raise OSError(errno.EACCES, "Permission denied")
            return _REAL_OPEN(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", refusing_open)

        message = _refusal(_paths.runtime_dir)

        assert "could not" in message.lower()
        assert "world-writable" not in message, (
            "an unanswerable question must not be reported as a finding"
        )


# --------------------------------------------------------------------------
# Found while reading the same function, not briefed
# --------------------------------------------------------------------------

@posix_only
class TestTheConfiguredBaseMustBeAbsolute:
    """A relative runtime dir is resolved against the *current working directory*.

    `_runtime_base()` returns `Path(override)` and `Path(xdg) / "supertool"`
    with no absoluteness check, and the freedesktop spec requires
    `$XDG_RUNTIME_DIR` to be an absolute path precisely because of this. A
    relative value puts the daemon's socket and pidfile inside whatever project
    directory supertool happens to be invoked from — a per-cwd runtime dir with
    the project's own ancestry, which for a shared checkout is exactly the
    world-writable parent this issue is about, arrived at from the other side.

    Refused rather than silently rewritten: guessing that a relative path meant
    `$HOME/…` would relocate a daemon location on the operator's behalf.
    """

    def test_a_relative_supertool_runtime_dir_is_refused(self, monkeypatch, tmp_path):
        import _paths  # noqa: PLC0415

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", "runtime/mcp")

        message = _refusal(_paths.runtime_dir)

        assert "SUPERTOOL_RUNTIME_DIR" in message
        assert "absolute" in message
        assert not (tmp_path / "runtime").exists(), (
            "refusing must not have created the directory it refused"
        )

    def test_a_relative_xdg_runtime_dir_is_refused(self, monkeypatch, tmp_path):
        import _paths  # noqa: PLC0415

        (tmp_path / "xdg").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SUPERTOOL_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "xdg")

        message = _refusal(_paths.runtime_dir)

        assert "XDG_RUNTIME_DIR" in message
        assert "absolute" in message
