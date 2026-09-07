#!/usr/bin/env python3
"""gh-issue — the GitHub half of `gl-issue` (#898), a radar tier scoped to one
GitHub issue rather than a population (#2369).

`presets/watch/tiers/gl_issue.py` is the template this module is built on --
same shape, same reasoning, GitHub's own APIs and event vocabulary substituted
where the two forges disagree. Registered by name like any other tier:

    {"ops": {"radar": {"radar_tiers": {"gh-issue": {}}}}}

    radar:gh-issue:2369

`_arg` carries the whole invocation string after `radar:`, so this tier
accepts either `"gh-issue:2369"` (the shape shown above, self-prefixed) or a
bare `"2369"`. A leading `#` is stripped either way. Anything else is refused
with the syntax restated, never guessed at.

Why no feed, no exclusions, no filter vocabulary
-------------------------------------------------

Same argument `gl-issue` makes for itself. This tier's population is "issue
#2369's own linked PRs", answered by one live GraphQL query scoped to the
issue's own number on every run -- there is nothing to discover between runs
that a fresh query would not already show. So there is no feed, and the
filter vocabulary `gh-prs` needs has nothing to filter here either: the one
parameter this tier takes is *which issue*, and that arrives through `_arg`.

What "related" means on GitHub, and why it is not GitLab's endpoint
----------------------------------------------------------------------

GitLab has a dedicated `related_merge_requests` endpoint. GitHub has no
equivalent REST or GraphQL collection -- "related" is not a first-class
relationship there. What GitHub *does* expose is narrower and more precise:
`Issue.closedByPullRequestsReferences`, the set of open PRs whose body
carries a working closing reference (`Closes #2369`, `Fixes #2369`, ...) to
this issue. `presets/github/issue.py`'s own `gh-issue` op already queries
this field for its "Linked PRs" section (#780/#782), and this tier reuses
that query and that parsing rather than re-deriving either: one GraphQL
shape, one place that decides what "linked" means, not two that can drift.

Deliberately narrower than a naive `gh pr list --search "#2369"` would be --
that matched a PR that only *mentions* the issue in prose exactly as
eagerly as one that actually closes it (#780 item 2, measured live on this
repo's own tracker: #774 read as linked to #770 when it only mentioned it).
A closing reference is a claim about intent to fix, not about being in the
same conversation, so it is the one relation this tier heals a watcher onto.

What this tier watches, and what it does not (yet)
----------------------------------------------------

For every one of the issue's linked PRs still `OPEN` (GitHub's GraphQL PR
state, not GitLab's lowercase `opened`), this tier ensures a `github-pr`
watcher over it -- source `github-pr`, `ONLY_EVENTS` below, which is the
*entire* event set `sources/github-pr/events.json` declares: checks going red
or green or starting, a review verdict, a comment, a merge, a close, a
conflict, or the watcher itself going unreachable. A closed or merged linked
PR is not watched: its own terminal event already fired before this tier's
report next runs, and asking `github-pr` to keep polling a merged PR would
just accumulate a dead poller.

**Deliberately out of scope for this issue**, named rather than silently
dropped (the same three `gl-issue`'s own docstring declines, and #2369's own
body repeats them for this half):

  * the `radar:gh-issue:N` arg-collision with `gh-prs`'s own filter parser
    (see "A caveat inherited..." below) -- pre-existing on `gl-issue`, and
    this tier inherits the same non-fix rather than solving it twice.
  * no per-tier/per-source policy markdown (`RADAR_POLICY`) -- deferred to
    #953/#898 alongside `gl-issue`'s own deferral.
  * no per-category `history.md` ledger -- needs the policy layer above it to
    mean anything, so it waits on the same decision.

What this tier does add on top of the shared per-PR heal, because it is what
a single-issue focus actually asks for: the **issue's own** state and label
set are tracked across runs (`tiers/_snapshot.py`, keyed on the issue
number), so a reopen and a label change are named on the board the run they
happen -- the same vocabulary `sources/github-issue-feed/events.json`
declares for a population feed (`issue_reopened`, `issue_labeled`,
`issue_unlabeled`, `issue_closed`) even though nothing here spawns that
source. It cannot: `github-issue-feed` is population-first by design, with
"deliberately no per-id companion" (its own module docstring, #525) -- its
`FILTER_KEYS` has no way to scope a REST issue listing to one number. So this
tier detects the same movement the way `gl-issue` does: by diffing this
run's live-fetched state against the previous run's snapshot, not by
spawning a feed poller that cannot be aimed at a single issue in the first
place. `issues_unreachable` has no analogue here for the same reason -- there
is no live feed poller whose reachability this tier could report.

A caveat inherited from how `_arg` reaches every registered tier
------------------------------------------------------------------

Radar passes the *same* `_arg` string to every tier configured in
`ops.radar.radar_tiers` (`tier_reports`, one loop, one `arg`). Registering
`gh-issue` alongside `gh-prs` and invoking `radar:gh-issue:2369` therefore
also hands `"gh-issue:2369"` to `gh-prs`'s own `resolve_filter`, which does
not recognise that shape and raises -- reported as a failure for that tier
alone, never fatal to this one. That is an existing property of the tier
contract, not something introduced here (`gl-issue`'s own docstring names
the identical collision against `gl-mrs`), and it is the reason a focused
radar session is expected to register `gh-issue` on its own rather than
beside a population tier that shares the argument slot.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_WATCH = _HERE.parent

sys.path.insert(0, str(_HERE))
import _radar_errors  # noqa: E402  (one copy, one class identity — #1847)

#: This tier's failure vocabulary, from the one copy `gl-issue` and `gh-prs`
#: already share. See `_radar_errors.py` for why it is `import`, never `_load`.
RadarError = _radar_errors.RadarError
RadarUnreachable = _radar_errors.RadarUnreachable
RadarUnconfigured = _radar_errors.RadarUnconfigured


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The `gh-issue` *op*'s own module — reused for its `_gh` CLI wrapper, its
# `_owner_repo`/`_closing_prs_query`/`_closing_pr_nodes`/`_check_tally`
# machinery (#780/#782/#815), and the `_untrusted`/`_auth_probe`/
# `_status_probe` names it already imports. One closing-reference query, not
# two, is the whole reason -- the same one `gl_issue.py` gives for reusing
# `presets/gitlab/issue.py`'s `_glab_api`.
issue_op = _load("radar_github_issue_op", _WATCH.parent / "github" / "issue.py")

# The snapshot store, shared with every other tier since #859: a previous
# board keyed by the population it describes, so a delta cannot lie.
snapshot = _load("radar_snapshot", _HERE / "_snapshot.py")

SOURCE = "github-pr"
SNAPSHOT_PREFIX = "supertool-radar-gh-issue"

# The *entire* event set `sources/github-pr/events.json` declares -- not a
# subset, unlike `gh-prs`'s own default heal (`watch(SOURCE, number, [])`,
# which asks the source for its own default rather than naming one here).
# This tier names its own copy because #2369 asks for the full vocabulary
# explicitly: a single-issue focus wants every signal on its own linked PRs,
# not the wider board's default filter.
ONLY_EVENTS = (
    "checks_failed", "checks_succeeded", "checks_pending",
    "review_approved", "review_changes_requested", "comment_added",
    "merged", "closed", "conflicts_appeared", "pr_unreachable",
)

# This tier takes no config beyond what radar itself injects (`_arg`,
# `_watch`). `quiet_when_healthy` is accepted for symmetry with every other
# tier even though `RADAR_QUIET_DEFAULT` below is False by default, the same
# reasoning `gl_issue.py` gives for carrying it.
RADAR_OPTIONS = {"quiet_when_healthy"}

# A focused board's report *is* the board — the same reasoning `gl_issue.py`
# gives: total silence on a quiet issue is indistinguishable from a radar
# that failed to run, and this tier exists precisely so an issue you are
# focused on is never silently unwatched.
RADAR_QUIET_DEFAULT = False

# `gh`'s auth-configuration exit code (#1568/#1823) -- measured on gh 2.50.0,
# checked before the message arms below because `gh` spells "no credentials"
# differently depending on whether it thinks it is interactive.
GH_RC_NO_CREDENTIALS = 4

# Substrings of `gh`'s own stderr that mean the request never landed. Its own
# copy rather than a shared one — `gh_prs.py`'s own `_UNREACHABLE_MARKERS`
# and `gl_issue.py`'s `TRANSPORT_MARKERS` both give the same reason: one
# CLI's transport vocabulary can widen without touching whatever else carries
# a copy of it.
TRANSPORT_MARKERS = (
    "not logged in",
    "http 401",
    "rate limit",
    "http 403",
    "dial tcp",
    "no such host",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "i/o timeout",
    "tls handshake timeout",
    "client.timeout",
    "error connecting to",
)


def _transport_unreachable(err: str) -> bool:
    """Does this `gh` stderr describe a request that never landed?"""
    low = err.lower()
    return any(marker in low for marker in TRANSPORT_MARKERS)


def _no_watch(source: str, scope: str, only: list[str] | None = None) -> str:
    """Fallback `_watch` when a caller supplied none.

    "failed", never "alive" — see `gl_issue._no_watch` for the argument: a
    tier asked to reconcile without a way to spawn cannot report coverage
    that nobody handed it a spawner to establish.
    """
    return "failed"


def parse_arg(arg: str) -> str:
    """The issue number `_arg` names, or `RadarError` with the syntax restated.

    Accepts `"gh-issue:2369"` (this tier's own name as a prefix, the shape
    the module docstring shows) or a bare `"2369"`. A leading `#` is stripped
    either way, since that is how an issue number is written everywhere else
    in this repo's own tracker prose.
    """
    s = (arg or "").strip()
    if s.lower().startswith("gh-issue:"):
        s = s[len("gh-issue:"):]
    s = s.strip().lstrip("#").strip()
    if not s.isdigit():
        raise RadarError(
            "radar: gh-issue tier requires an issue number, e.g. "
            f"radar:gh-issue:2369 (got _arg={arg!r})")
    return s


def _run(args: list[str], number: str, what: str,
        timeout: int = 15) -> "subprocess.CompletedProcess[str]":
    """One `gh` call. `RadarError`/`RadarUnreachable`/`RadarUnconfigured` on
    any failure, never a guessed-at payload. Mirrors `gl_issue._get`'s
    three-way split (unreachable / product error / parse failure), plus the
    `GH_RC_NO_CREDENTIALS` state `gh` has and `glab` does not (#1568/#1823).
    """
    try:
        result = issue_op._gh(args, timeout=timeout)
    except FileNotFoundError as exc:
        raise RadarUnreachable(f"gh not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RadarUnreachable(f"gh timed out {what} for issue #{number}") from exc
    except OSError as exc:
        raise RadarUnreachable(f"gh could not run: {exc}") from exc
    if result.returncode == 0:
        return result
    if result.returncode < 0:
        # Not a finished answer — same predicate as `gh_prs._query`'s own
        # arm: the sign of the return code is the whole predicate, and an
        # empty stderr here must not render as "gh answered nothing wrong".
        err = issue_op._untrusted.flat((result.stderr or "").strip()) or "unknown error"
        raise RadarUnreachable(
            f"gh did not finish before it answered {what} for issue "
            f"#{number} (returncode {result.returncode}): {err}")
    err = issue_op._untrusted.flat((result.stderr or "").strip()) or "unknown error"
    if result.returncode == GH_RC_NO_CREDENTIALS:
        # Checked before the message arms below, because `gh` spells this
        # differently depending on whether it thinks it is interactive.
        raise RadarUnconfigured(
            "gh has no credentials in this environment, so it refused "
            f"before {what} for issue #{number}: {err}")
    if issue_op._auth_probe.says_not_authenticated(err):
        raise RadarUnreachable(
            f"gh says this request was not authenticated (exit "
            f"{result.returncode}) {what} for issue #{number}: {err}. "
            "Run: gh auth login")
    if issue_op._status_probe.says_not_found(err):
        raise RadarError(f"issue #{number} not found: {err}")
    if _transport_unreachable(err):
        raise RadarUnreachable(
            f"gh could not reach the API (exit {result.returncode}) "
            f"{what} for issue #{number}: {err}")
    raise RadarError(
        f"gh did not answer {what} for issue #{number}, and nothing in its "
        f"output says why (exit {result.returncode}): {err}")


def _fetch_issue(number: str) -> dict:
    result = _run(
        ["issue", "view", number, "--json", "number,title,state,labels,url"],
        number, "fetching the issue")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError(f"could not parse gh JSON output for issue #{number}") from exc
    if not isinstance(data, dict):
        raise RadarError(f"gh returned no issue object for issue #{number}")
    return data


def _fetch_closing_prs(number: str, web_url: str) -> list[dict]:
    owner_name = issue_op._owner_repo(web_url)
    if owner_name is None:
        raise RadarError(
            f"could not determine owner/repo for issue #{number}'s linked-PR lookup")
    owner, name = owner_name
    query = issue_op._closing_prs_query(owner, name, number)
    result = _run(["api", "graphql", "-f", f"query={query}"],
                  number, "fetching linked PRs")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError(
            f"could not parse gh GraphQL output for issue #{number}'s linked PRs") from exc
    nodes = issue_op._closing_pr_nodes(payload)
    if nodes is None:
        raise RadarError(
            f"gh returned an unexpected shape for issue #{number}'s linked PRs")
    return nodes


def _number_sort_key(value: str) -> tuple[int, str]:
    """Numeric where possible, so `#9` sorts before `#10`."""
    return (0, value.rjust(20, "0")) if value.isdigit() else (1, value)


def _check_line(pr: dict) -> str:
    """One linked PR's check verdict — three states, never two.

    Reuses `presets/github/issue.py`'s own `_check_tally` (#815) so this tier
    and the `gh-issue` op read one sentence for "no run" / "unknown" /
    the tally, not two hand-rolled copies that can disagree.
    """
    number = pr.get("number", "?")
    return issue_op._check_tally(pr, number)


def radar_report(options: dict | None = None) -> tuple[list[str], bool]:
    """(lines, healthy) — one issue's own board, as radar's tier contract wants it.

    `healthy` means "this tier could tell you the truth", not "the issue is
    in a good state" — the same distinction `gl_issue.radar_report` draws. A
    watcher that stopped being coverable counts against it; so does a reopen,
    a label change or a linked-PR set that moved, for the same reason a
    departed MR counts against `gl-issue`'s health: `quiet_when_healthy` drops
    the whole report when healthy, and an event this board exists to surface
    must not be the thing that makes it go quiet.
    """
    options = options or {}
    watch = options.get("_watch") or _no_watch
    number = parse_arg(str(options.get("_arg") or ""))

    issue = _fetch_issue(number)
    web_url = str(issue.get("url") or "")
    closing = _fetch_closing_prs(number, web_url)

    title = issue_op._untrusted.flat(str(issue.get("title") or "?"))
    state = str(issue.get("state") or "?")
    labels = sorted({issue_op._untrusted.flat(str(label.get("name")
                     if isinstance(label, dict) else label))
                     for label in (issue.get("labels") or []) if label})

    open_prs = [pr for pr in closing
                if isinstance(pr, dict) and pr.get("state") == "OPEN"]
    pr_numbers = sorted({str(pr.get("number")) for pr in open_prs
                         if pr.get("number") is not None}, key=_number_sort_key)

    watch_status = {pr_number: watch(SOURCE, pr_number, list(ONLY_EVENTS))
                    for pr_number in pr_numbers}
    # "unclaimable" is #693's third state, passed straight through `_watch` /
    # `ensure_watcher`: the slot could not be claimed, so nothing was spawned
    # and nothing was established about it -- not covered, and not merely
    # "failed" either. Same catch as `gl_issue.radar_report` and `gh_prs.heal`.
    uncovered = sorted((pr_number for pr_number, status in watch_status.items()
                        if status in ("failed", "capped", "unclaimable")),
                       key=_number_sort_key)

    digest = snapshot.key(number)
    previous = snapshot.read(SNAPSHOT_PREFIX, digest, "issue")
    prev_entry = (previous or {}).get("issue") if previous else None

    # Cold start (no previous entry) has nothing to diff against — "moved" is
    # a claim about a comparison, and a comparison against nothing is not
    # one. Without this guard every linked PR on the very first run reads as
    # "new", which is simply what the board looks like the first time it is
    # ever built, not an event worth an unhealthy flag.
    cold_start = prev_entry is None
    reopened = (not cold_start and prev_entry.get("state") == "CLOSED"
               and state == "OPEN")
    prev_labels = set(prev_entry.get("labels") or []) if not cold_start else set()
    labels_added = [] if cold_start else sorted(set(labels) - prev_labels)
    labels_removed = [] if cold_start else sorted(prev_labels - set(labels))
    prev_prs = set(prev_entry.get("prs") or []) if not cold_start else set()
    new_prs = [] if cold_start else sorted(set(pr_numbers) - prev_prs, key=_number_sort_key)
    departed_prs = ([] if cold_start
                    else sorted(prev_prs - set(pr_numbers), key=_number_sort_key))

    # This board renders the issue's own title on every call, plus every
    # linked PR's title and branch when there are any -- all of it author's
    # words, flattened by `flat()` rather than fenced. `gl_issue` owes this
    # same one-line disclosure once, at the top, for exactly this reason.
    lines = [issue_op._untrusted.flat_note("the issue and PR titles"),
             f"gh-issue #{number}: {title}",
             f"  state: {state}" + ("  <-- REOPENED" if reopened else "")]
    lines.append(f"  labels: {', '.join(labels) or 'none'}")
    if labels_added:
        lines.append(f"  labels added: {', '.join(labels_added)}")
    if labels_removed:
        lines.append(f"  labels removed: {', '.join(labels_removed)}")
    if not open_prs:
        lines.append("  open linked PRs: none")
    else:
        lines.append(f"  open linked PRs: {len(open_prs)}")
        for pr in sorted(open_prs, key=lambda p: _number_sort_key(str(p.get("number")))):
            pr_number = str(pr.get("number", "?"))
            pr_title = issue_op._untrusted.flat(str(pr.get("title") or "?"))
            pr_branch = issue_op._untrusted.flat(str(pr.get("headRefName") or "?"))
            lines.append(f"    #{pr_number} {pr_title}")
            lines.append(f"      branch: {pr_branch}")
            lines.append(f"      checks: {_check_line(pr)}")
            lines.append(f"      watcher: {watch_status.get(pr_number, '?')}")
    if new_prs:
        lines.append(f"  new linked PR(s) since last run: "
                     f"{', '.join('#' + n for n in new_prs)}")
    if departed_prs:
        lines.append(f"  no longer linked/open: "
                     f"{', '.join('#' + n for n in departed_prs)}")
    if uncovered:
        lines.append(f"  radar: WARNING — watcher not alive for "
                     f"{', '.join('#' + n for n in uncovered)}")

    snapshot.write(SNAPSHOT_PREFIX, digest,
                   {"state": state, "labels": labels, "prs": pr_numbers}, "issue")

    healthy = not (uncovered or reopened or labels_added or labels_removed
                   or new_prs or departed_prs)
    return lines, healthy


def radar_state(options: dict | None = None) -> list[str]:
    """What this tier knows, without spawning or calling GitHub (#859's
    `radar_state` pattern, reused here for the same reason: looking must not
    cost what acting costs).
    """
    options = options or {}
    try:
        number = parse_arg(str(options.get("_arg") or ""))
    except RadarError as exc:
        return [f"  issue     : REFUSED — {exc}"]
    out = [f"  issue     : #{number}"]
    digest = snapshot.key(number)
    path = snapshot.path(SNAPSHOT_PREFIX, digest)
    previous = snapshot.read(SNAPSHOT_PREFIX, digest, "issue")
    if previous is None:
        out.append(f"  snapshot  : {path} — absent (cold start next run)")
    else:
        entry = previous.get("issue") or {}
        out.append(f"  snapshot  : {path} — state={entry.get('state', '?')}, "
                   f"{len(entry.get('prs') or [])} PR(s), "
                   f"{len(entry.get('labels') or [])} label(s)")
    return out
