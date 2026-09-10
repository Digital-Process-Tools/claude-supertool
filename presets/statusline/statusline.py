#!/usr/bin/env python3
"""`statusline` — render a bar for Claude Code's `statusLine` hook (#1850).

Reads the hook's JSON payload on stdin (`workspace.current_dir`, model info,
...) and prints one line built from the segments named in
`ops.statusline.groups` in `.supertool.json`. **The render path never makes a
network call.** A network-backed op (currently `gh-pr`) publishes its own
check tally as a side effect of running normally — see
`presets/_statusline_fragments.py` — and this op only ever reads what was
published, rendering an explicit `unknown` when nothing has been published
yet in this worktree. That is the *expected* state, not an error: the harness
runs this after every assistant message, and a developer may not have run
`gh-pr` at all yet this session.

Two independent axes, settled across #1850's design discussion:

* **Render** is width only — a local op computes live and prints a compact
  line (`git-status`'s statusline segment here re-implements the git
  plumbing directly rather than reusing `presets/git/status.py`'s
  print-oriented `main()`, which is a scoping decision, not an oversight —
  see docs/presets/statusline.md).
* **Data source** is what actually varies: local segments have a
  sub-second timeout *budget*, never a cache; network-backed segments have a
  *cache* (the fragment), never a live call.

Configuration is nested arrays of op/segment names, never shell, never a
literal separator entry — see the module docstring in
`presets/_statusline_fragments.py` and docs/presets/statusline.md for why::

    "statusline": [["session"], ["git-status"], ["gh-pr"]]

registered under `ops.statusline.groups`. The outer array is joined with the
group separator (`ops.statusline.group_sep`, default ` | `), each inner array
with the item separator (`ops.statusline.item_sep`, default ` · `) — both
configuration keys, never array entries, so a separator is only ever emitted
between things that actually rendered. Unconfigured refuses outright rather
than inventing a default; naming an item this op does not know how to render
also refuses, by name, rather than rendering it blank.

**Multi-line output (one row per printed line) is explicitly deferred** — see
"Not in scope" in docs/presets/statusline.md. This op prints exactly one
line today.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _statusline_fragments as _fragments  # noqa: E402


def _dig(obj: Any, *keys: str) -> Any:
    """Nested `.get()` that never raises — the stdin contract is not ours to
    trust (#1850's "Still open" item 5): read it defensively, never crash."""
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _read_stdin_json() -> Dict[str, Any]:
    """The hook's payload, or `{}` on anything short of a well-formed object.

    Never blocks on an interactive terminal (`isatty()`), and never raises —
    malformed or absent stdin renders `unknown` segments downstream instead
    of crashing the status line, which the harness would surface as stderr
    on every single turn (#1850's stdin-contract item).
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class _Ctx:
    __slots__ = ("stdin", "workspace_dir", "stale_secs", "git_budget")

    def __init__(self, stdin: Dict[str, Any], workspace_dir: str,
                 stale_secs: int, git_budget: float) -> None:
        self.stdin = stdin
        self.workspace_dir = workspace_dir
        self.stale_secs = stale_secs
        self.git_budget = git_budget


# ── segments ────────────────────────────────────────────────────────────
# Registered here rather than as a per-op manifest declaration (#1850's "opt-in
# per op" design) — that would require touching every preset's manifest schema
# and the `ops` renderer, which is out of scope for this slice. Deferred; see
# docs/presets/statusline.md "Not in scope".

def render_session(ctx: _Ctx) -> str:
    """Local, from the stdin blob alone — no process, no fragment."""
    name = _dig(ctx.stdin, "model", "display_name") or _dig(ctx.stdin, "model", "id")
    if not name:
        return "session unknown"
    return str(name)


_BRANCH_AB_RE = re.compile(r"^# branch\.ab \+(\d+) -(\d+)")


def _parse_git_status_v2(output: str) -> Tuple[Optional[str], int, int, int]:
    """`(branch, ahead, behind, dirty)` from `git status --porcelain=v2 --branch`.

    `branch` is `None` only when the header line itself never arrived (a
    genuine parse failure); an unborn/detached HEAD still names itself
    (`(detached)` — git's own spelling) and is not treated as an error.
    """
    branch: Optional[str] = None
    ahead = behind = dirty = 0
    for line in output.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):].strip()
        elif line.startswith("# branch.ab "):
            m = _BRANCH_AB_RE.match(line)
            if m:
                ahead, behind = int(m.group(1)), int(m.group(2))
        elif line.startswith("#"):
            continue
        elif line:
            dirty += 1
    return branch, ahead, behind, dirty


def render_git_status(ctx: _Ctx) -> str:
    """Live, budgeted (never cached) — #1850's "a budget, not a cache".

    A sub-second timeout, not the 15s `git-status` op itself is allowed
    (#1882) — a status line has no such patience, and blowing the budget
    renders the same `unknown` marker as any other read failure rather than
    stalling the bar.
    """
    try:
        r = subprocess.run(
            ["git", "-C", ctx.workspace_dir, "status", "--porcelain=v2", "--branch"],
            capture_output=True, text=True, timeout=ctx.git_budget,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "git:unknown"
    if r.returncode != 0:
        return "git:unknown"
    branch, ahead, behind, dirty = _parse_git_status_v2(r.stdout)
    if branch is None:
        return "git:unknown"
    parts = [branch]
    if ahead:
        parts.append(f"up{ahead}")
    if behind:
        parts.append(f"down{behind}")
    if dirty:
        parts.append(f"dirty{dirty}")
    return " ".join(parts)


def _fmt_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h"


def render_gh_pr(ctx: _Ctx) -> str:
    """The fragment `gh-pr` published as a side effect — never a live call.

    `not-published` (never run this session, the normal case) and
    `unreadable` (a fragment exists but could not be trusted) render
    different `unknown` sentences on purpose — see
    `presets/_statusline_fragments.read`'s docstring for why folding them
    together is exactly the defect class this feature exists to avoid.
    """
    data, state = _fragments.read("gh-pr", ctx.workspace_dir)
    if state == "not-published":
        return "checks: unknown (not run this session)"
    if state == "unreadable" or data is None:
        return "checks: unknown (fragment unreadable)"
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary:
        return "checks: unknown (fragment malformed)"
    age = _fragments.age_seconds(data)
    if age is not None and age > ctx.stale_secs:
        return f"checks: {summary} (stale {_fmt_age(age)})"
    return f"checks: {summary}"


SEGMENTS = {
    "session": render_session,
    "git-status": render_git_status,
    "gh-pr": render_gh_pr,
}


# ── configuration ───────────────────────────────────────────────────────

def _load_groups_config() -> Tuple[Optional[List[List[str]]], str]:
    """`(groups, error)` from `SUPERTOOL_GROUPS` (`ops.statusline.groups`).

    Unconfigured refuses by name rather than inventing a default (#1850) —
    the same rule `radar`'s tiers and `dashboard`'s lane prefix already
    follow for their own configuration keys.
    """
    raw = os.environ.get("SUPERTOOL_GROUPS", "").strip()
    if not raw:
        return None, (
            "ERROR: statusline is not configured — add ops.statusline.groups to "
            '.supertool.json, e.g. {"ops": {"statusline": {"groups": '
            '[["session"], ["git-status"], ["gh-pr"]]}}}. An unconfigured '
            "statusline refuses rather than inventing a default."
        )
    try:
        groups = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"ERROR: ops.statusline.groups is not valid JSON: {exc}"
    if not isinstance(groups, list) or not groups:
        return None, (
            "ERROR: ops.statusline.groups must be a non-empty list of groups "
            "(each group a list of segment names)"
        )
    for group in groups:
        if (not isinstance(group, list) or not group
                or not all(isinstance(x, str) and x for x in group)):
            return None, (
                "ERROR: ops.statusline.groups must be a list of lists of "
                f"non-empty strings — got a group of {group!r}"
            )
    return groups, ""


def _unknown_segments(groups: List[List[str]]) -> List[str]:
    return sorted({name for group in groups for name in group
                   if name not in SEGMENTS})


def _unknown_segment_refusal(unknown: List[str]) -> str:
    known = ", ".join(sorted(SEGMENTS))
    names = ", ".join(f"`{n}`" for n in unknown)
    return (f"ERROR: {names} declares no statusline render "
            f"(available: {known}) — refused rather than rendering it blank")


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    """`int(env)`, tolerating a float-shaped JSON number (self-review finding,
    #1850): `ops.<op>.<key>` reaches this subprocess through the core's
    generic env passthrough, which JSON-encodes a non-string value verbatim
    -- `"stale_secs": 300.0` is legal JSON and exports the literal string
    `"300.0"`, which a bare `int(...)` raises on and silently drops to the
    default with no warning anywhere. `int(float(raw))` accepts both shapes;
    a genuinely non-numeric value still falls back to `default`.

    `OverflowError` is caught alongside `ValueError`/`TypeError` (second
    self-review finding, #1850): `int(float("inf"))` raises that, not
    `ValueError`, and this call sits in `main()` ABOVE `render()`'s own
    per-segment isolation -- an exception here would abort the whole render
    instead of falling back, which is strictly worse than the bug this
    function exists to fix.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return default


def render(groups: List[List[str]], ctx: _Ctx, item_sep: str, group_sep: str) -> str:
    rendered_groups = []
    for group in groups:
        rendered_items = []
        for name in group:
            try:
                rendered_items.append(SEGMENTS[name](ctx))
            except Exception:
                # A segment must never take the whole bar down with it —
                # #1850's per-segment failure isolation.
                rendered_items.append(f"{name}:unknown")
        rendered_items = [s for s in rendered_items if s]
        if rendered_items:
            rendered_groups.append(item_sep.join(rendered_items))
    return group_sep.join(rendered_groups)


def main() -> int:
    groups, err = _load_groups_config()
    if err:
        print(err)
        return 1
    unknown = _unknown_segments(groups)
    if unknown:
        print(_unknown_segment_refusal(unknown))
        return 1

    stdin_data = _read_stdin_json()
    workspace_dir = (_dig(stdin_data, "workspace", "current_dir")
                      or _dig(stdin_data, "cwd") or os.getcwd())
    ctx = _Ctx(
        stdin=stdin_data,
        workspace_dir=workspace_dir,
        stale_secs=_int_env("SUPERTOOL_STALE_SECS", 300),
        git_budget=_float_env("SUPERTOOL_GIT_BUDGET_SECS", 1.5),
    )
    item_sep = os.environ.get("SUPERTOOL_ITEM_SEP") or " · "
    group_sep = os.environ.get("SUPERTOOL_GROUP_SEP") or " | "
    print(render(groups, ctx, item_sep, group_sep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
