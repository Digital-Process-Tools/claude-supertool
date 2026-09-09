"""Shared safety helpers for social-publishing ops (closes #149).

Three guards centralised here so each platform's publish/comment script
gets the same protections:

1. **`file://` allowlist** — `safe_resolve_body_path` forces body files to
   live under `.max/`, `drafts/`, `posts/`, or `blog/` (relative to cwd).
   Closes the credential-exfil vector: `bluesky_publish:file:///Users/.../
   bluesky/app_password` is rejected before the file is read.
2. **Confirmation gate** — `require_confirm` blocks single-shot publishing.
   `|force` per-call or env / JSON opt-out bypasses for batch use.
3. **Token file mode** — `check_token_file_mode` refuses to load a token
   that's group/world-readable (mirrors what `git` enforces for SSH keys).

Each guard supports the same opt-out lookup order Florian asked for:
env var > project `.supertool.json` > default (strict).
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional


def _config_trust_violation(candidate: Path) -> Optional[str]:
    """POSIX ownership/permission guard, mirroring `_supertool._load_config`'s
    own #695 hardening (and `presets/gitlab/_maintenance.py`'s #2365 /
    `presets/worktree/_common.py`'s #2370 / `presets/slack/_authorization.py`'s
    #2416 copies of it -- presets cannot import the core module, so each
    walk-up loader in this codebase re-implements the same check rather than
    sharing it).

    `_supertool_config`'s result feeds `require_confirm`, `_body_allowlist`
    and `_disclosure_config` -- publish-credential-adjacent decisions for
    `bluesky`/`devto`/`hashnode`. A group/world-writable `.supertool.json`,
    or one owned by a different local user, is exactly the file another
    local account could rewrite between the moment it was reviewed and the
    moment this module reads it -- the same TOCTOU shape #695 closed for the
    core loader every other op goes through (#2366).

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


def _supertool_config() -> dict:
    """Walk up from cwd to find `.supertool.json`, stopping at the nearest
    `.git` ancestor and skipping a config this process does not own
    (#2366). Cached per process.

    Two limits, matching `_supertool._load_config`'s own #695 hardening --
    a config is exactly as trusted as the project that owns it, and this
    walk must not reach OUTSIDE that project, nor accept a config another
    local account could have rewritten:

    * the walk stops once it reaches a directory containing `.git` -- the
      repo root -- rather than continuing to `/`; a cwd not inside a git
      repo at all keeps walking to `/`, since there is no repo boundary;
    * each candidate is checked with `_config_trust_violation` before it
      is opened -- a file that is group/world-writable, or not owned by
      the caller (or root), is skipped with a warning on stderr, exactly
      like an absent one, so the walk can still find a further, trusted
      config higher up (until the repo-root boundary above stops it).

    An unreadable or malformed file used to fall back to `cfg = {}` with
    nothing said anywhere -- indistinguishable from a config file that
    parses fine and simply sets none of `no_publish_confirm`,
    `publish_body_allowlist`, `publish_disclosure_text` or
    `no_publish_disclosure` (#2306). Every publish safety reader
    (`require_confirm`, `_body_allowlist`, `_disclosure_config`) goes
    through this one function, so it is the single choke point to warn
    from -- the same reasoning `_merge_presets` uses for `builtin-ops`
    (#2308): one function, so one warning covers every downstream reader
    rather than teaching each of them to check for it separately.

    Policy chosen here is "warn and fall back", not "refuse": a malformed
    key unrelated to the one a caller actually needs must not block an
    unrelated publish, but the read failure is no longer silent -- it is
    named on stderr, once per process, before the default takes over.
    """
    global _CACHED_CONFIG
    try:
        return _CACHED_CONFIG  # type: ignore[has-type]
    except NameError:
        pass
    cfg: dict = {}
    d = Path.cwd().resolve()
    while True:
        candidate = d / ".supertool.json"
        if candidate.is_file():
            violation = _config_trust_violation(candidate)
            if violation is not None:
                # Skipped exactly like an absent file (this function's own
                # docstring) -- must NOT stop the walk here. The first cut
                # of this fix unconditionally `break`-ed after this whole
                # `if candidate.is_file():` block regardless of `violation`,
                # so a stray world-writable file anywhere on the walk
                # silently hid every config above it, including a
                # legitimately-owned one inside the SAME repo (caught in
                # self-review, oss:auditor spawn, with a reproduction:
                # `tests/test_publish_safety_config_trust_2366`'s
                # `test_an_untrusted_config_does_not_block_a_further_trusted_one_above_it`).
                # Falling through to the `.git`/parent walk below, matching
                # every sibling implementation
                # (`_supertool._load_config`, `presets/gitlab/_maintenance.py`,
                # `presets/worktree/_common.py`,
                # `presets/slack/_authorization.py`), none of which stop on
                # a violation either.
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
                    # UnicodeDecodeError is a ValueError, not an OSError, so
                    # it is caught by name rather than folded into the tuple
                    # above by inheritance -- the same gap
                    # `_supertool.py::_load_config` already closed for the
                    # sibling loader (#418): a `.supertool.json` that is not
                    # valid UTF-8 used to escape this clause entirely and
                    # crash the whole publish op, which is the opposite of
                    # the "warn and fall back, never refuse" policy this
                    # function's own docstring states (self-review finding
                    # on #2306, confirmed by the oss:auditor spawn).
                    sys.stderr.write(
                        f"WARNING: could not read {candidate} "
                        f"({exc.__class__.__name__}: {exc}) -- ignoring it. "
                        f"Every publish safety setting (disclosure, body "
                        f"allowlist, confirmation) falls back to its "
                        f"default, exactly as if this file set nothing at "
                        f"all.\n"
                    )
                # A found-and-TRUSTED file stops the walk here, whether it
                # parsed cleanly or not -- unlike the violation branch
                # above, a trusted-but-malformed file is not "absent",
                # matching every sibling implementation's own choice.
                break
        if (d / ".git").exists():
            break
        if d.parent == d:
            break
        d = d.parent
    _CACHED_CONFIG = cfg  # type: ignore[name-defined]
    return cfg


# --- file:// allowlist ---------------------------------------------------

_DEFAULT_BODY_ALLOWLIST: tuple[str, ...] = (
    ".max/",
    "drafts/",
    "posts/",
    "blog/",
)


def _body_allowlist() -> tuple[Path, ...]:
    """Allowlist of dirs (resolved absolute) where publish bodies may live.

    Additive sources:
      1. `_DEFAULT_BODY_ALLOWLIST`
      2. `$SUPERTOOL_PUBLISH_BODY_ALLOWLIST` env (os.pathsep-separated:
         `:` on POSIX, `;` on Windows)
      3. `"publish_body_allowlist": [...]` in `.supertool.json`
    """
    paths = list(_DEFAULT_BODY_ALLOWLIST)
    extra = os.environ.get("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", "")
    if extra:
        # os.pathsep — `:` on POSIX, `;` on Windows. A literal `:` would
        # mangle Windows paths like `C:\Temp\drafts` at the drive letter.
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
    """Resolve a `file://` or bare path to a Path inside the allowlist.

    Exits cleanly (sys.exit(2)) when the resolved path escapes the allowlist.
    Caller is responsible for verifying the path exists.
    """
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


# --- confirmation gate ---------------------------------------------------

def require_confirm(action: str, preview: str, *, force: bool = False) -> None:
    """Bail unless explicitly opted out — blocks single-shot LLM publish.

    Opt-out (any of):
      1. `force=True` per-call (existing `|force` suffix)
      2. `SUPERTOOL_NO_PUBLISH_CONFIRM=1` env
      3. `"no_publish_confirm": true` in `.supertool.json`
    """
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
        f"  To proceed: append |force, set SUPERTOOL_NO_PUBLISH_CONFIRM=1,\n"
        f"  or add `\"no_publish_confirm\": true` to .supertool.json.\n"
    )
    sys.exit(2)


