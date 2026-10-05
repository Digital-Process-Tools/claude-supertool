























































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




PRS_SUBDIR = "prs"
MANIFEST_NAME = "manifest.json"

CACHED = "cached"
NOT_CACHED = "not-cached"
UNREADABLE = "mirror-unreadable"


class MirrorConfig(NamedTuple):



























    path: Optional[str] = None
    error: Optional[str] = None


def _config_trust_violation(candidate: Path) -> Optional[str]:
















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
                    return MirrorConfig()  
                if not isinstance(raw, str) or not raw.strip():
                    return MirrorConfig(error=f"{candidate}: {CONFIG_KEY!r} must be a non-empty string")
                mirror_path = Path(raw.strip())
                if not mirror_path.is_absolute():
                    mirror_path = candidate.parent / mirror_path
                return MirrorConfig(path=str(mirror_path))



        if (d / ".git").exists():
            return MirrorConfig()
        parent = d.parent
        if parent == d:
            return MirrorConfig()
        d = parent


def _kind_dir(root: str, subdir: str) -> Path:
    return Path(root) / subdir


def _body_path(root: str, subdir: str, number: str) -> Path:
    return _kind_dir(root, subdir) / f"{number}.json"


def _manifest_path(root: str, subdir: str) -> Path:
    return _kind_dir(root, subdir) / MANIFEST_NAME


def _age(iso: str) -> str:








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





_LOCK_TIMEOUT = 10.0
_LOCK_POLL = 0.05


class _LockTimeout(Exception):
    pass


@contextlib.contextmanager
def _manifest_lock(root: str, subdir: str) -> Iterator[None]:

































    kind_dir = _kind_dir(root, subdir)
    kind_dir.mkdir(parents=True, exist_ok=True)
    lock_path = kind_dir / (MANIFEST_NAME + ".lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT
    fd = None


    last_refusal = ""
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError) as exc:












            last_refusal = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                raise _LockTimeout(
                    f"could not acquire {lock_path} within {_LOCK_TIMEOUT}s "
                    f"-- last refusal was {last_refusal}. If a process holding "
                    f"it has crashed, delete it by hand; a permission error "
                    f"with no lock file beside it is a different fault and "
                    f"deleting nothing will fix it"
                ) from None
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







    return _write(root, ISSUES_SUBDIR, "issue", number, payload)


def write_pr(root: str, number: str, payload: dict) -> Optional[str]:




    return _write(root, PRS_SUBDIR, "pr", number, payload)


def _write(root: str, subdir: str, key: str, number: str, payload: dict) -> Optional[str]:














    try:
        kind_dir = _kind_dir(root, subdir)
        kind_dir.mkdir(parents=True, exist_ok=True)
        read_at = datetime.now(timezone.utc).isoformat()

        body_path = _body_path(root, subdir, number)
        tmp_body = body_path.with_name(body_path.name + ".tmp")
        tmp_body.write_text(
            json.dumps({"read_at": read_at, key: payload}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_body, body_path)




        with _manifest_lock(root, subdir):
            manifest_path = _manifest_path(root, subdir)
            manifest: dict = {}
            if manifest_path.is_file():
                try:
                    parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        manifest = parsed
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):




                    manifest = {}
            manifest[str(number)] = read_at
            tmp_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
            tmp_manifest.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            os.replace(tmp_manifest, manifest_path)
        return None
    except _LockTimeout as exc:
        return str(exc)
    except OSError as exc:
        return f"{exc.__class__.__name__}: {exc}"


class MirrorHit(NamedTuple):






    state: str
    age: str = ""
    detail: str = ""


def read_issue(root: str, number: str) -> MirrorHit:




    return _read(root, ISSUES_SUBDIR, number)


def read_pr(root: str, number: str) -> MirrorHit:

    return _read(root, PRS_SUBDIR, number)


def _read(root: str, subdir: str, number: str) -> MirrorHit:









    manifest_path = _manifest_path(root, subdir)
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







        return MirrorHit(UNREADABLE, detail=f"{manifest_path}: entry for #{number} is not a parseable timestamp ({read_at!r})")

    body_path = _body_path(root, subdir, number)
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
