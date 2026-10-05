
















from __future__ import annotations

import functools
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Optional


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


def _supertool_config() -> dict:


































    global _CACHED_CONFIG
    try:
        return _CACHED_CONFIG  
    except NameError:
        pass
    cfg: dict = {}
    d = Path.cwd().resolve()
    while True:
        candidate = d / ".supertool.json"
        if candidate.is_file():
            violation = _config_trust_violation(candidate)
            if violation is not None:
















                sys.stderr.write(
                    f"WARNING: skipped {candidate} ({violation}) -- "
                    f"ignoring it for publish safety.\n"
                )
            else:
                try:
                    parsed = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        cfg = parsed
                    else:
                        sys.stderr.write(
                            f"WARNING: {candidate} does not hold a JSON object "
                            f"(got {type(parsed).__name__}) -- ignoring it. "
                            f"Every publish safety setting (disclosure, body "
                            f"allowlist, confirmation) falls back to its "
                            f"default, exactly as if this file set nothing at "
                            f"all.\n"
                        )
                except (OSError, json.JSONDecodeError,
                        UnicodeDecodeError) as exc:










                    sys.stderr.write(
                        f"WARNING: could not read {candidate} "
                        f"({exc.__class__.__name__}: {exc}) -- ignoring it. "
                        f"Every publish safety setting (disclosure, body "
                        f"allowlist, confirmation) falls back to its "
                        f"default, exactly as if this file set nothing at "
                        f"all.\n"
                    )




                break
        if (d / ".git").exists():
            break
        if d.parent == d:
            break
        d = d.parent
    _CACHED_CONFIG = cfg  
    return cfg




_DEFAULT_BODY_ALLOWLIST: tuple[str, ...] = (
    ".max/",
    "drafts/",
    "posts/",
    "blog/",
)


def _body_allowlist() -> tuple[Path, ...]:








    paths = list(_DEFAULT_BODY_ALLOWLIST)
    extra = os.environ.get("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", "")
    if extra:


        paths.extend(p for p in extra.split(os.pathsep) if p)
    cfg_extra = _supertool_config().get("publish_body_allowlist")
    if isinstance(cfg_extra, list):
        paths.extend(str(p) for p in cfg_extra if isinstance(p, str))
    cwd = Path.cwd().resolve()
    out: list[Path] = []
    for p in paths:
        try:
            out.append((cwd / p).resolve())
        except OSError:
            continue
    return tuple(out)


def safe_resolve_body_path(arg: str) -> Path:





    raw_path = arg[len("file://"):] if arg.startswith("file://") else arg
    try:
        resolved = Path(raw_path).resolve()
    except OSError:
        sys.stderr.write(f"ERROR: cannot resolve body path: {raw_path!r}\n")
        sys.exit(2)
    for allowed in _body_allowlist():
        try:
            resolved.relative_to(allowed)
            return resolved
        except ValueError:
            continue
    rel = ", ".join(p.name + "/" for p in _body_allowlist()[:4])
    sys.stderr.write(
        f"ERROR: publish body path escapes the safety allowlist: {raw_path!r}\n"
        f"  resolved to: {resolved}\n"
        f"  allowed dirs (relative to cwd): {rel}\n"
        f"  Extend (additive): SUPERTOOL_PUBLISH_BODY_ALLOWLIST=path1{os.pathsep}path2\n"
        f"    or `\"publish_body_allowlist\": [\"path1\"]` in .supertool.json\n"
    )
    sys.exit(2)




def require_confirm(action: str, preview: str, *, force: bool = False) -> None:







    if force:
        return
    if os.environ.get("SUPERTOOL_NO_PUBLISH_CONFIRM") == "1":
        return
    if bool(_supertool_config().get("no_publish_confirm")):
        return
    head = preview if len(preview) <= 200 else preview[:197] + "..."
    sys.stderr.write(
        f"ERROR: {action} requires explicit confirmation.\n"
        f"  Preview: {head!r}\n"
        f"  To proceed: append |force, define SUPERTOOL_NO_PUBLISH_CONFIRM=1 as an environment variable,\n"
        f"  or add `\"no_publish_confirm\": true` to .supertool.json.\n"
    )
    sys.exit(2)




_DEFAULT_DISCLOSURE_TEXT = "[AI-generated]"


def _disclosure_config() -> tuple[bool, str]:










    if os.environ.get("SUPERTOOL_NO_PUBLISH_DISCLOSURE") == "1":
        return False, ""
    cfg = _supertool_config()
    if bool(cfg.get("no_publish_disclosure")):
        return False, ""
    text = cfg.get("publish_disclosure_text")
    if isinstance(text, str) and text.strip():
        candidate = text.strip()
        if candidate.isascii():
            return True, candidate
        sys.stderr.write(
            f"WARNING: publish_disclosure_text {candidate!r} is not ASCII -- "
            "a console reading a non-UTF-8 codepage can crash on it after "
            "the publish already happened (#2066). Falling back to the "
            f"default marker ({_DEFAULT_DISCLOSURE_TEXT!r}).\n"
        )
    return True, _DEFAULT_DISCLOSURE_TEXT


def apply_forge_disclosure(body: str) -> tuple[str, str]:


























    enabled, marker = _disclosure_config()
    if not enabled:
        return body, "suppressed"
    if marker in body:
        return body, "already-present"
    separator = "\n\n"
    return body + separator + marker, "appended"


def apply_disclosure(body: str, *, max_len: Optional[int] = None) -> tuple[str, str]:



















    enabled, marker = _disclosure_config()
    if not enabled:
        return body, "suppressed"
    separator = "\n\n"
    tagged = body + separator + marker
    if max_len is not None and len(tagged) > max_len:
        return body, "dropped"
    return tagged, "appended"




def _probe_dir_for(path) -> str:









    candidate = os.path.dirname(os.fspath(path)) or "."
    if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
        return candidate
    sys.stderr.write(
        f"WARNING: cannot probe mode-bit enforcement in {candidate!r} "
        "(missing or not writable) -- falling back to the system temp "
        "directory, which may disagree with the credential's own "
        "filesystem.\n"
    )
    return tempfile.gettempdir()


@functools.lru_cache(maxsize=None)
def _mode_bits_are_enforced(probe_dir=None) -> bool:



















    try:
        fd, probe = tempfile.mkstemp(dir=probe_dir)
    except OSError:
        return False
    os.close(fd)
    try:
        os.chmod(probe, 0o600)
        return stat.S_IMODE(os.stat(probe).st_mode) == 0o600
    except OSError:
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def check_token_file_mode(path: Path) -> None:























    try:
        st = os.stat(path)
    except OSError:
        return
    mode = stat.S_IMODE(st.st_mode)
    if not mode & 0o077:
        return
    if not _mode_bits_are_enforced(_probe_dir_for(path)):
        sys.stderr.write(
            f"WARNING: could not verify the permissions on {path} -- this "
            f"filesystem reports {oct(mode)} for every file and does not "
            "enforce POSIX mode bits, so whether other users can read this "
            "credential is UNKNOWN rather than fine. On Windows it is the "
            "user profile's ACL that protects it, which this tool cannot "
            "read.\n"
        )
        return
    sys.stderr.write(
        f"ERROR: token file {path} has loose permissions ({oct(mode)}).\n"
        f"  Tighten with: chmod 600 {path}\n"
        f"  (Other users on this machine can currently read your token.)\n"
    )
    sys.exit(2)
