"""A verdict cache for classify's model stage (#2054).

`classify` had no memory at all. `check.py` is a fresh subprocess per call
(see the op's `cmd` in `presets/classify.json`), so an in-process
`lru_cache` on `model.classify` would buy nothing across calls -- it dies
with the process that would have populated it. #2049 wired `classify` into
`gh-issue`/`gh-pr`/`gl-issue`/`gl-mr` and the Slack watch source, and both
re-read the same text repeatedly: a tracker read path re-reads the same
issue body and the same comment every tick, and a poller sees the same
pinned message and the same repeated bot notice. Every repeat used to be a
full `claude -p` spawn -- this module is what makes a repeat free.

**File-backed, not in-process, and that is the whole point above.** One
small JSON file per cache key, under a directory this process owns --
`Cache.directory` (default: `default_dir()`, a per-uid leaf under
`tempfile.gettempdir()`, the same "public leaf in a world-writable
directory" shape `presets/_image_root.py` was written for). `_image_root.
ensure()` is reused rather than re-implemented: it creates the leaf
non-recursively and proves the result is a real directory this uid owns,
not a symlink or a squatted directory somebody else planted first
(`presets/_image_root.py`'s own docstring is the long version of that
argument -- one severity down here, since these entries are content-hashed
rather than public and committed).

**Three constraints from the issue, each enforced here rather than merely
documented:**

1. **Keyed on the text, never the prompt.** `_untrusted.fence()` draws a
   fresh nonce every process, and `check.py` is a fresh process per call --
   keying on the fenced prompt would produce a different key on every
   invocation for identical input and never hit. `key()` below hashes the
   bare text plus a version string, nothing else.
2. **`could-not-classify` is never written.** `put()` refuses it outright
   and returns why, rather than silently no-op'ing -- a caller that expects
   its cache write to have landed and did not needs to be told, even though
   nothing today reads that return value for anything but tests.
3. **The key carries a version derived from live constants, not a
   hand-bumped integer.** `version()` hashes `model.AXES` and
   `model._SYSTEM_PROMPT` together -- whichever changes, every previously
   cached verdict keys to a value nobody will look up again. That is
   invalidation without a migration step and without anyone having to
   remember to bump anything when the axis list or the prompt changes.

**`safe` gets a TTL; `suspect` does not, by choice.** The issue explicitly
leaves this open. The argument for the asymmetry: `safe` is the verdict a
caller acts on permissively, so letting a stale `safe` for text nobody has
re-examined in weeks quietly keep waving things through is the wrong
direction to be wrong in. `suspect` is what a caller treats with more
scrutiny already; re-affirming it forever costs nothing a caller was not
already paying attention to, and the model spawn saved by never
re-checking is the same spawn `Budget` (`presets/_classify_render.py`)
exists to ration. `SAFE_TTL_SECONDS` is a plain constant, not read from the
environment -- this module owns none of the "extra config key" plumbing
`docs/contributing.md` documents, and adding a knob nobody asked for is not
this issue's job.

**A symlink or reparse point planted at an entry's own name is refused, not
followed.** `os.open(..., os.O_NOFOLLOW)` is the guard on POSIX; on Windows,
where `O_NOFOLLOW` does not exist, `get()` falls back to an `os.lstat` check
first (`stat.S_ISLNK` / `st_reparse_tag`), the same fallback
`presets/_image_root.py::_verify_by_lstat` uses for the root directory
itself -- REASONED from CPython's documented behaviour, not observed on a
Windows host, the same grade that module records for its own Windows
branch. Without either check, a co-tenant able to write inside the
already-verified cache root (a different local uid squatting the leaf is
refused by `_image_root.ensure`, but nothing stops another process running
as the *same* uid) could plant a symlink at a predictable content-hash name
and have `get()` hand back an attacker-chosen `safe`/`suspect` verdict for
arbitrary text -- the boundary this repo's own untrusted-input convention
exists to hold.

**Never renders `could-not-classify` and never conflates "no entry" with "an
entry exists and could not be read".** `get()` returns three states for
"nothing usable was found" -- `"miss"` (no file), `"expired"` (a `safe`
entry outlived its TTL), and `"unreadable (...)"` (a symlink was refused, a
permission error, corrupt JSON, or a shape this cache does not recognise,
including one somehow naming `could-not-classify`) -- because a corrupt
cache and an honest cold start are different facts and a caller deciding
whether to complain needs the difference, even though both currently fall
through to the same "run the spawn" behaviour in `model.classify`.

**What this does not do.** No cleanup, no size cap, no background sweep --
entries are small (well under 1KB) JSON files and this leans on the
operating system's own temp-directory housekeeping the same way
`_image_root`'s attachment root already does. No cross-process locking:
two processes racing to write the same key write the same content (the
verdict for identical text under one version, barring genuine model
nondeterminism) through `tempfile.mkstemp` plus `os.replace`, which is
atomic on POSIX and on Windows since Python 3.3 -- the loser's write simply
replaces the winner's with equivalent content, not a torn file. No
operator-facing on/off switch: this cache runs whenever a caller passes one
in, and the only caller in this lane that does is `check.py`'s `main()` --
see that module for why `run()` itself defaults to no cache at all.
"""
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

