#!/usr/bin/env python3
"""Shared helpers for `worktree:setup` / `worktree:teardown` (#532).

A fresh git worktree cannot run a project's test suite when the suite
depends on gitignored local state — vendored binaries, machine-specific DB
config, a generated asset cache. None of that is committed, so a plain
`git worktree add` never brings it along, and each missing piece fails in
its own misleading way (see the issue for three real examples).

`worktree:setup` provisions a worktree from the *primary checkout*, driven
entirely by a project's own `.supertool.json` — nothing repo-specific lives
in this preset. Everything below is the mechanics shared by `setup_op.py`
and `teardown_op.py`: resolving which directory is the "primary" one,
reading the `ops.worktree.setup` config section, and reading/writing the
manifest of what `setup` actually created (so `teardown` can tell "ours" from
"a file the user made by hand" — the pairing the issue calls out by name).

**Why `git -C TARGET`, not this repo's own `_git_common._git`.** That helper
runs in the *process* cwd, which is exactly wrong here: `worktree:setup` is
routinely invoked from directory A to provision directory B (or from B to
read config that names A as the primary checkout). Every git call in this
module takes an explicit TARGET and runs `git -C TARGET ...` rather than
assuming cwd is the directory in question.
"""
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

#: Name of this repo's own per-project config file. Not a new format — the
#: issue's own TOML sketch (`[worktree.setup]`) is written in JSON here,
#: nested the same way `ops.git-diff.red_flags_extra` and
#: `ops.radar.radar_tiers` already nest per-op config: `ops.worktree.setup`.
CONFIG_FILENAME = ".supertool.json"

#: Relative path (through `git rev-parse --git-path`) for the manifest this
#: preset writes recording exactly what `setup` created. Not under
#: `info/`, `logs/` or any other name git treats as shared across worktrees
#: (see `git_path`'s docstring) — a made-up subdirectory resolves into the
#: per-worktree PRIVATE area (`.git/worktrees/<name>/...`), which is what
#: makes this genuinely worktree-scoped rather than shared with the primary
#: checkout or a sibling worktree.
MANIFEST_REL = "worktree-setup/manifest.json"

#: Same reasoning, for the exclude file `setup` maintains. See `setup_op.py`
#: for why this is NOT simply `.git/info/exclude` — that path is one of the
#: handful git treats as shared across every worktree of a repo, which would
#: make one worktree's provisioning silently change what every sibling
#: worktree's `git status` considers untracked.
EXCLUDE_REL = "worktree-setup/exclude"


class TargetError(Exception):
    """The target directory could not be resolved as a git worktree."""


