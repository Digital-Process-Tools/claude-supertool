#!/usr/bin/env python3
"""The open-PR board fetch and the default-branch head/run read, shared by
radar's GitHub tier and the dashboard op (#958).

`presets/watch/tiers/gh_prs.py` (`live_open_prs`) and
`presets/dashboard/dashboard.py` (`collect_board`/`collect_default`) each
grew their own copy of two reads: "spawn `gh pr list`, parse the JSON" and
"read the default branch's head commit and run list". Not the whole board --
the two callers render differently on purpose, one a delta view and one a
state view, and that stays where it is. What moved here is the part where a
fixed defect in one copy would not automatically fix the other: the mechanical
fetch, and the three-state contract underneath it.

**A board that could not be fetched must not render as an empty one, and a
default-branch read that failed must not render as though nothing was
asked.** Both functions below hand back an explicit error string on any
failure; an empty *success* (`[]`, `data == []`) is a different, real answer
and stays distinguishable from it -- the same distinction #859's radar tier
and #615/#846's default-branch board both already made independently, now
made once.

What is deliberately **not** here, because it is judgement rather than fetch:

  * **classifying *why* the board fetch failed** -- "no credentials" vs
    "rate limited" vs "the request never landed" -- stays with the caller.
    Radar's GitHub tier needs that distinction to decide `RadarUnreachable`
    vs `RadarUnconfigured` vs a plain `RadarError` (different retry
    semantics); the dashboard op needs none of it, only a string for its
    `Section`. Forcing one taxonomy over both would be the bend `gh_prs.py`'s
    own module docstring warns against making `gl_mrs` do for GitHub.
  * **composing the default-branch verdict** -- selecting the runs on the
    head SHA, fetching each run's jobs, reconciling declared legs, scoping
    the green, rendering the sentence -- stays with `gh-branch`'s own
    functions (`runs_on_sha`, `_jobs_for`, `_reconcile`, `scope_for`,
    `verdict`, ...), which both callers already compose directly rather than
    through a second copy. Radar composes them with a `ThreadPoolExecutor`
    and no `declared_pair`; the dashboard composes them serially and with
    `declared_pair` (#846/#1959). That is a real, current divergence between
    the two callers, not an oversight this file papers over -- see #958's
    pull request for why it is reported rather than force-unified here.
  * **the per-PR leg reconciliation** -- `pr._reconcile_checks` -- already
    has one copy (#724/#804/#837) and neither caller here re-derives it.

Precedent: `presets/watch/tiers/_snapshot.py` (#859), extracted for the same
reason -- "a second copy is how a fixed defect comes back" -- and the same
shape: plain functions over plain data, no caller-specific exception
hierarchy baked in.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any


def run_pr_list(cmd: list, timeout: int = 30) -> tuple:
    """Run a `gh pr list --json ...` argv and parse it.

    `(data, error, returncode, raw_stderr)`. The argv is the caller's own --
    filters, `--json` field set, `--limit` -- built by whichever of
    `github/prs.py:_build_list_cmd` or the dashboard's own fixed field list
    the caller needs; this function only spawns it and parses what comes
    back.

    `data` is `None` on any failure and a `list` (possibly empty) on success
    -- never `[]` for a call that did not answer. `returncode` is the
    subprocess's exit code, or `None` when the process never finished (it
    could not be spawned, or it was killed by the timeout): a caller that
    needs to tell "no credentials" (a specific exit code) from "the request
    never landed" (no exit code at all) needs that distinction kept, not
    collapsed into one error string. `returncode == 0` alongside `data is
    None` means `gh` itself succeeded but the payload was not usable (bad
    JSON, or JSON that was not a list) -- a caller classifying by exit code
    must check for that case before treating `0` as "no failure to explain".

    `error` is a ready-to-print message for a caller that only needs to say
    the fetch failed. `raw_stderr` is the unprocessed, stripped stderr text
    (`""` when there was none or the failure was not a nonzero exit) for a
    caller that needs to classify *why* a nonzero exit happened -- radar's
    GitHub tier tells "no credentials" from "rate limited" from "the request
    never landed" by pattern-matching this text together with `returncode`,
    and reformatting it into `error` first would make that matching fragile
    against wording this function might someday change.
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, encoding="utf-8",
                                errors="replace")
    except subprocess.TimeoutExpired:
        return None, f"gh pr list timed out after {timeout}s", None, ""
    except FileNotFoundError:
        # Its own arm, not folded into the generic `OSError` one below:
        # collapsing it produced "gh pr list failed: [Errno 2] No such file
        # or directory: 'gh'" here, a worse message than the dashboard's own
        # pre-extraction `_json_cmd` gave for exactly this case
        # (`f"{argv[0]} not found on PATH"`) -- caught in review (#958).
        binary = cmd[0] if cmd else "gh"
        return None, f"{binary} not found on PATH", None, ""
    except OSError as exc:
        return None, f"gh pr list failed: {exc}", None, ""
    if result.returncode != 0:
        raw = (result.stderr or "").strip()
        detail = raw or (result.stdout or "").strip()
        first = detail.splitlines()[0] if detail else "no output"
        return (None, f"gh pr list exited {result.returncode}: {first}",
                result.returncode, raw)
    try:
        data = json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return (None, "gh pr list returned unparseable JSON",
                result.returncode, "")
    if not isinstance(data, list):
        return (None, "gh pr list did not return a list",
                result.returncode, "")
    return data, "", result.returncode, ""


def head_and_runs(branch_mod: Any, ref: str) -> tuple:
    """`(sha, age_secs, runs, error)` -- the two reads default-branch health
    needs before any verdict can be composed.

    Both `gh-branch`'s own `_head_commit` and `_run_list` already carry their
    own three-state contract (`error == ""` iff the read succeeded); this
    only sequences the two calls the same way every caller was already
    sequencing them by hand, and keeps the same guarantee across the join:
    `runs is None` iff `error` is set. The run list is not asked for when the
    head commit could not be resolved -- there is no SHA to select runs
    against, and asking anyway would spend a call to learn nothing.

    An **empty** run list (`runs == []`) is a real, established fact -- the
    commit truly has no run yet, which is itself a verdict (#615), not an
    absence of information -- and stays distinguishable from `runs is None`
    (the read itself failed).
    """
    sha, age, err = branch_mod._head_commit(ref)
    if err:
        return sha, age, None, err
    runs, err = branch_mod._run_list(ref)
    if err or runs is None:
        return sha, age, None, err or "run list unreadable"
    return sha, age, runs, ""
