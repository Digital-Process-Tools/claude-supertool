#!/usr/bin/env python3
"""Maintenance-window lookup for `gl-job`'s container-kill classifier (#645).

Every DVSI runner host runs a cron cleanup+reboot at a known time (e.g.
`00:00 UTC`), which SIGTERMs/SIGKILLs whatever container is still running.
`gl-job:<id>:fail` used to report those deaths as a bare `exit code 137` /
`143`, indistinguishable from genuine OOM — filed live after five container
deaths across four MRs were misdiagnosed as "the fleet degrading under
load" and a whole board declared unreliable for four hours (#645).

Config shape, in `.supertool.json`, keyed by op name like every other
GitLab-side knob (see `docs/presets/gitlab.md`'s "Per-job failure patterns"
section for the sibling convention) — not a new top-level namespace:

    {
      "gl-runners": {
        "maintenance": {"window": "00:00 UTC", "duration": "5m"},
        "runners": {
          "dptools-runner-4": {"maintenance": {"window": "03:30 UTC"}},
          "hercule":          {"maintenance": null}
        }
      }
    }

`maintenance` at the top of `gl-runners` is the fleet-wide default.
`runners.<key>.maintenance` overrides it for one host — keyed by the
runner's own `description` (falling back to its numeric `id` as a string)
— and an explicit `null` opts that host out entirely, distinct from the
host simply having no entry. Declared in version control rather than
parsed out of the runner description string itself: that string is what
`gl-runners:queue` already keys its own matching on (`dptools-runner-7` vs
`dptools-runner-7 V2` is a real distinction there, #613), and embedding
config inside it would invite exactly the corruption this issue exists to
prevent.

Three states out of every lookup here, never two — the same discipline
this whole codebase keeps re-deriving (`CLAUDE.md`'s "The defect this
codebase keeps having"): a malformed `window`/`duration`, an unresolvable
runner key, or an unparseable `finished_at` must all read as "could not
tell", and "could not tell" must never render identically to "verified
clean". `maintenance_note` returns "" for the ones that mean "nothing to
add" (no container exit code, or no window declared at all) and a
disclosed line for the ones that mean "there was something to check and
here is what happened" — inside the window, outside it, or the clock
itself could not be read. Never raises: a maintenance-window lookup is not
allowed to be the reason a job classifier crashes.

Scope guard, enforced by the caller (`presets/gitlab/job.py`), not here:
only container-level exit codes qualify. A PHPUnit failure at 00:00:03 UTC
is still a PHPUnit failure — the window explains a killed container, it
never softens a real test result.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from datetime import datetime, time, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _untrusted  # noqa: E402  (a runner description is GitLab-hosted text)


#: SIGTERM (143), SIGKILL (137) and SIGSEGV-adjacent container kill (139) —
#: what a host's own cleanup/reboot script produces when it stops a
#: container mid-job. Never widened to a bare "any nonzero exit": that is
#: exactly the class of real failure (a script's own `exit 1`) this must
#: not soften.
CONTAINER_EXIT_CODES = frozenset({137, 139, 143})

_EXIT_CODE_RE = re.compile(r'\bexit code (\d+)\b', re.IGNORECASE)

# "00:00 UTC", "3:30 UTC" — one clock, one zone. No other timezone spelling
# is accepted; a fleet on a different zone writes its window in UTC too,
# the same way GitLab's own timestamps do.
_WINDOW_RE = re.compile(r'^\s*(\d{1,2}):(\d{2})\s*UTC\s*$', re.IGNORECASE)
# "5m", "30s", "1h" — amount plus a single unit letter. No bare integer:
# a duration with no unit is exactly the ambiguity #645's own config sketch
# never had to resolve, so it is treated as unparseable rather than guessed.
_DURATION_RE = re.compile(r'^\s*(\d+)\s*([smh])\s*$', re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

_CACHED_CONFIG: "dict | None" = None


def _config_trust_violation(candidate: Path) -> "str | None":
    """POSIX ownership/permission guard, mirroring `_supertool._load_config`'s
    own #695 hardening.

    Presets cannot import the core module (`_supertool.py` imports presets,
    not the other way — a preset importing it back would be circular), so
    this walk-up loader re-implements the same trust check rather than
    sharing it: a group/world-writable `.supertool.json`, or one owned by a
    different user, can be rewritten by another local account between the
    moment it was reviewed and the moment `gl-job:ID:fail` reads it for a
    maintenance window — the same TOCTOU shape #695 closed for the loader
    every other op goes through. POSIX-only: `st_uid` and the write bits
    are meaningless on Windows, so this returns `None` (trusted)
    unconditionally there rather than fabricate a check with no signal
    behind it. Root is treated as trusted, matching `_config_trust_violation`.
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


