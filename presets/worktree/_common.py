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

import json
import subprocess
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
    asks for. `error` is set only when a *found* config file could not be
    parsed as JSON or was not an object; a config that is simply absent is
    never routed through `error`.
    """

    def __init__(self, config: Optional[dict], error: Optional[str] = None):
        self.config = config
        self.error = error

    @property
    def configured(self) -> bool:
        return self.error is None and self.config is not None


def load_config(target: Path) -> ConfigResult:
    """Walk up from TARGET for the nearest `.supertool.json`'s
    `ops.worktree.setup` section.
    """
    d = target
    while True:
        candidate = d / CONFIG_FILENAME
        if candidate.is_file():
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


def validate_entry(entry: str) -> Optional[str]:
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
    """
    if not isinstance(entry, str) or not entry:
        return "not a non-empty string"
    if "\n" in entry or "\r" in entry:
        return "contains a newline or carriage return"
    p = Path(entry)
    if p.is_absolute():
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
    """The `{"linked": [...], "copied": [...], "excluded": [...]}` manifest
    `setup` wrote, as a `ConfigResult` — three states, not two (#532
    self-review): a manifest that never existed (`setup` never ran) is
    `ConfigResult({...empty lists...})`, exactly like before; a manifest that
    EXISTS but could not be parsed is now `ConfigResult(None, error=...)`
    rather than silently collapsing into the same empty shape — the two are
    not the same claim, and `teardown_op.py` must not treat "I could not read
    what setup did" as "setup did nothing," which used to leave a live
    symlink into the primary checkout untouched with a receipt reading
    exactly like a clean, empty teardown.
    """
    empty = {"linked": [], "copied": [], "excluded": []}
    try:
        path = git_path(target, MANIFEST_REL)
    except TargetError:
        return ConfigResult(empty)
    if not path.is_file():
        return ConfigResult(empty)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ConfigResult(None, f"{path}: {exc}")
    if not isinstance(data, dict):
        return ConfigResult(None, f"{path}: top level is not a JSON object")
    manifest = dict(empty)
    for key in ("linked", "copied", "excluded"):
        raw = data.get(key)
        if isinstance(raw, list):
            manifest[key] = [str(p) for p in raw]
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
