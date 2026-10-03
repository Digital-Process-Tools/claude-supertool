#!/usr/bin/env python3


















































from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _common  


DOC_POINTER = "declare ops.worktree.setup (link/copy/exclude) in .supertool.json — see docs/presets/worktree.md"


def _same_target(link_path: Path, expected: Path) -> bool:

    if not link_path.is_symlink():
        return False
    try:
        return link_path.resolve() == expected.resolve()
    except OSError:
        return False


def _validate(entries: tuple, root_a: Path, root_b: Path, lines: list) -> "tuple[tuple, dict]":




    valid = []
    joined = {}
    for entry in entries:
        source, source_reason = _common.safe_join(root_a, entry)
        dest, dest_reason = _common.safe_join(root_b, entry)
        reason = source_reason or dest_reason
        if reason:
            lines.append(f"  WARNING refusing {entry!r} — {reason}")
            continue
        valid.append(entry)
        joined[entry] = (source, dest)
    return tuple(valid), joined


def _do_link(entry: str, source: Path, dest: Path, lines: list, manifest: dict) -> None:
    if dest.exists() or dest.is_symlink():
        if _same_target(dest, source):
            lines.append(f"  already linked: {entry}")
            if entry not in manifest["linked"]:
                manifest["linked"].append(entry)
            return
        if dest.is_symlink():
            lines.append(f"  WARNING skipped (exists as a symlink to something else): {entry}")
        else:
            lines.append(f"  WARNING skipped (real content already there, won't replace with a symlink): {entry}")
        return
    if not source.exists():
        lines.append(f"  WARNING source missing, skipped: {entry} (expected at {source})")
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(source, dest, target_is_directory=source.is_dir())
    except OSError as exc:
        lines.append(f"  WARNING could not create symlink, skipped: {entry} ({exc})")
        return
    manifest["linked"].append(entry)
    lines.append(f"  linked: {entry}")


def _do_copy(entry: str, source: Path, dest: Path, lines: list, manifest: dict) -> None:
    if dest.exists() or dest.is_symlink():
        lines.append(f"  already present, skipped: {entry}")
        return
    if not source.exists():
        lines.append(f"  WARNING source missing, skipped: {entry} (expected at {source})")
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, symlinks=False)
        else:
            shutil.copy2(source, dest, follow_symlinks=True)
    except OSError as exc:
        lines.append(f"  WARNING could not copy, skipped: {entry} ({exc})")
        return
    manifest["copied"].append(entry)








    manifest["copy_fingerprints"][entry] = _common.fingerprint_copy(dest)
    lines.append(f"  copied: {entry}")


def _do_exclude(target: Path, exclude_entries: tuple, lines: list, manifest: dict) -> None:
    if not exclude_entries:
        return
    result = _common._run_git(["config", "--get", "extensions.worktreeConfig"], target)
    if result.stdout.strip() != "true":
        enable = _common._run_git(["config", "extensions.worktreeConfig", "true"], target)
        if enable.returncode != 0:
            lines.append(f"  WARNING could not enable extensions.worktreeConfig, exclude entries skipped: {enable.stderr.strip()}")
            return

    try:
        exclude_file = _common.git_path(target, _common.EXCLUDE_REL)
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        if exclude_file.is_file():
            existing = {line.rstrip("\n") for line in exclude_file.read_text(encoding="utf-8").splitlines()}
        new_lines = [e for e in exclude_entries if e not in existing]
        if new_lines:
            with exclude_file.open("a", encoding="utf-8") as fh:
                for e in new_lines:
                    fh.write(e + "\n")
    except (_common.TargetError, OSError) as exc:
        lines.append(f"  WARNING could not write the worktree-private exclude file, exclude entries skipped: {exc}")
        return

    for e in exclude_entries:
        if e in existing:
            lines.append(f"  already excluded: {e}")
        else:
            lines.append(f"  excluded (worktree-private): {e}")
        if e not in manifest["excluded"]:
            manifest["excluded"].append(e)

    set_result = _common._run_git(
        ["config", "--worktree", "core.excludesFile", str(exclude_file)], target)
    if set_result.returncode != 0:
        lines.append(f"  WARNING could not set core.excludesFile: {set_result.stderr.strip()}")