def load_config() -> dict:
    """Walk up from cwd for `.supertool.json`, stopping at the nearest
    `.git` ancestor; cached per process.

    Two limits, matching `_supertool._load_config`'s own #695 hardening —
    a config is exactly as trusted as the project that owns it, and must
    not reach OUTSIDE that project: opening a subdirectory of an otherwise
    unrelated tree (a shared `/tmp` extraction, a CI checkout dir with a
    stray ancestor config) must not silently pick up a `.supertool.json`
    that governs nothing the caller actually opened.

    * the walk stops once it reaches a directory containing `.git` — the
      repo root — rather than continuing to `/`; a caller not inside a git
      repo at all keeps walking to `/`, since there is no repo boundary;
    * each candidate is checked with `_config_trust_violation` first — a
      file that is group/world-writable, or not owned by the caller (or
      root), is skipped exactly like a malformed one.

    Warn on stderr and fall back to `{}` for anything unreadable, not
    owned by the caller, or malformed — never raise. A maintenance-window
    lookup runs inside a job classifier's own hot path; failing the read
    must never fail the call.
    """
    global _CACHED_CONFIG
    if _CACHED_CONFIG is not None:
        return _CACHED_CONFIG
    cfg: dict = {}
    d = Path.cwd().resolve()
    while True:
        candidate = d / ".supertool.json"
        if candidate.is_file():
            violation = _config_trust_violation(candidate)
            if violation is not None:
                sys.stderr.write(
                    f"WARNING: skipped {candidate} ({violation}) -- "
                    f"ignoring it for gl-runners maintenance windows.\n"
                )
            else:
                try:
                    parsed = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        cfg = parsed
                    else:
                        sys.stderr.write(
                            f"WARNING: {candidate} does not hold a JSON "
                            f"object (got {type(parsed).__name__}) -- "
                            f"ignoring it for gl-runners maintenance "
                            f"windows.\n"
                        )
                    _CACHED_CONFIG = cfg
                    return cfg
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    sys.stderr.write(
                        f"WARNING: could not read {candidate} "
                        f"({exc.__class__.__name__}: {exc}) -- ignoring it "
                        f"for gl-runners maintenance windows.\n"
                    )
        if (d / ".git").exists():
            break
        if d.parent == d:
            break
        d = d.parent
    _CACHED_CONFIG = cfg
    return cfg


def resolve_window(cfg: dict, runner_description, runner_id) -> "dict | None":
    """The `maintenance` dict that applies to this runner, or `None`.

    Resolution order: an explicit `runners.<key>.maintenance` (including an
    explicit `null`, which opts the host out and is returned as `None`
    without falling through) beats the fleet-wide default at the top of
    `gl-runners`; a runner with no entry, or an entry with no `maintenance`
    key at all, falls through to that default. `key` tries `description`
    first, then `str(id)` — the same precedence the issue settled on,
    because description is what a human reads and edits, while id is
    unique but unreadable.

    A per-runner override MERGES onto the fleet default rather than
    replacing it wholesale — `{"window": "03:30 UTC"}` alone still carries
    the fleet's `duration` (self-review finding: the docs' own canonical
    example, a `window`-only override, previously parsed to a 0-second
    window and silently never fired, because `parse_window` defaults a
    missing `duration` to `"0m"` with nothing distinguishing that from a
    deliberately instantaneous window). Only individual keys present in
    the override win; anything it omits still comes from the default.
    """
    gl_cfg = cfg.get("gl-runners")
    if not isinstance(gl_cfg, dict):
        return None
    default = gl_cfg.get("maintenance")
    runners = gl_cfg.get("runners")
    entry = None
    if isinstance(runners, dict):
        for key in (runner_description, str(runner_id) if runner_id is not None else None):
            if key and key in runners:
                entry = runners[key]
                break
    if isinstance(entry, dict) and "maintenance" in entry:
        override = entry.get("maintenance")
        if override is None:
            return None  # explicit opt-out — never falls back to the default
        if not isinstance(override, dict) or not isinstance(default, dict):
            # Either shape is not a plain dict to merge -- hand the override
            # through as-is; `parse_window` is what judges whether it is
            # well-formed, not this function.
            return override
        merged = dict(default)
        merged.update(override)
        return merged
    return default