def fingerprint_copy(path: Path) -> Optional[str]:
    """A stability marker for a `copy` manifest entry: `setup` records this
    right after copying, `teardown` recomputes it before deleting, and a
    mismatch means the entry has been touched since -- likely real work,
    not `setup`'s own leftover (#2429).

    Deliberately NOT a content hash. `copy` exists precisely for
    machine-specific and worktree-mutated artefacts -- a build output, a
    vendored binary, a generated cache -- exactly the kind that is large
    and legitimately rewritten wholesale. Hashing the actual bytes of every
    such entry on every `setup` and every `teardown` would be the most
    expensive part of provisioning a worktree, paid on the common path,
    for something (size, mtime_ns) already answers just as well: virtually
    every real edit -- a rebuild, an editor save, `git checkout` rewriting
    a tracked copy -- changes at least one of the two. The one gap this
    accepts (a rewrite that reproduces byte-for-byte size and mtime,
    ns-precision) is narrow enough that closing it is not worth doubling
    the cost of every setup/teardown pair to do it.

    A single file's fingerprint is its own `(size, mtime_ns)`; a directory's
    is a hash over every contained file's `(relpath, size, mtime_ns)`,
    sorted so the walk order never matters. Returns `None` -- an UNKNOWN,
    never a stand-in for "unchanged" -- when PATH does not exist or a
    filesystem error stops the walk partway through (a permission error,
    a symlink loop): the caller must treat that the same as a genuine
    mismatch, per this repo's own "three states, not two" rule, rather
    than reading a fingerprint that could not be computed as one that
    matched.
    """
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
    """`git -C CWD ARGS`, with a stall reported rather than raised.

    Mirrors the three-state discipline `_git_common._git` already applies
    to the plain-cwd case: a non-zero returncode is git's own answer, a
    timeout is reported as one (returncode -1, distinct from any code git
    itself produces) rather than raising and losing every partial line of
    stderr an earlier failure would have shown.
    """
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
    """TARGET, resolved and confirmed to be inside a git worktree.

    Defaults to cwd when PATH_ARG is empty/None, matching every other
    supertool op's "no path means here" convention.
    """
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
    """`git rev-parse --git-common-dir` from TARGET, or None if it could not
    be read. Two directories share a repository iff their common dirs match
    once resolved — this is what stands in for the generic cwd/repo
    containment boundary on this preset's PATH argument (see
    `worktree.json`'s "paths": {"args": []} and the comment beside it):
    an out-of-cwd PATH is not merely allowed here, it is the documented use
    case (provisioning a SIBLING worktree, or reading from the PRIMARY
    checkout while standing inside a linked one — both routinely outside
    cwd by construction), so the boundary this preset enforces instead is
    "PATH must be a worktree of the very repository you are already
    operating in", checked in dispatcher.py before either op ever runs.
    """
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
    """The repo's primary (non-linked) checkout.

    `git worktree list --porcelain` always lists the primary checkout
    first — verified against real git (2.46), not assumed from the docs —
    so the first `worktree ` line is the answer regardless of which
    worktree TARGET itself is.
    """
    result = _run_git(["worktree", "list", "--porcelain"], target)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "git did not answer"
        raise TargetError(f"could not list worktrees from {target}: {stderr}")
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):]).resolve()
    raise TargetError(f"`git worktree list` returned no worktree at all from {target}")


def git_path(target: Path, rel: str) -> Path:
    """Resolve REL through `git rev-parse --git-path`, run against TARGET.

    Git resolves a handful of well-known relative paths (`info/exclude`,
    `logs/HEAD`, ...) into the SHARED common git dir even for a linked
    worktree — verified directly (`git rev-parse --git-path info/exclude`
    from a linked worktree prints the primary checkout's `.git/info/exclude`,
    not a private one). A path git does not recognise as one of those,
    like the `worktree-setup/...` paths this module uses, resolves into the
    per-worktree PRIVATE directory instead (`.git/worktrees/<name>/...`).
    That distinction is the whole mechanism this preset's worktree-scoping
    relies on, so it is centralised here rather than assumed at each call
    site.
    """
    result = _run_git(["rev-parse", "--git-path", rel], target)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "git did not answer"
        raise TargetError(f"could not resolve git path {rel!r} for {target}: {stderr}")
    resolved = Path(result.stdout.strip())
    if not resolved.is_absolute():
        resolved = target / resolved
    return resolved


class ConfigResult:
    """Three states, not two (docs/validators.md) — never a bare dict/None.

    `config` is the `ops.worktree.setup` object once resolved: `{}` when
    the project HAS the section but never populated `link`/`copy`/`exclude`
    is a real, distinguishable value from `config is None`, which means
    the section was never declared at all — the clean no-op case the issue
    asks for. `error` is set only when a *found, trusted* config file could
    not be parsed as JSON or was not an object; a config that is simply
    absent, or that failed the ownership/permission check below (#2370),
    is never routed through `error` — see `load_config`.
    """

    def __init__(self, config: Optional[dict], error: Optional[str] = None):
        self.config = config
        self.error = error

    @property
    def configured(self) -> bool:
        return self.error is None and self.config is not None


