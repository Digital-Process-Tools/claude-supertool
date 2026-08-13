#!/usr/bin/env python3
"""A root created by this process and proven ours (#1493).

Two callers, and the name is the older one: `gl-issue`'s attachment root, which
is what `default_root` and `ROOT_PREFIX` are for, and `presets/watch/naming.py`'s
derived state directory, which brings its own path and uses `ensure`/`refusal`
only (#1518). Everything below the `default_root` line is about a directory, not
about images. The rest of this docstring is written from the attachment root
because that is the case that produced it; the second caller's leaf is public and
committed rather than per-uid, which makes the squat residual below more likely
to be hit, not different in kind.

`gl-issue` downloaded issue attachments under the literal
`"/tmp/supertool-images"`. Two defects live in that one constant:

* **A fixed name in a shared, world-writable directory.** Any local user can
  take that name before we do -- as a directory of their own, or as a symlink
  pointing anywhere. `is_inside` below realpaths *both* of its arguments, which
  is correct for the leaf and says nothing at all about the root: a symlink
  planted at the root is resolved through on both sides, so containment answers
  `True` about the attacker's directory just as readily as about ours. The check
  was never wrong. Nothing established what it was checking against, and that is
  what the rest of this module is.
* **A hardcoded POSIX literal.** A leading-slash path is anchored to the current
  drive on Windows, so it landed in a `tmp/supertool-images` at the drive root
  rather than under the platform temp directory.

## Why a stable per-user root and not `mkdtemp()`

`tempfile.mkdtemp()` per invocation is the airtight answer and it was rejected:
`gl-issue` prints the attachment paths under `## Images` and invites the reader
to open them, so a root that moves every call makes the receipt a path that only
existed during the call that printed it. The stable root keeps the receipt
meaningful, and its safety comes from proof rather than from luck.

`O_NOFOLLOW` on a *fixed* root is not the symlink ban it looks like. It is the
statement that the name we are about to write under is a real directory at the
moment we look, held by a descriptor, so the tightening, the ownership check and
the mode check are three questions about one object rather than three
independent path resolutions that can each answer about something different.
That is the arrangement `presets/mcp/_paths.py` arrived at for the runtime
directory (#583/#598/#544/#568) and this is the same shape, one severity down --
the writes here are still by path, so what is bought is a verified root and not
a race-free write.

## What is left, said out loud

**A co-tenant can still squat the name.** Another local uid can create
`<temp>/supertool-images-<our uid>` first, and we then refuse rather than write
into it -- attachments stop working until it is removed. That is the correct side
of the trade (a loud refusal beats a write into somebody else's directory) and it
is not closed, only named. The per-uid suffix keeps this from happening by
accident between two ordinary users sharing a machine.

## Windows, and the grade of these claims

`tempfile.gettempdir()` on Windows resolves inside the user profile
(`%LOCALAPPDATA%\\Temp` by default), which is not the shared, world-writable
directory the whole finding rests on -- so moving off the literal is most of the
Windows repair, and there is no `O_NOFOLLOW` or `O_DIRECTORY` to reach for
anyway.

Two of the four arms below cannot be asked there: `st_uid` is a constant on
Windows and the permission bits are synthesized, so an ownership or mode verdict
would be a sentence with no measurement behind it. They are skipped rather than
faked, which is this repo's rule -- `docs/validators.md` §"Declining instead of
guessing". What still answers on Windows is the pair that matters there: the
name must not be a symlink and must not be a reparse point (a junction, which
`stat.S_ISLNK` denies and which `st_reparse_tag` is the only way to see).

**That Windows behaviour is reasoned from CPython's documented `st_reparse_tag`
and `gettempdir()` resolution order, not observed on a Windows host** -- the same
grade #627 recorded for itself, and stated here rather than left for a reader to
assume.
"""
from __future__ import annotations

import os
import stat
import tempfile
from typing import Optional, Tuple

#: The leaf name, before the per-user suffix. Exported so a test names the same
#: string the module does rather than restating a literal that can drift.
ROOT_PREFIX = "supertool-images"

# Probed at import. Both flags are POSIX and CPython gates them on the C macro,
# so their absence *is* the Windows branch -- asked as a capability rather than
# read off `os.name`, because the question here is "can this be held open as a
# directory without following a link", and that is what has to be true.
_HAVE_DIR_FD = all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW"))

#: `refusal`'s uid argument, distinguished from a caller who really means
#: "nobody" -- so a test can exercise the ownership arm on a platform where
#: `os.geteuid` does not exist.
_UNSET = object()


def _euid() -> Optional[int]:
    """Our effective uid, or None where the question cannot be asked."""
    geteuid = getattr(os, "geteuid", None)
    return None if geteuid is None else geteuid()


def _why(exc: OSError) -> str:
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


def default_root(suffix: str = "") -> str:
    """The attachment root for this user on this platform.

    The uid goes in the name only where there is a uid to put there: on Windows
    `gettempdir()` is already inside the user's profile, and adding a constant
    `st_uid` of 0 would read as a per-user name while being nothing of the kind.
    """
    uid = _euid()
    name = ROOT_PREFIX if uid is None else "{0}-{1}".format(ROOT_PREFIX, uid)
    return os.path.join(tempfile.gettempdir(), name + suffix)