def parse_window(window_cfg) -> "tuple[time, int] | None":
    """`(start, duration_seconds)`, or `None` for anything not shaped like a
    declared window.

    Deliberately permissive about what counts as "not shaped like one" —
    absent, wrong JSON type, an out-of-range clock, a duration with no
    recognised unit — because the caller's whole point is that an
    unparseable declaration must read as "no window", never as an error
    that interrupts a job classifier (#645's own explicit requirement).
    """
    if not isinstance(window_cfg, dict):
        return None
    window = window_cfg.get("window")
    duration = window_cfg.get("duration", "0m")
    if not isinstance(window, str):
        return None
    m = _WINDOW_RE.match(window)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    if not isinstance(duration, str):
        return None
    dm = _DURATION_RE.match(duration)
    if not dm:
        return None
    amount, unit = int(dm.group(1)), dm.group(2).lower()
    return time(hour, minute), amount * _UNIT_SECONDS[unit]


def _parse_finished_at(finished_at) -> "datetime | None":
    """GitLab's own ISO-8601 `finished_at`, normalised to UTC — or `None`
    when it is missing or not parseable as one."""
    if not isinstance(finished_at, str) or not finished_at:
        return None
    try:
        dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def died_in_window(finished_at, start: time, duration_s: int) -> "bool | None":
    """Did `finished_at` land inside `[start, start + duration_s)` UTC?

    `None` — not a guessed `False` — when `finished_at` cannot be read as a
    timestamp at all: the third state, same reasoning as `_is_past` in
    `presets/gitlab/job.py` for a malformed expiry.
    """
    dt = _parse_finished_at(finished_at)
    if dt is None:
        return None
    start_s = start.hour * 3600 + start.minute * 60 + start.second
    end_s = start_s + duration_s
    day_s = dt.hour * 3600 + dt.minute * 60 + dt.second
    if end_s <= 86400:
        return start_s <= day_s <= end_s
    return day_s >= start_s or day_s <= (end_s - 86400)


def is_container_exit(exit_code) -> bool:
    """True for an exit code a host's own reboot/cleanup script can produce."""
    return isinstance(exit_code, int) and exit_code in CONTAINER_EXIT_CODES


def exit_code_from_texts(texts) -> "int | None":
    """The first `exit code N` named in `texts` (the boilerplate lines
    `gl-job` already discounted), or `None` if none says so."""
    for text in texts:
        m = _EXIT_CODE_RE.search(text)
        if m:
            return int(m.group(1))
    return None


def _format_window(start: time, duration_s: int) -> str:
    if duration_s % 60 == 0:
        dur = f"{duration_s // 60}m"
    else:
        dur = f"{duration_s}s"
    return f"{start.strftime('%H:%M')} UTC +{dur}"


def maintenance_note(exit_code, finished_at, runner_description, runner_id) -> str:
    """The extra block `gl-job:ID:fail` appends for a container-level exit
    that a declared maintenance window can speak to. `""` when there is
    truly nothing to say — not a container exit code, or no window
    resolvable at all for this runner (fleet default included) — which is
    the same "nothing new here" state a caller already prints without this
    note.

    A *declared but malformed* window is a different, third state — never
    collapsed into the same silent `""` (self-review finding: a typo'd
    `window`/`duration` used to render byte-identical to "no window
    configured", so an operator had no way to discover their declaration
    never took effect). It gets its own disclosed line instead, distinct
    from both a real verdict and true silence.

    A hint, never a verdict on its own: "inside the window" still only
    says RETRY for a container-level kill, and #645's scope guard (only
    `is_container_exit` codes reach this function at all) is what keeps a
    real test failure from ever being softened by it.
    """
    if not is_container_exit(exit_code):
        return ""
    cfg = load_config()
    window_cfg = resolve_window(cfg, runner_description, runner_id)
    if window_cfg is None:
        return ""
    parsed = parse_window(window_cfg)
    if parsed is None:
        return (
            f"A maintenance window is declared for this runner but could "
            f"not be parsed ({window_cfg!r}) -- treated as no window: exit "
            f"code {exit_code} is not explained by it. Check "
            f"gl-runners.maintenance / gl-runners.runners.<key>.maintenance "
            f"in .supertool.json."
        )
    start, duration_s = parsed
    window_desc = _format_window(start, duration_s)
    host = f" on {_untrusted.flat(runner_description)}" if runner_description else ""
    verdict = died_in_window(finished_at, start, duration_s)
    if verdict is None:
        return (
            f"Maintenance window declared{host} ({window_desc}), but this "
            f"job's finish time could not be read — could not tell whether "
            f"exit code {exit_code} landed inside it."
        )
    died_str = _untrusted.flat(str(finished_at))
    if verdict:
        return (
            f"Died {died_str}{host}, inside the declared maintenance window "
            f"({window_desc}).\n"
            f"Verdict: RETRY — scheduled cleanup, not memory pressure and "
            f"not code."
        )
    return (
        f"Died {died_str}{host}, outside the declared maintenance window "
        f"({window_desc}) — exit code {exit_code} is not explained by "
        f"scheduled maintenance."
    )
