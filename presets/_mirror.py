"""Write-through mirror for `gh-issue` reads (#1955).

Every `gh-*` read op renders text and throws it away -- the next tick asking
the same question pays a fresh API call for an answer it already had. The
naive fix (a poller that refreshes on an interval) invents a freshness it
cannot have: a cached answer that was correct when taken and false a minute
later is indistinguishable from a fresh one at render time. A **write-through**
mirror has no such problem by construction -- it is only ever populated as a
side effect of a read the caller was already making, so a mirrored file's age
*is* the age of the read that produced it. There is no second clock to
disagree with.

**Scope of this module, as shipped**: `gh-issue` is the only read op wired to
write through it. `gh-pr`, `gh-issues` and `gh-prs` are not -- see the issue
comment on #1955 sizing the full lane at 4,500+ lines across four files, each
needing its own TDD/doc/fragment pass, which is more than one lane should
land at once. Extending the other three to populate the same mirror is
tracked separately. Because only one op writes through it, `not-cached` here
means "never read via `gh-issue`" -- it does NOT mean "does not exist on the
tracker". The manifest that would let a caller tell those apart (built from
the full issue list `gh-issues` already fetches) is exactly the piece that
extension would add; until then, a caller must not read `not-cached` as
`no-such-issue`.

**What this must never become**: an authority for anything a decision turns
on. Open/closed state, assignee, labels and linked-PR state stay live reads --
`claude-oss`'s own collision-avoidance (two agents claiming the same issue)
depends on the assignee field being live. This module stores exactly what
`gh-issue` rendered, untruncated, and nothing here re-derives or infers a
current value from it.

Opt-in via a top-level `"gh_mirror_dir"` key in `.supertool.json`. Unset
(the default) means the mirror is off and every call into this module is a
no-op for the writer, or answers `not-configured` for the reader.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, NamedTuple, Optional

CONFIG_KEY = "gh_mirror_dir"
CONFIG_FILENAME = ".supertool.json"

ISSUES_SUBDIR = "issues"
MANIFEST_NAME = "manifest.json"

CACHED = "cached"
NOT_CACHED = "not-cached"
UNREADABLE = "mirror-unreadable"


class MirrorConfig(NamedTuple):
    """The resolved mirror root, or why there isn't one.

    Three states, matching every other walk-up loader in this codebase
    (`presets/_remote_default.py`'s `config_default`, `presets/worktree/_common.py`'s
    `load_config`): `path` set means configured and trusted; both `None`
    means genuinely unset (nobody asked for a mirror, OR the nearest
    `.supertool.json` was untrusted -- see below); `path` `None` with
    `error` set means a `.supertool.json` was found, trusted, and STILL
    could not be used (malformed JSON, a `gh_mirror_dir` of the wrong
    type) -- which must render as `mirror-unreadable`, never silently as
    "not configured".

    **Untrusted ownership/permissions is deliberately the FIRST case, not
    the third**, matching every sibling walk-up loader
    (`presets/_publish_safety.py`, `presets/gitlab/_maintenance.py`,
    `presets/worktree/_common.py`, `presets/slack/_authorization.py`): a
    trust violation is skipped exactly like an absent file, with a stderr
    warning, and the walk continues upward for a further, trusted config --
    it never sets `.error`. Self-review finding (#1955): an earlier draft
    of this docstring claimed the opposite, which
    `tests/test_gh_mirror_1955.py::test_group_writable_config_is_skipped_like_an_absent_one`
    already disproved (it asserts `cfg.error is None`). The practical
    consequence, named rather than hidden: a `not configured` render from
    `gh-mirror` cannot currently distinguish "nobody set `gh_mirror_dir`"
    from "somebody did, but this process does not trust that file" -- the
    same ambiguity every sibling loader already accepts.
    """
    path: Optional[str] = None
    error: Optional[str] = None


def _config_trust_violation(candidate: Path) -> Optional[str]:
    """POSIX ownership/permission guard (mirrors `_supertool._load_config`'s
    #695 hardening; presets cannot import the core module, so -- like
    `presets/_publish_safety.py` #2366, `presets/gitlab/_maintenance.py`
    #2365, `presets/worktree/_common.py` #2370 and `presets/slack/_authorization.py`
    #2416 before it -- this is a sixth copy of the same check rather than a
    shared import).

    A group/world-writable `.supertool.json`, or one owned by a different
    local user, is exactly the file another local account could rewrite
    between the moment it was reviewed and the moment this module reads it
    to decide where to write mirrored issue bodies on disk.

    POSIX-only: `st_uid` and the write bits are meaningless on Windows, so
    this returns `None` (trusted) unconditionally there. Root is treated as
    trusted, matching every sibling copy.
    """
    if os.name != "posix":
        return None
    try:
        st = candidate.stat()
    except OSError as exc:
        return f"cannot stat: {exc}"
    caller_uid = os.getuid()
    if st.st_uid not in (caller_uid, 0) and caller_uid != 0:
        return (
            f"not owned by the current user (owner uid {st.st_uid}, "
            f"running as uid {caller_uid})"
        )
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"group/world-writable (mode {stat.S_IMODE(st.st_mode):o})"
    return None


def load_config(start: Path) -> MirrorConfig:
    """Walk up from START for the nearest `.supertool.json`'s `gh_mirror_dir`,
    stopping at the nearest `.git` ancestor and skipping a config this
    process does not own.

    The mirror path is resolved relative to the directory HOLDING the
    `.supertool.json` that named it, not relative to the caller's cwd, so
    the writer (`gh-issue`, invoked from wherever a maintainer loop happens
    to be standing) and the reader (the `gh-mirror` op) agree on the same
    absolute directory regardless of how deep either one was invoked from.
    """
    d = start
    while True:
        candidate = d / CONFIG_FILENAME
        if candidate.is_file():
            violation = _config_trust_violation(candidate)
            if violation is not None:
                sys.stderr.write(
                    f"WARNING: skipped {candidate} ({violation}) -- "
                    f"ignoring it for the gh mirror.\n"
                )
            else:
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    return MirrorConfig(error=f"{candidate}: {exc.__class__.__name__}: {exc}")
                if not isinstance(data, dict):
                    return MirrorConfig(error=f"{candidate}: top level is not a JSON object")
                raw = data.get(CONFIG_KEY)
                if raw is None:
                    return MirrorConfig()  # found the config; the key is simply absent -- not configured
                if not isinstance(raw, str) or not raw.strip():
                    return MirrorConfig(error=f"{candidate}: {CONFIG_KEY!r} must be a non-empty string")
                mirror_path = Path(raw.strip())
                if not mirror_path.is_absolute():
                    mirror_path = candidate.parent / mirror_path
                return MirrorConfig(path=str(mirror_path))
            # An untrusted candidate is skipped exactly like an absent one --
            # fall through to the .git/parent walk below, matching every
            # sibling implementation.
        if (d / ".git").exists():
            return MirrorConfig()
        parent = d.parent
        if parent == d:
            return MirrorConfig()
        d = parent


def _issues_dir(root: str) -> Path:
    return Path(root) / ISSUES_SUBDIR


def _body_path(root: str, number: str) -> Path:
    return _issues_dir(root) / f"{number}.json"


def _manifest_path(root: str) -> Path:
    return _issues_dir(root) / MANIFEST_NAME


def _age(iso: str) -> str:
    """ISO timestamp -> 'Nd'/'Nh'/'Nm' ago. '' on parse failure, 'now' on skew.

    Same shape as `presets/github/issues.py::_age` and
    `presets/github/pr.py::_relative_age` -- this codebase's convention is to
    keep each small age-formatter local rather than share one across preset
    files (see `presets/_publish_safety.py`'s docstring for the same
    reasoning applied to the trust check above).
    """
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


#: How long a write waits for the manifest lock before deciding it is stale
#: and breaking it, and how often it polls while waiting. Module-level so
#: tests can shrink both without a real multi-second sleep (#1955 self-review).
_LOCK_TIMEOUT = 10.0
_LOCK_POLL = 0.05


@contextlib.contextmanager
def _manifest_lock(root: str) -> Iterator[None]:
    """Exclusive lock spanning one manifest read-modify-write.

    Self-review finding (#1955, Explore spawn): without this, two
    concurrent `gh-issue` reads for two DIFFERENT issue numbers could each
    read the manifest before the other wrote it back, so whichever
    `os.replace` landed second silently dropped the other's newly-added key
    -- exactly the multi-agent scenario this module's own docstring names
    as a live concern ("two agents claiming the same issue"). `write_issue`
    holds this for its whole read-modify-write, not just the final
    `os.replace` (which is atomic on its own but does not make the READ
    that preceded it atomic with it).

    `os.open(..., O_CREAT | O_EXCL)` is the lock primitive because it is
    atomic on both POSIX and Windows, unlike `fcntl`/`msvcrt`, so this
    needs no platform branch. A lock held past `_LOCK_TIMEOUT` is treated
    as stale (the process that created it crashed without releasing it)
    and broken rather than hung on forever -- a write-through cache is not
    worth blocking a caller's read indefinitely over.
    """
    issues_dir = _issues_dir(root)
    issues_dir.mkdir(parents=True, exist_ok=True)
    lock_path = issues_dir / (MANIFEST_NAME + ".lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            time.sleep(_LOCK_POLL)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


def write_issue(root: str, number: str, payload: dict) -> Optional[str]:
    """Write PAYLOAD (the full, untruncated `gh issue view --json` reply)
    plus a read timestamp, and update the manifest. Never raises.

    Returns `None` on success, or an error string on failure -- a caller
    populating the mirror as a side effect of a read must not let a mirror
    write failure take the read down with it, so this never raises; the
    caller decides whether/how loudly to surface a non-`None` return.

    Body and manifest are each written to a `.tmp` sibling and swapped in
    with `os.replace`, so a crash mid-write leaves the previous version (or
    nothing) rather than a half-written file a later read would parse as
    corrupt.
    """
    try:
        issues_dir = _issues_dir(root)
        issues_dir.mkdir(parents=True, exist_ok=True)
        read_at = datetime.now(timezone.utc).isoformat()

        body_path = _body_path(root, number)
        tmp_body = body_path.with_name(body_path.name + ".tmp")
        tmp_body.write_text(
            json.dumps({"read_at": read_at, "issue": payload}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_body, body_path)

        # The manifest's read-modify-write is the part `os.replace` alone
        # does not make atomic: it makes the FINAL write atomic, not the
        # read that decided what to write. Locked for exactly that span.
        with _manifest_lock(root):
            manifest_path = _manifest_path(root)
            manifest: dict = {}
            if manifest_path.is_file():
                try:
                    parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        manifest = parsed
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    # A corrupted manifest on the way IN is replaced by this
                    # write rather than propagated -- the write is what repairs
                    # it. A reader hitting the same corruption before this runs
                    # gets `mirror-unreadable`, never a silent `not-cached`.
                    manifest = {}
            manifest[str(number)] = read_at
            tmp_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
            tmp_manifest.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            os.replace(tmp_manifest, manifest_path)
        return None
    except OSError as exc:
        return f"{exc.__class__.__name__}: {exc}"


class MirrorHit(NamedTuple):
    """One read op's answer for one issue number -- the three-state contract
    #1955 asks for. `state` is one of `CACHED` / `NOT_CACHED` / `UNREADABLE`,
    and the third must never render as either of the first two: a
    permission-denied directory or a corrupted manifest is a finding about
    the mirror itself, not evidence that the issue was never fetched.
    """
    state: str
    age: str = ""
    detail: str = ""


def read_issue(root: str, number: str) -> MirrorHit:
    """Look NUMBER up in ROOT's manifest and body file.

    Every OSError distinct from "the manifest/body file does not exist" is
    `UNREADABLE`, never folded into `NOT_CACHED` -- `Path.is_file()` swallows
    `PermissionError` and returns `False`, which is exactly the
    absence-produced-by-the-tool defect this module exists not to repeat, so
    the manifest and body are read directly (`read_text`) rather than
    stat-checked first.
    """
    manifest_path = _manifest_path(root)
    try:
        raw_manifest = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MirrorHit(NOT_CACHED)
    except OSError as exc:
        return MirrorHit(UNREADABLE, detail=f"{manifest_path}: {exc.__class__.__name__}: {exc}")
    except UnicodeDecodeError as exc:
        return MirrorHit(UNREADABLE, detail=f"{manifest_path}: {exc.__class__.__name__}: {exc}")

    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        return MirrorHit(UNREADABLE, detail=f"{manifest_path}: corrupted JSON ({exc})")
    if not isinstance(manifest, dict):
        return MirrorHit(UNREADABLE, detail=f"{manifest_path}: top level is not a JSON object")

    read_at = manifest.get(str(number))
    if read_at is None:
        return MirrorHit(NOT_CACHED)
    if not isinstance(read_at, str) or not read_at:
        return MirrorHit(UNREADABLE, detail=f"{manifest_path}: entry for #{number} is not a timestamp string")
    if _age(read_at) == "":
        # `_age` returns "" for both an empty input (already excluded above)
        # and a non-empty string that does not parse as an ISO timestamp --
        # a hand-corrupted or truncated manifest value. Self-review finding
        # (#1955): this used to be checked only for emptiness, so a
        # corrupted-but-non-blank value passed straight through to CACHED
        # with a blank age once the body file parsed clean -- the exact
        # inversion this module exists to prevent.
        return MirrorHit(UNREADABLE, detail=f"{manifest_path}: entry for #{number} is not a parseable timestamp ({read_at!r})")

    body_path = _body_path(root, number)
    try:
        raw_body = body_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MirrorHit(UNREADABLE, detail=f"manifest lists #{number} but {body_path} does not exist")
    except OSError as exc:
        return MirrorHit(UNREADABLE, detail=f"{body_path}: {exc.__class__.__name__}: {exc}")
    except UnicodeDecodeError as exc:
        return MirrorHit(UNREADABLE, detail=f"{body_path}: {exc.__class__.__name__}: {exc}")

    try:
        json.loads(raw_body)
    except json.JSONDecodeError as exc:
        return MirrorHit(UNREADABLE, detail=f"{body_path}: corrupted JSON ({exc})")

    return MirrorHit(CACHED, age=_age(read_at))
