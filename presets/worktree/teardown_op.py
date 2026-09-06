#!/usr/bin/env python3
"""`worktree:teardown[:PATH]` — undo what `worktree:setup` did, leave
everything else alone (#532).

The only way to tell "a copy `setup` created" from "a file the user made
by hand at the same path" is to have recorded which is which at creation
time — content alone cannot answer it, and re-deriving it from the config
(which just names the same relative path either way) cannot either. So
teardown works from the manifest `setup_op.py` writes, not from the config
directly: an entry is only ever removed when `setup` itself recorded
creating it, which is also why a manifest lost or never written means
teardown removes nothing rather than guessing from the config that
everything configured must be ours.

`link` entries get a second check on top of the manifest: the symlink must
still resolve to the primary checkout right now. A worktree where the user
deleted the symlink and dropped in real content of their own has, by
definition, nothing of ours left to remove — teardown leaves it alone and
says so, rather than deleting content it never created.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _common  # noqa: E402
from setup_op import DOC_POINTER, _same_target  # noqa: E402


def _remove_link(entry: str, source: Path, dest: Path, lines: list) -> None:
    if not dest.exists() and not dest.is_symlink():
        lines.append(f"  already gone: {entry}")
        return
    if not _same_target(dest, source):
        lines.append(f"  left alone (no longer our symlink — user modified?): {entry}")
        return
    dest.unlink()
    lines.append(f"  removed link: {entry}")


def _remove_copy(entry: str, dest: Path, lines: list) -> None:
    if not dest.exists() and not dest.is_symlink():
        lines.append(f"  already gone: {entry}")
        return
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    else:
        shutil.rmtree(dest)
    lines.append(f"  removed copy: {entry}")


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
        return 0, f"worktree.setup not configured — {DOC_POINTER} — nothing to tear down"

    manifest = _common.read_manifest(target)
    linked = manifest.get("linked", [])
    copied = manifest.get("copied", [])

    if not linked and not copied:
        return 0, "no provisioning manifest for this worktree — nothing recorded as setup's own, nothing removed"

    if linked:
        lines.append("link:")
        for entry in linked:
            _remove_link(entry, primary / entry, target / entry, lines)

    if copied:
        lines.append("copy:")
        for entry in copied:
            _remove_copy(entry, target / entry, lines)

    # Trim the exclude file down to only the entries this run's config still
    # names — never delete the file outright, since a worktree-private
    # core.excludesFile pointing at a since-removed file is harmless, and
    # leaving the FILE in place (empty or not) is simpler than also having
    # to undo the `git config --worktree` pointer.
    cfg = cfg_result.config
    exclude_entries, _ = _common.str_list(cfg, "exclude")
    if exclude_entries:
        try:
            exclude_file = _common.git_path(target, _common.EXCLUDE_REL)
        except _common.TargetError:
            exclude_file = None
        if exclude_file is not None and exclude_file.is_file():
            remaining = [
                line for line in exclude_file.read_text(encoding="utf-8").splitlines()
                if line not in exclude_entries
            ]
            exclude_file.write_text(
                ("\n".join(remaining) + "\n") if remaining else "", encoding="utf-8")
            lines.append(f"exclude: removed {len(exclude_entries)} worktree-private entry/entries")

    try:
        manifest_path = _common.git_path(target, _common.MANIFEST_REL)
        if manifest_path.is_file():
            manifest_path.unlink()
    except _common.TargetError:
        pass

    return 0, "worktree:teardown " + str(target) + "\n" + "\n".join(lines)
