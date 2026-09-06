#!/usr/bin/env python3
"""`worktree:setup[:PATH]` — provision a worktree from the primary checkout,
driven entirely by `ops.worktree.setup` in `.supertool.json` (#532).

Three configured kinds, each with a different failure mode if you get it
backwards (the issue's own warning, taken seriously here rather than left to
discipline):

  * `link`   — symlinked from the primary checkout. For large, immutable,
    shared artefacts. NEVER for anything the worktree might mutate or that
    is machine-specific — a symlink shares state back into the primary
    checkout, so a write in the worktree is a write to everyone.
  * `copy`   — copied, never linked. For machine/environment-specific state,
    or anything the worktree owns and may rewrite. `shutil.copytree(...,
    symlinks=False)` / `shutil.copy2` — deliberately not `os.symlink` under
    any code path, because THAT is exactly the corruption the issue names:
    a `copy`-declared path silently becoming a symlink by a coding
    shortcut is the one mistake this module refuses to make no matter how
    it is called.
  * `exclude` — appended to a worktree-PRIVATE exclude file (see
    `_common.git_path`'s docstring for why not `.git/info/exclude`, which
    git shares across every worktree of the repo) so a symlinked artefact
    can never be swept into a commit by `git add -A` / `git add .`.

Every configured entry gets exactly one of three reported outcomes —
linked/copied/excluded, skipped-with-a-reason, or warned-and-skipped for a
missing source — never a bare "done". A missing source WARNS rather than
FAILS: some artefacts are legitimately regenerated on demand (over HTTP, in
this project's own case) and a hard failure here would make the op worse
than not running it at all. The same "warn, never crash" discipline covers
every FILESYSTEM operation below (#532 self-review): `os.symlink`,
`shutil.copytree`/`copy2` and the exclude-file write can all raise a plain
`OSError` for reasons that have nothing to do with this op's own logic
(permission denied, a full disk, a Windows host with no symlink privilege),
and none of those should abort every remaining configured entry with a raw
traceback.

Every `link`/`copy`/`exclude` entry is validated BEFORE anything touches
disk (`_common.validate_entry`/`safe_join`, #532 self-review): entries come
from `.supertool.json` in the worktree BEING PROVISIONED, which is routinely
someone else's branch, so an entry containing a newline, an absolute path,
or a `..` segment is refused rather than trusted — the first would let a
crafted config forge a fake outcome line in this op's own receipt, the
other two would let it name a source or destination outside the primary
checkout / target worktree entirely.

Idempotent by construction: an entry already in the state `setup` would
produce is reported as already-there and touched a second time only to
record it in the manifest if it somehow was not already (recovering from a
manifest that was itself lost or hand-edited).
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


DOC_POINTER = "declare ops.worktree.setup (link/copy/exclude) in .supertool.json — see docs/presets/worktree.md"


def _same_target(link_path: Path, expected: Path) -> bool:
    """Is LINK_PATH a symlink whose resolved target is EXPECTED?"""
    if not link_path.is_symlink():
        return False
    try:
        return link_path.resolve() == expected.resolve()
    except OSError:
        return False


def _validate(entries: tuple, root_a: Path, root_b: Path, lines: list) -> "tuple[tuple, dict]":
    """Split ENTRIES into (valid, {entry: (source, dest)}), refusing any
    entry `_common.safe_join` rejects against EITHER root — never touching
    disk for a refused entry.
    """
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

    # A path declared in BOTH link and copy is the exact corruption hazard
    # the issue names (link shares mutable state back to the primary
    # checkout; copy exists precisely because some paths must NOT do that).
    # Refuse to touch it under either heading rather than picking one.
    both = sorted(set(link_entries) & set(copy_entries))
    if both:
        for entry in both:
            lines.append(f"  WARNING refusing {entry} — declared in BOTH link and copy (pick one)")
        link_entries = tuple(e for e in link_entries if e not in both)
        copy_entries = tuple(e for e in copy_entries if e not in both)

    if target.resolve() == primary.resolve():
        if link_entries or copy_entries:
            lines.append("  target is the primary checkout — nothing to link or copy into itself")
        link_entries, copy_entries = (), ()

    # Validate structurally BEFORE anything touches disk -- untrusted config,
    # see the module docstring and `_common.validate_entry`.
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
    if manifest_result.error:
        lines.append(f"  WARNING could not read the existing provisioning manifest, treating as empty: {manifest_result.error}")
        manifest = {"linked": [], "copied": [], "excluded": []}
    else:
        manifest = manifest_result.config
    for key in ("linked", "copied", "excluded"):
        manifest.setdefault(key, [])

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

    manifest_write_error = _common.write_manifest(target, manifest)
    if manifest_write_error:
        lines.append(f"  WARNING could not save the provisioning manifest, teardown may miss entries: {manifest_write_error}")

    if not (link_entries or copy_entries or exclude_entries):
        if lines:
            # Something WAS declared (e.g. a path refused for being in both
            # `link` and `copy`) even though nothing is left to act on --
            # that warning must reach the caller, never be swallowed by a
            # generic "nothing to do" that reads as a clean, unremarkable run.
            return 0, "worktree:setup " + str(target) + "\n" + "\n".join(lines)
        return 0, "worktree.setup is configured but declares no link/copy/exclude entries — nothing to do"

    return 0, "worktree:setup " + str(target) + "\n" + "\n".join(lines)