# --- authorship disclosure (#2042) ---------------------------------------

_DEFAULT_DISCLOSURE_TEXT = "[AI-generated]"


def _disclosure_config() -> tuple[bool, str]:
    """(enabled, marker text) -- env var > `.supertool.json` > default (on).

    Same opt-out lookup order as `require_confirm` and the body allowlist.
    ASCII enforced here, not merely assumed: `_DEFAULT_DISCLOSURE_TEXT` is
    ASCII by construction, and a `publish_disclosure_text` override that is
    not ASCII is rejected in favor of the default rather than passed
    through -- both are printed and sent, and a console reading a
    non-UTF-8 codepage kills the process at the `print` after the work
    already happened (#2066).
    """
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
    """Append a machine-authorship marker to a forge (pull request/issue) body.

    Returns `(new_body, state)`:
      "appended"        -- the marker is in `new_body`, freshly added.
      "already-present" -- the configured marker was already a substring of
                            `body`; `new_body == body`, unmodified. This is
                            what keeps `gh-pr-edit` idempotent: a body update
                            routinely starts from the published body (which
                            already carries the marker) and corrects it, and
                            an unconditional append would stack a second copy
                            on every such edit (#2100).
      "suppressed"       -- opted out via env or `.supertool.json`; `new_body
                            == body`.

    Deliberately a second entry point rather than a `max_len` of `None` on
    `apply_disclosure`: a pull request or issue body has no analogue of
    Bluesky's 300-char ceiling, so the `dropped` state has nothing to mean
    here, and reusing that state's plumbing for a check it can never trigger
    would be the wrong shape to carry forward.

    Callers that also parse the body for a closing keyword (`gh-pr-create`'s
    `Closes #N` gate) must call this AFTER that parse, not before: the
    marker text does not contain the substring `Closes`, but the order is a
    real constraint the issue names explicitly, not a coincidence to rely on
    silently (#2100).
    """
    enabled, marker = _disclosure_config()
    if not enabled:
        return body, "suppressed"
    if marker in body:
        return body, "already-present"
    separator = "\n\n"
    return body + separator + marker, "appended"


