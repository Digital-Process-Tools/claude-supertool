





























































































from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))       
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  
import model  
import _image_root  




SAFE_TTL_SECONDS = 24 * 60 * 60



_CACHEABLE_STATES = ("safe", "suspect")

_ROOT_NAME = "supertool-classify-cache"


def _euid() -> Optional[int]:





    geteuid = getattr(os, "geteuid", None)
    return None if geteuid is None else geteuid()


def default_dir() -> str:




    uid = _euid()
    name = _ROOT_NAME if uid is None else "{0}-{1}".format(_ROOT_NAME, uid)
    return os.path.join(tempfile.gettempdir(), name)


def version() -> str:









    blob = ("\x1f".join(model.AXES) + "\x1e" + model._SYSTEM_PROMPT).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def key(text: str, version_: Optional[str] = None) -> str:








    v = version_ if version_ is not None else version()
    blob = (v + "\x00" + text).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _discard(tmp: str) -> None:




    try:
        os.unlink(tmp)
    except OSError:
        pass


class Cache:







    def __init__(self, directory: Optional[str] = None,
                 safe_ttl: int = SAFE_TTL_SECONDS) -> None:
        self.directory = directory if directory is not None else default_dir()
        self.safe_ttl = safe_ttl

    def _root(self) -> Tuple[Optional[str], str]:
        return _image_root.ensure(self.directory)

    def _path(self, k: str) -> Optional[str]:




        root, _why = self._root()
        if root is None:
            return None
        return os.path.join(root, "{0}.json".format(k))

    def get(self, text: str, *, now: Optional[float] = None
            ) -> Tuple[Optional["model.Verdict"], str]:















        root, why = self._root()
        if root is None:
            return None, "unreadable (cache root: {0})".format(why)
        path = os.path.join(root, "{0}.json".format(key(text)))
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:









            try:
                st = os.lstat(path)
            except FileNotFoundError:
                return None, "miss"
            except OSError as exc:
                return None, "unreadable ({0})".format(type(exc).__name__)
            if stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0):
                return None, (
                    "unreadable (a symlink or reparse point was refused, "
                    "not followed)")
        try:
            fd = os.open(path, os.O_RDONLY | nofollow)
        except FileNotFoundError:
            return None, "miss"
        except OSError as exc:




            return None, "unreadable ({0})".format(type(exc).__name__)
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as exc:
            return None, "unreadable ({0})".format(type(exc).__name__)
        if not isinstance(raw, dict):
            return None, "unreadable (not a JSON object)"
        state = raw.get("state")
        axes = raw.get("axes")
        reason = raw.get("reason", "")
        written = raw.get("written")
        if (state not in _CACHEABLE_STATES or not isinstance(axes, list)
                or not all(isinstance(a, str) for a in axes)
                or any(a not in model.AXES for a in axes)
                or not isinstance(reason, str)
                or not isinstance(written, (int, float))
                or isinstance(written, bool)):













            return None, "unreadable (unexpected shape)"
        current = now if now is not None else time.time()
        if state == "safe" and (current - written) > self.safe_ttl:
            return None, "expired"
        return model.Verdict(state, list(axes), reason), "hit"

    def put(self, text: str, verdict: "model.Verdict", *,
            now: Optional[float] = None) -> str:








        if verdict.state not in _CACHEABLE_STATES:
            return "refused: {0!r} is never cached".format(verdict.state)
        root, why = self._root()
        if root is None:
            return "cache root: {0}".format(why)
        path = os.path.join(root, "{0}.json".format(key(text)))
        payload = {
            "state": verdict.state,
            "axes": list(verdict.axes),
            "reason": verdict.reason,
            "written": now if now is not None else time.time(),
        }
        directory, name = os.path.split(path)
        try:
            fd, tmp = tempfile.mkstemp(prefix="{0}.".format(name),
                                       suffix=".tmp", dir=directory)
        except OSError as exc:
            return "{0} could not be opened for writing ({1})".format(
                path, type(exc).__name__)
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
            except OSError:


                os.close(fd)
                raise




            os.replace(tmp, path)
        except OSError as exc:
            _discard(tmp)
            return "{0} could not be written ({1})".format(
                path, type(exc).__name__)
        return ""


def default_cache() -> Cache:




    return Cache()