def run(target: Path) -> "tuple[int, str]":
    lines = []
    try:
        primary = _common.resolve_primary(target)
    except _common.TargetError as exc:
        return 1, f"ERROR: {exc}"

    cfg_result = _common.load_config(target)
    if cfg_result.error:
        return 1, f"ERROR: could not read worktree.setup config: {cfg_result.error}"
    if not cfg_result.configured:
        return 0, f"worktree.setup not configured — {DOC_POINTER}"

    cfg = cfg_result.config
    link_entries, link_warn = _common.str_list(cfg, "link")
    copy_entries, copy_warn = _common.str_list(cfg, "copy")
    exclude_entries, exclude_warn = _common.str_list(cfg, "exclude")
    for w in (link_warn, copy_warn, exclude_warn):
        if w:
            lines.append(f"  WARNING {w}")





    both = sorted(set(link_entries) & set(copy_entries))
    if both:
        for entry in both:
            lines.append(f"  WARNING refusing {entry!r} — declared in BOTH link and copy (pick one)")
        link_entries = tuple(e for e in link_entries if e not in both)
        copy_entries = tuple(e for e in copy_entries if e not in both)

    if target.resolve() == primary.resolve():
        if link_entries or copy_entries:
            lines.append("  target is the primary checkout — nothing to link or copy into itself")
        link_entries, copy_entries = (), ()



    link_entries, link_paths = _validate(link_entries, primary, target, lines)
    copy_entries, copy_paths = _validate(copy_entries, primary, target, lines)
    valid_exclude = []
    for e in exclude_entries:
        reason = _common.validate_entry(e)
        if reason:
            lines.append(f"  WARNING refusing {e!r} — {reason}")
            continue
        valid_exclude.append(e)
    exclude_entries = tuple(valid_exclude)

    manifest_result = _common.read_manifest(target)
















    manifest_unreadable = manifest_result.error is not None
    if manifest_unreadable:
        lines.append(
            "  WARNING could not read the existing provisioning manifest "
            f"({manifest_result.error}) -- proceeding with provisioning, "
            "but leaving the on-disk manifest untouched rather than "
            "overwriting it with an incomplete record"
        )
        manifest = {"linked": [], "copied": [], "excluded": [], "copy_fingerprints": {}}
    else:
        manifest = manifest_result.config
    for key in ("linked", "copied", "excluded"):
        manifest.setdefault(key, [])
    manifest.setdefault("copy_fingerprints", {})

    if link_entries:
        lines.append("link:")
        for entry in link_entries:
            source, dest = link_paths[entry]
            _do_link(entry, source, dest, lines, manifest)

    if copy_entries:
        lines.append("copy:")
        for entry in copy_entries:
            source, dest = copy_paths[entry]
            _do_copy(entry, source, dest, lines, manifest)

    if exclude_entries:
        lines.append("exclude:")
        _do_exclude(target, exclude_entries, lines, manifest)

    if manifest_unreadable:
        lines.append(
            "  WARNING skipped saving the provisioning manifest -- could not "
            "read the existing one, refusing to overwrite it; this run's own "
            "entries above may go unrecorded until the manifest is readable again"
        )
    else:
        manifest_write_error = _common.write_manifest(target, manifest)
        if manifest_write_error:
            lines.append(f"  WARNING could not save the provisioning manifest, teardown may miss entries: {manifest_write_error}")

    if not (link_entries or copy_entries or exclude_entries):
        if lines:




            return 0, "worktree:setup " + str(target) + "\n" + "\n".join(lines)
        return 0, "worktree.setup is configured but declares no link/copy/exclude entries — nothing to do"

    return 0, "worktree:setup " + str(target) + "\n" + "\n".join(lines)
