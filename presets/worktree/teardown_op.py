#!/usr/bin/env python3
"""`worktree:teardown[:PATH]` — undo what `worktree:setup` did, leave
everything else alone (#532).

The only way to tell "a copy `setup` created" from "a file the user made
by hand at the same path" is to have recorded which is which at creation
time — content alone cannot answer it, and re-deriving it from the config
(which just names the same relative path either way) cannot either. So
teardown works from the manifest `setup_op.py` writes, not from the config
directly, for ALL THREE kinds — `link`, `copy` AND `exclude` (#532
self-review: `exclude` used to be driven by the CURRENT config instead of
the manifest, which meant an exclude-only setup with no `link`/`copy`
entries had literally nothing recorded to check and teardown reported
"nothing to tear down" while the exclude file it should have cleaned up sat
untouched; it also meant editing the config between `setup` and `teardown`
silently orphaned whatever `exclude` entries had been dropped from it, since
teardown only ever knew about what the config says NOW). An entry is only
ever removed when `setup` itself recorded creating it, which is also why a
manifest that never existed means teardown removes nothing rather than
guessing from the config that everything configured must be ours.

A manifest that EXISTS but could not be parsed is a DIFFERENT claim from a
manifest that never existed, and is refused rather than silently treated as
empty (#532 self-review) — treating "I could not read what setup did" as
"setup did nothing" would leave a live symlink into the primary checkout, or
a real copy, sitting there while the receipt reads exactly like a clean,
already-torn-down worktree.

`link` entries get a second check on top of the manifest: the symlink must
still resolve to the primary checkout right now. A worktree where the user
deleted the symlink and dropped in real content of their own has, by
definition, nothing of ours left to remove — teardown leaves it alone and
says so, rather than deleting content it never created.

Every actual filesystem removal is wrapped so an `OSError` (a locked file, a
permission error) reports a WARNING and moves on to the next entry, rather
than crashing the whole run and leaving every entry after it untouched with
no explanation (#532 self-review, matching `setup_op.py`'s own discipline).
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
    try:
        dest.unlink()
    except OSError as exc:
        lines.append(f"  WARNING could not remove link, left in place: {entry} ({exc})")
        return
    lines.append(f"  removed link: {entry}")


def _remove_copy(entry: str, dest: Path, lines: list) -> None:
    if not dest.exists() and not dest.is_symlink():
        lines.append(f"  already gone: {entry}")
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

    if not linked and not copied and not excluded:
        return 0, "no provisioning manifest for this worktree — nothing recorded as setup's own, nothing removed"

    if linked:
        lines.append("link:")
        for entry in linked:
            source, reason = _common.safe_join(primary, entry)
            dest, dest_reason = _common.safe_join(target, entry)
            if reason or dest_reason:
                lines.append(f"  WARNING left alone (manifest entry no longer valid — {reason or dest_reason}): {entry}")
                continue
            _remove_link(entry, source, dest, lines)

    if copied:
        lines.append("copy:")
        for entry in copied:
            dest, reason = _common.safe_join(target, entry)
            if reason:
                lines.append(f"  WARNING left alone (manifest entry no longer valid — {reason}): {entry}")
                continue
            _remove_copy(entry, dest, lines)

    # The exclude file is trimmed to exactly the entries THIS MANIFEST
    # recorded creating -- never to whatever the config says right now (see
    # the module docstring), and never deleted outright: a worktree-private
    # `core.excludesFile` pointing at a since-emptied file is harmless, and
    # leaving the FILE in place is simpler than also undoing the
    # `git config --worktree` pointer.
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

    try:
        manifest_path = _common.git_path(target, _common.MANIFEST_REL)
        if manifest_path.is_file():
            manifest_path.unlink()
    except (_common.TargetError, OSError):
        pass

    return 0, "worktree:teardown " + str(target) + "\n" + "\n".join(lines)
