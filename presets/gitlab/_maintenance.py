#!/usr/bin/env python3



















































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
import _untrusted  







CONTAINER_EXIT_CODES = frozenset({137, 139, 143})

_EXIT_CODE_RE = re.compile(r'\bexit code (\d+)\b', re.IGNORECASE)




_WINDOW_RE = re.compile(r'^[ \t]*(\d{1,2}):(\d{2})[ \t]*UTC[ \t]*\Z', re.IGNORECASE)



_DURATION_RE = re.compile(r'^[ \t]*(\d+)[ \t]*([smh])[ \t]*\Z', re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

_CACHED_CONFIG: "dict | None" = None


def _config_trust_violation(candidate: Path) -> "str | None":















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
            return None  
        if not isinstance(override, dict) or not isinstance(default, dict):



            return override
        merged = dict(default)
        merged.update(override)
        return merged
    return default


def parse_window(window_cfg) -> "tuple[time, int] | None":









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

    return isinstance(exit_code, int) and exit_code in CONTAINER_EXIT_CODES


def exit_code_from_texts(texts) -> "int | None":


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