sys.path.insert(0, str(Path(__file__).resolve().parent))       # for model (sibling)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for _image_root
import model  # noqa: E402
import _image_root  # noqa: E402

#: How long a cached `safe` verdict is trusted before a fresh spawn is owed.
#: See the module docstring's "safe gets a TTL" paragraph for the asymmetry
#: with `suspect`, which never expires.
SAFE_TTL_SECONDS = 24 * 60 * 60

#: The only two states `put()` will ever write. `could-not-classify` is the
#: transient bucket (#2054's own constraint 2) and is refused unconditionally.
_CACHEABLE_STATES = ("safe", "suspect")

_ROOT_NAME = "supertool-classify-cache"


def _euid() -> Optional[int]:
    """Our effective uid, or `None` where the question cannot be asked
    (Windows). Duplicated in miniature from `_image_root._euid` rather than
    importing a private symbol across a module boundary this file does not
    own -- three lines, and the two copies cannot drift apart from each
    other in any way that matters."""
    geteuid = getattr(os, "geteuid", None)
    return None if geteuid is None else geteuid()


def default_dir() -> str:
    """The default cache root: a per-uid leaf under the platform temp
    directory, the same shape `_image_root.default_root()` uses for the
    attachment root -- public name, world-writable parent, made safe by
    `_image_root.ensure()` rather than by the name being secret."""
    uid = _euid()
    name = _ROOT_NAME if uid is None else "{0}-{1}".format(_ROOT_NAME, uid)
    return os.path.join(tempfile.gettempdir(), name)


def version() -> str:
    """A short digest of what a cached verdict currently means.

    `model.AXES` and `model._SYSTEM_PROMPT` together define the question a
    verdict is an answer to (#2054's constraint 3). Hashing them, rather
    than hand-bumping a version integer whenever either changes, means a
    contributor who edits the system prompt or renames an axis does not
    have to remember this file exists for old entries to stop being read
    back as new ones -- they simply key differently from this point on.
    """
    blob = ("\x1f".join(model.AXES) + "\x1e" + model._SYSTEM_PROMPT).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def key(text: str, version_: Optional[str] = None) -> str:
    """The cache file's stem for `text` under `version_` (default: the
    current live `version()`).

    Hashes the bare text, never a fenced prompt -- see the module
    docstring's constraint 1. `version_` is accepted as a parameter (rather
    than always recomputed) so a caller comparing two versions does not pay
    for recomputing the live one twice.
    """
    v = version_ if version_ is not None else version()
    blob = (v + "\x00" + text).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _discard(tmp: str) -> None:
    """Remove a temporary file this process created. Silent on failure --
    same convention as `presets/watch/transport.py::_discard`: only ever
    called on a path `mkstemp` returned under `O_EXCL`, so there is no
    chance this removes something somebody else planted."""
    try:
        os.unlink(tmp)
    except OSError:
        pass


