#!/usr/bin/env python3























from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional

_GIT_TIMEOUT = 15





CONFIG_FILENAME = ".supertool.json"








MANIFEST_REL = "worktree-setup/manifest.json"






EXCLUDE_REL = "worktree-setup/exclude"


class TargetError(Exception):
    pass


def fingerprint_copy(path: Path) -> Optional[str]:




























    try:
        if path.is_symlink() or path.is_file():
            st = path.stat()
            return f"{st.st_size}:{st.st_mtime_ns}"
        if not path.is_dir():
            return None
        entries = []
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                fp = Path(root) / name
                try:
                    st = fp.stat()
                except OSError:
                    return None
                rel = fp.relative_to(path).as_posix()
                entries.append(f"{rel}:{st.st_size}:{st.st_mtime_ns}")
        entries.sort()
        blob = "\n".join(entries).encode("utf-8", "surrogateescape")
        return hashlib.sha256(blob).hexdigest()
    except OSError:
        return None


def _run_git(args: list, cwd: Path, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:








    cmd = ["git", "-C", str(cwd)] + args
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=-1, stdout="",
            stderr=f"timed out after {timeout}s",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=cmd, returncode=-1, stdout="",
            stderr=f"{exc.__class__.__name__}: {exc}",
        )


def resolve_target(path_arg: Optional[str]) -> Path:





    target = Path(path_arg).expanduser().resolve() if path_arg else Path.cwd().resolve()
    if not target.is_dir():
        raise TargetError(f"not a directory: {target}")
    result = _run_git(["rev-parse", "--is-inside-work-tree"], target)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "git did not answer"
        raise TargetError(f"{target} is not inside a git repository ({stderr})")
    if result.stdout.strip() != "true":
        raise TargetError(f"{target} is not inside a git working tree")
    return target


def common_dir(target: Path) -> Optional[Path]:












    result = _run_git(["rev-parse", "--git-common-dir"], target)
    if result.returncode != 0:
        return None
    p = Path(result.stdout.strip())
    if not p.is_absolute():
        p = target / p
    try:
        return p.resolve()
    except OSError:
        return None


def resolve_primary(target: Path) -> Path:







    result = _run_git(["worktree", "list", "--porcelain"], target)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "git did not answer"
        raise TargetError(f"could not list worktrees from {target}: {stderr}")
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):]).resolve()
    raise TargetError(f"`git worktree list` returned no worktree at all from {target}")


def git_path(target: Path, rel: str) -> Path:













    result = _run_git(["rev-parse", "--git-path", rel], target)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "git did not answer"
        raise TargetError(f"could not resolve git path {rel!r} for {target}: {stderr}")
    resolved = Path(result.stdout.strip())
    if not resolved.is_absolute():
        resolved = target / resolved
    return resolved


class ConfigResult:












    def __init__(self, config: Optional[dict], error: Optional[str] = None):
        self.config = config
        self.error = error

    @property
    def configured(self) -> bool:
        return self.error is None and self.config is not None


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


def load_config(target: Path) -> ConfigResult:


























    d = target
    while True:
        candidate = d / CONFIG_FILENAME
        if candidate.is_file():
            violation = _config_trust_violation(candidate)
            if violation is not None:
                sys.stderr.write(
                    f"WARNING: skipped {candidate} ({violation}) -- "
                    f"ignoring it for worktree.setup config.\n"
                )
            else:
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    return ConfigResult(None, f"{candidate}: {exc}")
                if not isinstance(data, dict):
                    return ConfigResult(None, f"{candidate}: top level is not a JSON object")
                ops_section = data.get("ops")
                worktree_section = ops_section.get("worktree") if isinstance(ops_section, dict) else None
                setup_section = worktree_section.get("setup") if isinstance(worktree_section, dict) else None
                if not isinstance(setup_section, dict):
                    return ConfigResult(None)
                return ConfigResult(setup_section)
        if (d / ".git").exists():
            return ConfigResult(None)
        parent = d.parent
        if parent == d:
            return ConfigResult(None)
        d = parent


def str_list(cfg: dict, key: str) -> tuple:




    raw = cfg.get(key)
    if raw is None:
        return (), None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return (), f"ops.worktree.setup.{key} must be a list of strings — ignoring it"
    return tuple(raw), None


def validate_entry(entry: str, path_cls: type = Path) -> Optional[str]:












































    if not isinstance(entry, str) or not entry:
        return "not a non-empty string"
    if "\n" in entry or "\r" in entry:
        return "contains a newline or carriage return"
    p = path_cls(entry)
    if p.is_absolute() or p.root or p.drive:
        return "must be a relative path, not absolute"
    if ".." in p.parts:
        return "must not contain '..'"
    return None


def safe_join(root: Path, entry: str) -> "tuple[Optional[Path], Optional[str]]":







    reason = validate_entry(entry)
    if reason:
        return None, reason
    candidate = root / entry













    try:
        resolved_root = root.resolve()
        resolved_parent = candidate.parent.resolve()
    except OSError as exc:
        return None, f"could not resolve ({exc})"
    resolved_candidate = resolved_parent / candidate.name
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return None, f"resolves outside {resolved_root}"
    return candidate, None


def read_manifest(target: Path) -> ConfigResult:




































    empty = {"linked": [], "copied": [], "excluded": [], "copy_fingerprints": {}}
    result = _run_git(["rev-parse", "--git-path", MANIFEST_REL], target)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "git did not answer"
        return ConfigResult(
            None,
            f"could not resolve the provisioning manifest path for {target}: {stderr}",
        )
    resolved = Path(result.stdout.strip())
    path = resolved if resolved.is_absolute() else target / resolved
    if not path.is_file():
        return ConfigResult(empty)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ConfigResult(None, f"{path}: {exc}")
    if not isinstance(data, dict):
        return ConfigResult(None, f"{path}: top level is not a JSON object")
    manifest = dict(empty)
    manifest["copy_fingerprints"] = {}
    for key in ("linked", "copied", "excluded"):
        raw = data.get(key)
        if isinstance(raw, list):
            manifest[key] = [str(p) for p in raw]
    raw_fp = data.get("copy_fingerprints")
    if isinstance(raw_fp, dict):
        manifest["copy_fingerprints"] = {
            str(k): (str(v) if v is not None else None) for k, v in raw_fp.items()
        }
    return ConfigResult(manifest)


def write_manifest(target: Path, manifest: dict) -> Optional[str]:










    try:
        path = git_path(target, MANIFEST_REL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (TargetError, OSError) as exc:
        return str(exc)
    return None