def refusal(st, uid=_UNSET) -> str:
    """Empty when this stat describes a root only we can write, else why not.

    A pure function of the stat, and deliberately so: the ownership and mode arms
    are unreachable through the filesystem on Windows, so a caller's tests can
    still exercise them here instead of reporting a coverage they do not have.

    Never returns a bare `False`. A rejection without a reason is the absence
    this repo keeps filing, one layer in.
    """
    if uid is _UNSET:
        uid = _euid()
    if stat.S_ISLNK(st.st_mode):
        return "it is a symlink, and the root of a write must be a real directory"
    if getattr(st, "st_reparse_tag", 0):
        return ("it is a reparse point (a junction or a link), and the root of a "
                "write must be a real directory")
    if not stat.S_ISDIR(st.st_mode):
        return "it is not a directory"
    if uid is None:
        # Windows: `st_uid` is a constant and the mode bits are synthesized, so
        # both remaining arms would be assertions with no measurement behind
        # them. Declining them is not the same as passing them, and the caller's
        # refusal is not weakened by their absence -- the symlink and reparse
        # arms above are the shapes reachable in a per-user temp directory,
        # which is where `default_root` puts this on Windows.
        return ""
    if st.st_uid != uid:
        return ("it is owned by uid {0}, not by us ({1}), so nothing here "
                "established that we may write into it".format(st.st_uid, uid))
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        # `& 0o077` rather than `!= 0o700`: the question is reach by other
        # users, so an odd owner-only mode passes and a group-read-only one does
        # not. Same spelling as `presets/mcp/_paths.py` for the same reason.
        return ("its mode is {0} -- group or other can reach it, and the chmod "
                "to 0700 did not take".format(oct(mode)))
    return ""


def is_inside(candidate: str, directory: str) -> bool:
    """True when `candidate` really resolves inside `directory`.

    Compared after realpath on both sides, and with a trailing separator on the
    directory so a sibling like `<temp>/images-other` cannot pass as
    `<temp>/images`.

    **What this establishes, and what it does not (#1493).** Realpathing both
    sides is right for the *leaf*: it is what makes a `..` or a symlink in a
    remote-chosen name resolve to where the write would actually land, and be
    compared against where the root actually is. It establishes nothing whatever
    about `directory` being a directory anyone should write into -- a symlink
    planted at the root itself is resolved through on *both* sides, so this
    answers `True` about the attacker's directory exactly as readily as about
    ours. It is a containment test, not an ownership test, and it never was one.
    `ensure` is what establishes the root; every call to this belongs against a
    root that came back from it.

    It lives here rather than once per forge preset because both `gl-issue` and
    `gh-issue` ask it about a root this module hands out, and two copies of a
    containment test are two chances for one of them to drift (#1506).
    """
    root = os.path.realpath(directory)
    target = os.path.realpath(candidate)
    return target == root or target.startswith(root + os.sep)


def ensure(root: str) -> Tuple[Optional[str], str]:
    """`(root, "")` when `root` is a directory we made and can prove is ours.

    `(None, reason)` otherwise, and the reason is always a sentence a caller can
    print. Three states, never two: an unanswerable root is not an empty one.

    The leaf is created **non-recursively**, on purpose. `os.makedirs` would
    manufacture every missing component and verify none of them, which is the
    same hole one level up; a root that is a direct child of an existing
    directory adds exactly one component, and that is the one this checks.
    """
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        # Left by an earlier invocation -- or by somebody else. Which one is
        # settled below by stat, not by having found it where we expected it.
        pass
    except OSError as exc:
        return None, "{0} could not be created ({1})".format(root, _why(exc))

    if _HAVE_DIR_FD:
        return _verify_by_fd(root)
    return _verify_by_lstat(root)


def _verify_by_fd(root: str) -> Tuple[Optional[str], str]:
    """POSIX: hold the directory open, then ask the descriptor everything.

    `O_NOFOLLOW` can only fire here if the name stopped being a real directory
    between the mkdir above and this open -- which is precisely the swap this
    refuses to run through. It gets a refusal rather than a second attempt,
    because retrying is the behaviour being removed.
    """
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        return None, (
            "{0} could not be opened as a directory without following a link "
            "({1}) -- a symlink or a file is on that name, or it stopped being "
            "a directory while we were looking at it".format(root, _why(exc))
        )
    try:
        # `fchmod`, not `chmod`: `mode=` on the mkdir applies only when we are
        # the ones creating the leaf, so a root left loose by an earlier run
        # would otherwise keep its mode. A path-based chmod would tighten
        # whatever the name points at *now*, which is not necessarily what the
        # fstat below describes. Its result is checked rather than assumed --
        # the mode arm of `refusal` is what enforces it.
        try:
            os.fchmod(fd, 0o700)
        except OSError:
            pass
        try:
            st = os.fstat(fd)
        except OSError as exc:
            return None, "{0} could not be inspected ({1})".format(root, _why(exc))
        why = refusal(st)
    finally:
        os.close(fd)
    if why:
        return None, "{0} is not a root this process can use: {1}".format(root, why)
    return root, ""


def _verify_by_lstat(root: str) -> Tuple[Optional[str], str]:
    """Windows: no `O_DIRECTORY`, no `O_NOFOLLOW`, no directory descriptor.

    `os.lstat` is what remains, and it is enough for the two arms that answer
    there -- it does not follow the final component, so a symlink or a junction
    on the name is visible rather than resolved through. There is no `fchmod`
    step because the mode bits it would set are not what gates access here.
    """
    try:
        st = os.lstat(root)
    except OSError as exc:
        return None, "{0} could not be inspected ({1})".format(root, _why(exc))
    why = refusal(st)
    if why:
        return None, "{0} is not a root this process can use: {1}".format(root, why)
    return root, ""
