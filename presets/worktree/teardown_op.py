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
from setup_op import DOC_POINTER, _same_target  


def _remove_link(entry: str, source: Path, dest: Path, lines: list) -> None:
    if not dest.exists() and not dest.is_symlink():
        lines.append(f"  already gone: {entry}")
        return
    if not _same_target(dest, source):
        lines.append(f"  left alone (no longer our symlink — user modified?): {entry}")
        return
    try:
        dest.unlink()
    except OSError as exc:
        lines.append(f"  WARNING could not remove link, left in place: {entry} ({exc})")
        return
    lines.append(f"  removed link: {entry}")


def _copy_is_stale(dest: Path, recorded: "str | None") -> "str | None":


















    if recorded is None:
        return "no fingerprint recorded for this entry (manifest predates this check, or `setup` itself could not compute one) -- cannot confirm nothing has changed since"
    current = _common.fingerprint_copy(dest)
    if current is None:
        return "could not compute a current fingerprint to compare (permission error?) -- cannot confirm nothing has changed since"
    if current != recorded:
        return "content differs from what setup created -- modified since provisioning?"
    return None


def _remove_copy(entry: str, dest: Path, lines: list, recorded_fingerprint: "str | None", force: bool) -> None:
    if not dest.exists() and not dest.is_symlink():
        lines.append(f"  already gone: {entry}")
        return
    if not force:
        stale_reason = _copy_is_stale(dest, recorded_fingerprint)
        if stale_reason:
            lines.append(
                f"  left alone ({stale_reason}): {entry} "
                "-- rerun `worktree:teardown:PATH:force` to remove anyway"
            )
            return
    try:
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    except OSError as exc:
        lines.append(f"  WARNING could not remove copy, left in place: {entry} ({exc})")
        return
    lines.append(f"  removed copy: {entry}")


def _worktreeconfig_state_line(target: Path) -> str:







    result = _common._run_git(["config", "--get", "extensions.worktreeConfig"], target)
    if result.returncode == 0:
        value = result.stdout.strip()
    elif result.returncode == 1:
        value = None  
    else:
        return (
            "  extensions.worktreeConfig: could not determine current state "
            f"({result.stderr.strip() or 'git exited ' + str(result.returncode)})"
        )
    if value == "true":
        return (
            "  extensions.worktreeConfig left enabled (repo-wide, shared .git/config -- "
            "unsetting it here would also stop any sibling worktree's own --worktree-scoped "
            "config, e.g. core.excludesFile, from being read; see docs/presets/worktree.md)"
        )
    return (
        f"  extensions.worktreeConfig is not currently set to true (value={value!r}) -- "
        "teardown never sets or unsets this repo-wide flag itself; see docs/presets/worktree.md"
    )


def run(target: Path, force: bool = False) -> "tuple[int, str]":
    lines = []
    try:
        primary = _common.resolve_primary(target)
    except _common.TargetError as exc:
        return 1, f"ERROR: {exc}"

    cfg_result = _common.load_config(target)
    if cfg_result.error:
        return 1, f"ERROR: could not read worktree.setup config: {cfg_result.error}"
    if not cfg_result.configured:
        return 0, f"worktree.setup not configured — {DOC_POINTER} — nothing to tear down"

    manifest_result = _common.read_manifest(target)
    if manifest_result.error:
        return 1, (
            f"ERROR: could not read the provisioning manifest, refusing to guess what setup created: "
            f"{manifest_result.error}"
        )
    manifest = manifest_result.config
    linked = manifest.get("linked", [])
    copied = manifest.get("copied", [])
    excluded = manifest.get("excluded", [])
    copy_fingerprints = manifest.get("copy_fingerprints", {})

    if not linked and not copied and not excluded:
        return 0, "no provisioning manifest for this worktree — nothing recorded as setup's own, nothing removed"

    if linked:
        lines.append("link:")
        for entry in linked:
            source, reason = _common.safe_join(primary, entry)
            dest, dest_reason = _common.safe_join(target, entry)
            if reason or dest_reason:
                lines.append(f"  WARNING left alone (manifest entry no longer valid — {reason or dest_reason}): {entry!r}")
                continue
            _remove_link(entry, source, dest, lines)

    if copied:
        lines.append("copy:")
        for entry in copied:
            dest, reason = _common.safe_join(target, entry)
            if reason:
                lines.append(f"  WARNING left alone (manifest entry no longer valid — {reason}): {entry!r}")
                continue
            _remove_copy(entry, dest, lines, copy_fingerprints.get(entry), force)

































    if excluded:
        try:
            exclude_file = _common.git_path(target, _common.EXCLUDE_REL)
            if exclude_file.is_file():
                remaining = [
                    line for line in exclude_file.read_text(encoding="utf-8").splitlines()
                    if line not in excluded
                ]
                exclude_file.write_text(
                    ("\n".join(remaining) + "\n") if remaining else "", encoding="utf-8")
                lines.append(f"exclude: removed {len(excluded)} worktree-private entry/entries")
        except (_common.TargetError, OSError) as exc:
            lines.append(f"  WARNING could not clean up the worktree-private exclude file: {exc}")
        lines.append(_worktreeconfig_state_line(target))

    try:
        manifest_path = _common.git_path(target, _common.MANIFEST_REL)
        if manifest_path.is_file():
            manifest_path.unlink()
    except (_common.TargetError, OSError) as exc:







        lines.append(f"  WARNING could not remove the provisioning manifest itself: {exc}")

    return 0, "worktree:teardown " + str(target) + "\n" + "\n".join(lines)
