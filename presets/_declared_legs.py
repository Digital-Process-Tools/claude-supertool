#!/usr/bin/env python3
"""The second, independent leg count — one source, three ops (#804/#837).

Every CI summary supertool prints tallies the jobs GitHub handed back. That
tally cannot detect a missing *input*: `9 total: 5 passed, 0 failed, 4 pending`
sums perfectly and is externally short of a fourteen-leg matrix. Only a second
count of what *should* have arrived can catch it, which is what this module is.

**The dip is measured, not assumed.** Run 30997282630 of this repo, one failed
leg re-run with `gh run rerun --failed` while sampling every ~2s::

    15:57:31  run_view=0   latest=0   all_distinct=14
    15:57:39  run_view=9   latest=9   all_distinct=14
    15:57:49  run_view=14  latest=14  all_distinct=14

`gh run view <id> --json jobs` requests
`repos/{o}/{r}/actions/runs/{id}/jobs?per_page=100` with **no filter**, and
GitHub's default for that endpoint is `filter=latest` — observed directly with
`GH_DEBUG=api`. It is therefore the same source #724 caught dipping for
`gh-pr:status`, not merely a similar one. For ~18s after a partial re-run it
returns a strict subset of the matrix, and both `gh-run` and `gh-branch` read
it as the whole.

**What holds is `filter=all`.** A previous attempt's job rows are history and
cannot be withdrawn, so the set of *distinct job names across every attempt*
only ever grows — the `all_distinct` column above, flat at 14 through a sample
where the other two read 0. Distinct names and not `total_count`: under
`filter=all` that counts every row of every attempt (28 across two attempts of
a fourteen-leg matrix) and would declare a complete tally short by 14, which is
a false alarm, which is how a disclosure gets ignored.

Two ways this reads low, both in the safe direction — a floor that is too low
under-claims a shortfall, it never invents one:

* a run whose matrix genuinely gained legs between attempts (needs a matrix
  computed at runtime; the workflow file is fixed per commit);
* more than 100 job records across all attempts, where the first page
  truncates the name set.

`None` on every failure, never a fallback number: a guessed floor can sit under
the real one, which is this defect wearing a fix's clothes.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Sequence

# Extra `gh api` calls one render will pay for. Each reconciled run is one
# round trip, and an op that answers a status question must not turn into a
# fan-out; past this many runs the answer is "unestablished", which is honest
# and bounded, rather than a slow correct one.
MAX_RECONCILED_RUNS = 4

_OWNER_REPO = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)(?:/|$)")


def owner_repo(url: str) -> tuple[str, str]:
    """`(owner, repo)` parsed from any GitHub URL, `("", "")` when unparseable.

    One parser for the PR URL (`.../pull/N`), the run URL
    (`.../actions/runs/N`) and `nameWithOwner` — the three shapes the three
    callers each already hold, so none of them buys an extra call to learn
    which repository it is talking about.
    """
    text = str(url or "").strip()
    if not text:
        return ("", "")
    if "://" not in text:
        parts = text.split("/")
        if len(parts) == 2 and all(parts):
            return (parts[0], parts[1])
        return ("", "")
    m = _OWNER_REPO.match(text)
    return (m.group(1), m.group(2)) if m else ("", "")


def _run(argv: list[str], timeout: int = 15):
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


def legs_for_run(owner: str, repo: str, run_id) -> list[str] | None:
    """Distinct job names one run declares, or `None` when unestablished.

    First-seen order is kept so the names read like the matrix rather than
    like a set.
    """
    if not owner or not repo or not str(run_id or "").strip():
        return None
    try:
        r = _run([
            "gh", "api",
            f"repos/{owner}/{repo}/actions/runs/{run_id}"
            "/jobs?filter=all&per_page=100",
        ])
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return None
    seen: list[str] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        name = str(j.get("name") or "?")
        if name not in seen:
            seen.append(name)
    return seen


def legs_for_runs(owner: str, repo: str,
                  run_ids: Sequence) -> tuple[int | None, list[str]]:
    """`(total, names)` across several runs — `(None, [])` if any is unreadable.

    Partial reconciliation is not offered on purpose. A declared count summed
    over *some* of the runs is a smaller number than the truth, and a smaller
    declared count is exactly what makes a short tally look complete.
    """
    ids = [str(r) for r in run_ids if str(r or "").strip()]
    if not ids or len(ids) > MAX_RECONCILED_RUNS:
        return (None, [])
    names: list[str] = []
    for rid in ids:
        found = legs_for_run(owner, repo, rid)
        if found is None:
            return (None, [])
        names.extend(found)
    return (len(names), names)


def reconcilable(attempt: object) -> bool:
    """True when a second source can differ from the tally at all.

    On attempt 1 `filter=all` *is* `filter=latest` — same attempt, same rows —
    so the call buys a number that is equal to the one it would check by
    construction. Measured on three attempt-1 runs of this repo
    (30997282630, 30962154243, 30939825226): `latest=14, all_rows=14,
    all_distinct=14`. A prior attempt is what puts names in `all` that
    `latest` has dropped.

    An unreadable or absent `attempt` reconciles: paying one call is the
    cheap error, and skipping on a field that failed to arrive would make the
    silence depend on a field nobody read.
    """
    try:
        return int(attempt) != 1
    except (TypeError, ValueError):
        return True


def missing_names(declared: Sequence[str], found: Sequence[str]) -> list[str]:
    """Declared leg names with the found ones removed, duplicates respected."""
    remaining: dict[str, int] = {}
    for n in found:
        remaining[n] = remaining.get(n, 0) + 1
    out: list[str] = []
    for name in declared:
        if remaining.get(name, 0):
            remaining[name] -= 1
        else:
            out.append(name)
    return out