def apply_disclosure(body: str, *, max_len: Optional[int] = None) -> tuple[str, str]:
    """Append a machine-authorship marker to a publish body.

    Returns `(new_body, state)`:
      "appended"   -- the marker is in `new_body`.
      "suppressed" -- opted out via env or `.supertool.json`; `new_body == body`.
      "dropped"    -- on by default but did not fit within `max_len`
                      (Bluesky's 300-char cap makes this concrete);
                      `new_body == body`, unmodified rather than truncated.

    On by default: with no configuration at all, `state` is "appended".
    Silence about authorship must be a decision somebody recorded -- in the
    environment or `.supertool.json` -- never a side effect of the body
    merely being long, which is why "dropped" is its own state rather than
    silently truncating the marker or the body to make room.

    Called before the caller builds its confirmation preview, so a human
    confirming a publish sees the disclaimer that will actually be attached
    (one of the issue's open questions) rather than the bare body.
    """
    enabled, marker = _disclosure_config()
    if not enabled:
        return body, "suppressed"
    separator = "\n\n"
    tagged = body + separator + marker
    if max_len is not None and len(tagged) > max_len:
        return body, "dropped"
    return tagged, "appended"


# --- token file mode check -----------------------------------------------

def check_token_file_mode(path: Path) -> None:
    """Exit cleanly if a credential file is group/world-readable.

    Mirrors `ssh`'s refusal to use an insecure key. Caller passes a Path —
    we no-op if the file doesn't exist (caller surfaces the right error).
    """
    try:
        st = os.stat(path)
    except OSError:
        return
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        sys.stderr.write(
            f"ERROR: token file {path} has loose permissions ({oct(mode)}).\n"
            f"  Tighten with: chmod 600 {path}\n"
            f"  (Other users on this machine can currently read your token.)\n"
        )
        sys.exit(2)