def _config_trust_violation(candidate: Path) -> Optional[str]:
    """POSIX ownership/permission guard, mirroring `_supertool._load_config`'s
    own #695 hardening (and `presets/gitlab/_maintenance.py`'s #2365 copy of
    it — presets cannot import the core module, so each walk-up loader in
    this codebase re-implements the same check rather than sharing it).

    `worktree.setup`'s config is read from the WORKTREE BEING PROVISIONED
    (`load_config` walks up from `target`), and its `link`/`copy`/`exclude`
    entries drive real filesystem effects (`os.symlink`, `shutil.copytree`,
    lines appended to a worktree-private `core.excludesFile`). A
    group/world-writable `.supertool.json`, or one owned by a different
    local user, can be rewritten by another account between the moment it
    was reviewed and the moment `worktree:setup` reads it — the same TOCTOU
    shape #695 closed for the core loader every other op goes through.

    POSIX-only: `st_uid` and the write bits are meaningless on Windows, so
    this returns `None` (trusted) unconditionally there. Root is treated as
    trusted, matching `_supertool._config_trust_violation`.
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


def load_config(target: Path) -> ConfigResult:
    """Walk up from TARGET for the nearest `.supertool.json`'s
    `ops.worktree.setup` section, stopping at the nearest `.git` ancestor
    and skipping a config this process does not own (#2370).

    Two limits, matching `_supertool._load_config`'s own #695 hardening —
    a config is exactly as trusted as the project that owns it, and this
    walk must not reach OUTSIDE that project, nor accept a config another
    local account could have rewritten:

    * the walk stops once it reaches a directory containing `.git` — the
      repo root — rather than continuing to `/`; a target not inside a git
      repo at all keeps walking to `/`, since there is no repo boundary;
    * each candidate is checked with `_config_trust_violation` before it is
      opened — a file that is group/world-writable, or not owned by the
      caller (or root), is skipped with a warning on stderr, exactly like
      an absent one, so the walk can still find a further, trusted config
      higher up (until the repo-root boundary above stops it). This is
      deliberately treated the same as "absent" rather than surfaced as
      `.error`: the core loader (`_supertool._load_config`) accepts or
      skips each candidate independently as it walks, and this walk must
      accept exactly what the core loader would from the same directory —
      routing a skip through `.error` would make `worktree:setup` refuse
      outright in a case the core loader would happily resolve by finding
      a further candidate. A found-and-trusted-but-malformed file is
      unaffected by this and is still reported through `.error`, as before.
    """
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
    """cfg[key] as a tuple of strings, plus a warning when the declared
    value is present but not a list of strings — never silently coerced,
    never silently dropped without saying so.
    """
    raw = cfg.get(key)
    if raw is None:
        return (), None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return (), f"ops.worktree.setup.{key} must be a list of strings — ignoring it"
    return tuple(raw), None


def validate_entry(entry: str, path_cls: type = Path) -> Optional[str]:
    """None if ENTRY is safe to use as a `link`/`copy`/`exclude` entry, else
    the reason it is refused (#532 self-review).

    `.supertool.json` is read from the WORKTREE BEING PROVISIONED (`load_config`
    walks up from `target`), which is routinely a fresh checkout of a branch
    somebody else wrote — a PR being tested, say. Two things follow from an
    entry being untrusted text rather than the maintainer's own configuration:

    * A newline/carriage-return inside it would land verbatim in this op's own
      receipt (every outcome line is `f"...: {entry}"`), letting a crafted
      config forge a fake `linked: ...`/`excluded: ...` line inside what is
      really a single warning — indistinguishable, to anything parsing this
      op's stdout, from a real success. Refused outright rather than escaped:
      nothing here needs a literal newline, so there is no legitimate case to
      preserve.
    * An absolute path, or one containing a `..` segment, would let a crafted
      entry name a source or destination OUTSIDE the primary checkout /
      target worktree entirely. `safe_join` below is the second, structural
      half of this check (containment survives even a parts-based check that
      missed something); this half exists because the parts check is cheap
      and names the exact reason before any path is even joined.

    **`is_absolute()` alone missed a whole class on Windows** (#532 CI,
    windows-latest/3.11): `WindowsPath("/etc/passwd").is_absolute()` is
    `False` — pathlib calls a path "absolute" only once it names a drive,
    and a POSIX-style entry like `/etc/passwd` has none, so it is merely
    *rooted* ("anchored to whichever drive is current"). `root`/`drive`
    catch that (and the sibling case, a *drive-relative* entry like
    `C:foo` — relative to the CWD on drive C: at run time, which is exactly
    as unpredictable as no root at all) without weakening the check on
    POSIX: a legitimate relative entry has both empty there on either
    platform, and a POSIX host never sets `drive` at all. `safe_join`'s
    containment check still caught the escape either way (`root`/`drive`
    or not, an entry landing outside the target resolves outside it) — the
    bug was only that the CHEAP check no longer named the reason before the
    structural one had to.

    `path_cls` defaults to the platform's own `Path` and exists so a test on
    any host can exercise Windows path semantics deterministically via
    `PureWindowsPath`, the way `tests/test_worktree_setup_teardown_532.py`
    does — this repo's suite runs on macOS/Linux, so the Windows arm below
    is otherwise unreachable here and would be reasoned about, never
    observed, without it.
    """
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
    """(root / entry), refused with a reason if it would land outside ROOT.

    Belt-and-braces alongside `validate_entry`'s parts-based check: this one
    resolves symlinks and `.`/`..` the way the filesystem actually would,
    so it also catches a ROOT that itself contains a symlink component, not
    only a `..` spelled directly in ENTRY.
    """
    reason = validate_entry(entry)
    if reason:
        return None, reason
    candidate = root / entry
    # Resolve the PARENT, not the candidate itself: the candidate is
    # routinely the very symlink this preset created on a prior run (a
    # `link` entry, re-checked on the idempotent path, or the destination
    # `teardown` is about to remove), and resolving straight through it
    # follows the symlink to wherever it legitimately points -- the primary
    # checkout, for a `link` entry -- which is outside `root` by design and
    # would make this check refuse the very thing it is meant to allow
    # (#532 self-review fixup: this exact bug broke the idempotent-rerun and
    # teardown-leaves-a-real-symlink-alone tests the moment this check was
    # added). Resolving only the ancestry still catches a `..` that escaped
    # `validate_entry`'s parts-based check via a symlinked ancestor, and
    # still resolves a legitimately symlinked ROOT itself (e.g. macOS's
    # `/tmp` -> `/private/tmp`).
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
    """The `{"linked": [...], "copied": [...], "excluded": [...],
    "copy_fingerprints": {...}}` manifest `setup` wrote, as a `ConfigResult`
    — three states, not two (#532 self-review): a manifest that never
    existed (`setup` never ran) is `ConfigResult({...empty lists...})`,
    exactly like before; a manifest that EXISTS but could not be parsed is
    now `ConfigResult(None, error=...)` rather than silently collapsing
    into the same empty shape — the two are not the same claim, and
    `teardown_op.py` must not treat "I could not read what setup did" as
    "setup did nothing," which used to leave a live symlink into the
    primary checkout untouched with a receipt reading exactly like a
    clean, empty teardown.

    `copy_fingerprints` (#2429) maps each `copied` entry to the
    `fingerprint_copy` value `setup` recorded right after creating it, so
    `teardown` can tell a `copy` entry `setup` itself last touched from one
    the worktree has since mutated. A manifest written before this key
    existed simply omits it — read back as `{}`, an entry absent from that
    dict is a real, distinguishable "nothing was ever recorded for this
    one" rather than the same shape as "recorded and unchanged".

    Same three-state discipline applies to *resolving the manifest path
    itself* (#2371): `git rev-parse --git-path` does not look at whether the
    manifest file exists — it only computes where it WOULD be — so a
    nonzero return from it here is never "there is genuinely no manifest".
    By the time any caller reaches `read_manifest`, `target` has already
    been confirmed by `resolve_target` to be a real git worktree, so a
    failure at this step is `_run_git`'s own timeout/OSError guard
    (returncode -1) or some other transient git failure — an UNKNOWN, not
    an absence — and goes through `.error` exactly like a manifest that
    exists but fails to parse. Only `path.is_file()` returning False, once
    the path itself was actually resolved, is a genuine "setup never wrote
    one". Deliberately does not call the shared `git_path` helper here
    (unlike `write_manifest` below), because `git_path` collapses every
    nonzero `_run_git` result into a single `TargetError` and throws away
    the distinction this needs.
    """
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
    """Write MANIFEST, or return the reason it could not be written.

    Never raises: a manifest write is bookkeeping FOR teardown, not a
    condition of `setup` having succeeded, so the caller turns this into a
    WARNING rather than aborting a run that otherwise worked (#532
    self-review — `path.write_text` used to be a bare call, so a permission
    error or a full disk here crashed the whole op with a raw traceback,
    which is exactly the "never a hard failure" contract this preset
    otherwise holds everywhere else).
    """
    try:
        path = git_path(target, MANIFEST_REL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (TargetError, OSError) as exc:
        return str(exc)
    return None