class Cache:
    """One verdict cache, rooted at `directory` (default: `default_dir()`).

    Every method is total: none of them raise for an ordinary filesystem
    failure. A cache that cannot be read or written degrades to "nothing
    cached", never to a crash and never to a fabricated verdict.
    """

    def __init__(self, directory: Optional[str] = None,
                 safe_ttl: int = SAFE_TTL_SECONDS) -> None:
        self.directory = directory if directory is not None else default_dir()
        self.safe_ttl = safe_ttl

    def _root(self) -> Tuple[Optional[str], str]:
        return _image_root.ensure(self.directory)

    def _path(self, k: str) -> Optional[str]:
        """The file `k` lives at, establishing the root first. `None` when
        the root itself could not be established -- a caller wanting the
        reason should call `_root()` directly; this is the convenience form
        `get`/`put` use once they have already decided they need a path."""
        root, _why = self._root()
        if root is None:
            return None
        return os.path.join(root, "{0}.json".format(k))

    def get(self, text: str, *, now: Optional[float] = None
            ) -> Tuple[Optional["model.Verdict"], str]:
        """`(verdict, "hit")`, or `(None, status)` for everything else.

        `status` is one of `"miss"` (no entry), `"expired"` (a `safe` entry
        past its TTL) or `"unreadable (...)"` (a symlink refused, a
        permission error, corrupt JSON, or a shape this cache does not
        recognise). The three are kept apart rather than all collapsing to
        `None` because a corrupt cache and an honest cold start are
        different facts about the world, even though every caller in this
        lane currently reacts to all three the same way (fall through and
        spawn) -- see the module docstring.

        Never returns a `could-not-classify` verdict: that state is never
        written (see `put`), so any entry this method can parse is `safe`
        or `suspect`.
        """
        root, why = self._root()
        if root is None:
            return None, "unreadable (cache root: {0})".format(why)
        path = os.path.join(root, "{0}.json".format(key(text)))
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            # Windows: `O_NOFOLLOW` does not exist, so `os.open` below would
            # follow a symlink or reparse point planted at this name with no
            # refusal at all -- `_image_root._verify_by_lstat` is the
            # precedent for the fallback: `lstat` does not follow the final
            # component, so a link is visible here even where the open-time
            # flag cannot see it. REASONED from CPython's documented
            # `st_reparse_tag`/`stat.S_ISLNK` behaviour, not observed on a
            # Windows host -- the same grade `_image_root.py` records for
            # its own Windows branch.
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
            # ELOOP/EMLINK (a symlink was planted at this name and refused)
            # or any other open failure -- both are "could not read", not
            # "no entry" (`presets/watch/transport.py::read_state_checked`
            # is the precedent for keeping these apart).
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
                or not isinstance(reason, str)
                or not isinstance(written, (int, float))
                or isinstance(written, bool)):
            # Covers a hand-edited or corrupted file, and a defensive floor
            # against a `could-not-classify` entry that should never have
            # existed in the first place (`put` refuses to write one, but a
            # `get` that trusted the file blindly would still hand one back
            # if something else put it there).
            return None, "unreadable (unexpected shape)"
        current = now if now is not None else time.time()
        if state == "safe" and (current - written) > self.safe_ttl:
            return None, "expired"
        return model.Verdict(state, list(axes), reason), "hit"

    def put(self, text: str, verdict: "model.Verdict", *,
            now: Optional[float] = None) -> str:
        """`""` once the entry has landed, else why not.

        Refuses unconditionally when `verdict.state` is not `safe` or
        `suspect` -- `could-not-classify` is never written, the transient
        bucket must not calcify into a permanent verdict for that text
        (#2054's constraint 2). This is the enforcement; the docstring is
        the argument.
        """
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
                # `os.fdopen` does not take the descriptor on its failing
                # arm -- same hazard `write_json_contained` guards against.
                os.close(fd)
                raise
            # `os.replace` on a destination that is a symlink removes the
            # link itself rather than following it (POSIX `rename(2)`), so
            # no `O_NOFOLLOW` is needed on this side -- same reasoning
            # `presets/watch/transport.py::write_json_contained` documents.
            os.replace(tmp, path)
        except OSError as exc:
            _discard(tmp)
            return "{0} could not be written ({1})".format(
                path, type(exc).__name__)
        return ""


def default_cache() -> Cache:
    """The cache a real caller reaches for -- rooted at `default_dir()`,
    the ordinary TTL. Tests build `Cache(directory=...)` directly instead,
    against a `tmp_path`, so nothing in the suite touches the real
    filesystem's temp directory or the real clock."""
    return Cache()
