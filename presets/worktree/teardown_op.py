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

`copy` entries get an analogous second check (#2429), built differently
because `copy` has no single fact like a link target to compare against:
`setup` records a `fingerprint_copy` of the copy right after creating it,
and teardown recomputes it before deleting. `copy` is declared (per
`worktree.json`) for "anything machine-specific or the worktree might
mutate" — exactly the kind of entry whose content is EXPECTED to diverge
from what `setup` wrote, so deleting it unconditionally, as this used to,
could destroy real work the moment it was ever legitimately edited. A
mismatched, missing, or unreadable fingerprint is all treated the same —
left alone, named, and pointed at `:force` — per this repo's own rule that
an unknown must never collapse into "unchanged".

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


def _copy_is_stale(dest: Path, recorded: "str | None") -> "str | None":
    """None if DEST still matches what `setup` recorded creating; otherwise
    the reason it is treated as modified and left alone (#2429).

    `copy` (per `worktree.json`) is declared for "anything machine-specific
    or the worktree might mutate" -- exactly the kind whose content is
    EXPECTED to diverge from what `setup` wrote, unlike a `link` entry
    (`_same_target`, above), which is meant to stay untouched for its
    whole life. Deleting a `copy` entry unconditionally, the way this used
    to work, could destroy real work the moment the entry was ever legitimately
    edited -- and this preset has no analogous single fact (a link target)
    to compare against, so the check has to be built rather than reused.

    Both "the recording is missing" and "the recording exists but no
    longer matches" are treated identically, as staleness, per this
    repo's own "three states, not two, and err toward refusing when you
    cannot tell" rule (CLAUDE.md) -- a `None` here must never be read as
    "confirmed unchanged".
    """
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
    """Report what `extensions.worktreeConfig` (#2419) actually is right
    now -- three states, not a guess. Teardown never sets or unsets this
    SHARED, repo-wide flag itself, but a receipt that assumed it must still
    be `true` because `setup` normally enables it would be a false claim
    the moment it was changed out-of-band (a human, another tool) between
    `setup` and `teardown` -- caught by review on #2419.
    """
    result = _common._run_git(["config", "--get", "extensions.worktreeConfig"], target)
    if result.returncode == 0:
        value = result.stdout.strip()
    elif result.returncode == 1:
        value = None  # git's own signal: key not set, not an error
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

    # The exclude file is trimmed to exactly the entries THIS MANIFEST
    # recorded creating -- never to whatever the config says right now (see
    # the module docstring), and never deleted outright: a worktree-private
    # `core.excludesFile` pointing at a since-emptied file is harmless, and
    # leaving the FILE in place is simpler than also undoing the
    # `git config --worktree` pointer.
    #
    # `extensions.worktreeConfig` itself (#2419) is left alone too, and for
    # a stronger reason than "simpler": `setup_op._do_exclude` enables it
    # with a plain `git config extensions.worktreeConfig true` -- no
    # `--worktree` flag -- so it lands in the SHARED `.git/config`, not a
    # file scoped to this worktree. Disabling it here would not undo
    # anything this worktree alone did; it would stop every worktree in the
    # repository, including ones that do not exist yet, from having its own
    # `--worktree`-scoped config read AT ALL. A sibling worktree that is
    # still relying on its own `core.excludesFile` set this way would have
    # that setting silently stop applying -- its `git status` would start
    # showing whatever it excludes as untracked again, with nothing telling
    # it why.
    #
    # A live sibling worktree IS enumerable at teardown time (`git worktree
    # list --porcelain`, same as `_common.resolve_primary` already does),
    # so "check whether anything else still needs it" is not impossible --
    # only a worktree added AFTER this teardown runs is genuinely
    # unreachable to any such check. Doing the check anyway would buy
    # nothing: leaving the flag enabled costs nothing (with no
    # `config.worktree` file present the extra lookup it adds simply finds
    # nothing), while a "was I the last one" check that races a
    # concurrently-created sibling would trade a harmless no-op for a real
    # way to break one. So this teardown never touches the flag either
    # way -- it reports what it actually finds below, rather than assuming
    # what `setup` usually leaves behind.
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
        # Everything this manifest named has already been undone above (or
        # reported a WARNING and left in place) by the time we get here, so
        # a failure to remove the RECEIPT itself is never a reason to fail
        # the whole run -- the next `worktree:setup` just overwrites a stale
        # manifest rather than reading it. Reported rather than swallowed
        # (matching this function's own WARNING discipline elsewhere) so a
        # permission error here is at least visible, never silent.
        lines.append(f"  WARNING could not remove the provisioning manifest itself: {exc}")

    return 0, "worktree:teardown " + str(target) + "\n" + "\n".join(lines)
