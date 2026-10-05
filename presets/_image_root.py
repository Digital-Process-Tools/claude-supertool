#!/usr/bin/env python3












































































from __future__ import annotations

import os
import stat
import tempfile
from typing import Optional, Tuple



ROOT_PREFIX = "supertool-images"





_HAVE_DIR_FD = all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW"))




_UNSET = object()


def _euid() -> Optional[int]:

    geteuid = getattr(os, "geteuid", None)
    return None if geteuid is None else geteuid()


def _why(exc: OSError) -> str:
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


def default_root(suffix: str = "") -> str:






    uid = _euid()
    name = ROOT_PREFIX if uid is None else "{0}-{1}".format(ROOT_PREFIX, uid)
    return os.path.join(tempfile.gettempdir(), name + suffix)


def refusal(st, uid=_UNSET) -> str:









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






        return ""
    if st.st_uid != uid:
        return ("it is owned by uid {0}, not by us ({1}), so nothing here "
                "established that we may write into it".format(st.st_uid, uid))
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:



        return ("its mode is {0} -- group or other can reach it, and the chmod "
                "to 0700 did not take".format(oct(mode)))
    return ""


def is_inside(candidate: str, directory: str) -> bool:





















    root = os.path.realpath(directory)
    target = os.path.realpath(candidate)
    return target == root or target.startswith(root + os.sep)


def ensure(root: str) -> Tuple[Optional[str], str]:










    try:
        os.mkdir(root, 0o700)
    except FileExistsError:


        pass
    except OSError as exc:
        return None, "{0} could not be created ({1})".format(root, _why(exc))

    if _HAVE_DIR_FD:
        return _verify_by_fd(root)
    return _verify_by_lstat(root)


def _verify_by_fd(root: str) -> Tuple[Optional[str], str]:







    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        return None, (
            "{0} could not be opened as a directory without following a link "
            "({1}) -- a symlink or a file is on that name, or it stopped being "
            "a directory while we were looking at it".format(root, _why(exc))
        )
    try:






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







    try:
        st = os.lstat(root)
    except OSError as exc:
        return None, "{0} could not be inspected ({1})".format(root, _why(exc))
    why = refusal(st)
    if why:
        return None, "{0} is not a root this process can use: {1}".format(root, why)
    return root, ""
