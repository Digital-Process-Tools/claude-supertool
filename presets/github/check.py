#!/usr/bin/env python3
"""Check-run detail + annotations — the *other* id namespace (#793).

GitHub reports CI through two surfaces that both hand out bare integers:

  * **Actions jobs** — `repos/{o}/{r}/actions/jobs/<id>`, what `gh-job` reads.
  * **check runs** — `repos/{o}/{r}/check-runs/<id>`, what every GitHub App
    that is not Actions writes to: CodeQL default setup, Dependabot,
    code-scanning uploads from other tools, external CI.

**The two spaces overlap in one direction, and only one.** Verified live on
2026-08-05 against this repo: an Actions job id also resolves as a check run
(GitHub creates one per job, sharing the integer), so `gh-check:<job-id>`
answers. A check run written by an App has no Actions job behind it, so
`gh-job:<check-run-id>` 404s — and used to answer "no such job exists in this
repo", an absence by one route published as an absence from the world. That
asymmetry is why the id-not-found branch in `_not_found_message` that points
back at `gh-job` is nearly unreachable in practice; it stays because "check
runs are a superset" is an observation about today's GitHub, not a contract. This op is the second route, and it
is a **sibling rather than a fallback inside `gh-job`** on purpose: an op
whose header says `Job #N` must never turn out to have read a check run.
Each op probes the other namespace only to *improve its own 404 message* and
never to render content, so the answer's provenance is always the op you
called (see `docs/presets/github.md`, "Two id namespaces").

For a scanning check the whole finding is the annotation triple — `path:line`,
title, message — which is why that is what this prints. The check-run object's
`output.summary` is printed above it because some apps put the finding there
and publish no annotations at all; **zero annotations is never rendered as an
all-clear**.

This op does not read the code-scanning API. `code-scanning/alerts?ref=…`
returned empty in the incident that filed #793 while the finding sat in an
annotation, and an emptiness that means "not the endpoint that knows" is not
something to put in front of a reader deciding a merge.

Config via SUPERTOOL_ env vars (set from .supertool.json):
  GH_CHECK_ANNOTATION_CAP   — annotations printed before `+N more` (default 5)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _checks  # noqa: E402  (NAMED_CAP — the repo's disclosure cap, #605/#619)
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)
from _env import env_int  # noqa: E402  (the one numeric-knob reader)

# One page of annotations. GitHub's default is 30; asking for the maximum makes
# the common case whole in a single request, and a page that comes back *full*
# is disclosed as a floor rather than as a total (see `_annotations`).
PER_PAGE = 100

# How long each gh call may take. Same budgets as gh-job's metadata calls.
TIMEOUT = 15

SUMMARY_MAX = 600
MESSAGE_MAX = 500


def _api_path(suffix: str) -> str:
    return _repo_target.api_path(suffix)


def _gh_error_kind(stderr: str) -> str:
    """Bucket a gh failure. A deliberate copy of `job.py`'s twin.

    The presets are standalone scripts, not a package — `job.py` cannot be
    imported from here without inventing one. The buckets this op acts on are
    only `notfound` versus everything else, so the copy is small and its drift
    risk is bounded; widening it would be the moment to move it into
    `presets/_checks.py`.
    """
    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return "repo"
    if "could not resolve" in s or "404" in s or "not found" in s:
        return "notfound"
    if "401" in s or "unauthorized" in s or "not logged in" in s or "token" in s:
        return "auth"
    if "rate limit" in s or "429" in s:
        return "ratelimit"
    if "403" in s or "forbidden" in s:
        return "forbidden"
    return "other"


class GhCall:
    """One gh invocation, in the three states a caller has to tell apart.

    `ok` — it answered. `absent` — it answered 404, which is information.
    Neither — it did not answer, and *that is not evidence of absence*; every
    call site below has to branch on the difference rather than folding a
    transport failure into "not there" (`docs/validators.md`, "Declining
    instead of guessing").
    """

    def __init__(self, ok: bool, data: object = None, absent: bool = False,
                 error: str = "") -> None:
        self.ok = ok
        self.data = data
        self.absent = absent
        self.error = error


def _gh(args: list[str], timeout: int = TIMEOUT) -> GhCall:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return GhCall(False, error="gh not found — install from https://cli.github.com")
    except subprocess.TimeoutExpired:
        return GhCall(False, error=f"gh timed out after {timeout}s")
    if r.returncode != 0:
        kind = _gh_error_kind(r.stderr)
        detail = r.stderr.strip() or f"gh exited {r.returncode}"
        return GhCall(False, absent=(kind == "notfound"), error=detail)
    try:
        return GhCall(True, json.loads(r.stdout or "null"))
    except (json.JSONDecodeError, ValueError) as exc:
        return GhCall(False, error=f"gh returned unparseable JSON: {exc}")


def _job_probe(check_id: str) -> GhCall:
    """Ask the *Actions* namespace about this id — for the message only."""
    return _gh(["gh", "api", _api_path(f"actions/jobs/{check_id}")])


def _not_found_message(check_id: str, probe: GhCall) -> str:
    """A 404 from the checks API, resolved into which of three things it is."""
    scope = _repo_target.not_found_scope()
    if probe.ok:
        meta = probe.data if isinstance(probe.data, dict) else {}
        name = str(meta.get("name") or "?")
        return (
            f"ERROR: No check run #{check_id} {scope} — but an **Actions job** "
            f"with that id does exist ({name}). The id is right and the "
            f"namespace is not: Actions jobs and check runs are two id spaces "
            f"and this op reads the second. Read it with: "
            f"./supertool 'gh-job:{check_id}:fail'"
        )
    if probe.absent:
        return (
            f"ERROR: No check run #{check_id} {scope}, and no Actions job with "
            f"that id either — both namespaces answered 404, so the id is "
            f"wrong. Check the ID. List the check runs on a PR with: "
            f"./supertool 'gh-check:pr:NUMBER'"
        )
    return (
        f"ERROR: No check run #{check_id} {scope}. Whether it is an Actions job "
        f"instead could not be established — that probe did not answer: "
        f"{probe.error}. This op is not guessing between a wrong id and an id "
        f"in the other namespace. Retry, or read it directly with: "
        f"gh api {_api_path('actions/jobs/' + check_id)}"
    )


def _clip(text: str, limit: int) -> str:
    """Cut with the cut named. Silence at the boundary is the bug (#605)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit} chars truncated]"


def _print_header(check_id: str, check: dict, routed_from: str = "",
                  mode_note: str = "") -> tuple[str, str]:
    """Metadata block. Returns `(status, conclusion)`, which the caller branches on."""
    name = str(check.get("name") or "?")
    status = str(check.get("status") or "?")
    conclusion = str(check.get("conclusion") or "")
    app = check.get("app") if isinstance(check.get("app"), dict) else {}
    slug = str((app or {}).get("slug") or "")
    print(f"# Check run #{check_id} — {name}")
    # The provenance line. This op can be handed either kind of id, so which
    # API answered is part of the answer, not an implementation detail.
    print("Source: checks API (a check run, not an Actions job)")
    if routed_from:
        # #827: when the reader called a *different* op, the header alone is
        # not enough — "Check run" under a call they typed as `gh-job` reads
        # as a bug unless the routing says so. This line is the entire licence
        # to route: #821 declined to answer because answering would have been
        # "a probe that silently changes which API answered", and that is
        # right about *silently*, not about *answering*.
        print(routed_from)
    if mode_note:
        # Beside the routing, not after the Output block: both sentences answer
        # "why does this not look like what I asked for", and a reader who has
        # already started reading Output has stopped asking.
        print(mode_note)
    print(f"Status: {status}" + (f" / {conclusion}" if conclusion else ""))
    if slug:
        print(f"App: {slug}")
    head_sha = str(check.get("head_sha") or "")
    if head_sha:
        print(f"Commit: {head_sha}")
    url = str(check.get("html_url") or "")
    if url:
        print(f"URL: {url}")
    output = check.get("output") if isinstance(check.get("output"), dict) else {}
    title = _clip(str((output or {}).get("title") or ""), 200)
    summary = _clip(str((output or {}).get("summary") or ""), SUMMARY_MAX)
    if title or summary:
        print("\n## Output")
        if title:
            print(f"Title: {title}")
        if summary:
            print(f"Summary: {summary}")
    return status, conclusion


def _annotation_line(a: dict) -> list[str]:
    path = str(a.get("path") or "?")
    start = a.get("start_line")
    end = a.get("end_line")
    where = f"{path}:{start}" if start else path
    if end and start and end != start:
        where += f"-{end}"
    level = str(a.get("annotation_level") or "")
    title = str(a.get("title") or "").strip()
    head = f"  {where}"
    if level:
        head += f"  [{level}]"
    if title:
        head += f"  {title}"
    lines = [head]
    message = _clip(str(a.get("message") or ""), MESSAGE_MAX)
    for part in message.splitlines():
        lines.append(f"      {part}")
    raw = _clip(str(a.get("raw_details") or ""), MESSAGE_MAX)
    for part in raw.splitlines():
        lines.append(f"      | {part}")
    return lines


def _print_annotations(annotations: list, conclusion: str, status: str = "") -> None:
    total = len(annotations)
    cap = env_int("GH_CHECK_ANNOTATION_CAP", _checks.NAMED_CAP, minimum=1)
    if total == 0:
        print("\n## Annotations (0)")
        if status and status != "completed":
            # Observed live on 2026-08-05 against a running leg of this repo:
            # the check-run object carries `status: in_progress` and an empty
            # `conclusion`, and annotations are written as the check runs. A
            # zero here is a reading taken mid-flight, and rendering it with
            # the vocabulary of a finished clean check is the all-clear this
            # whole issue is about, one state further back.
            print(
                f"This check run has not finished — its status is `{status}`. "
                f"Annotations are written while a check runs, so 0 is a "
                f"count taken mid-flight, not a result. Re-read it once the "
                f"check completes."
            )
        elif conclusion and conclusion not in ("success", "neutral", "skipped"):
            print(
                f"This check run published no annotations, and its conclusion "
                f"is `{conclusion}`. That is not an all-clear: a check can "
                f"fail with its detail only in the Output above, or in a "
                f"system this op does not read. Read the Output and the URL "
                f"before treating this as nothing."
            )
        else:
            print(
                f"This check run published no annotations and concluded "
                f"`{conclusion or 'unknown'}` — nothing was flagged on a line."
            )
        return

    shown = annotations[:cap]
    hidden = total - len(shown)
    note = f"+{hidden} more" if hidden else ""
    header = f"\n## Annotations ({total})"
    if hidden:
        header = (f"\n## Annotations ({total} total, {len(shown)} shown — "
                  f"{note}; raise GH_CHECK_ANNOTATION_CAP=N)")
    print(header)
    for a in shown:
        if not isinstance(a, dict):
            continue
        for line in _annotation_line(a):
            print(line)
    if hidden:
        # Header *and* footer: a reader cut off by a downstream budget never
        # reaches a footer, and a reader who scrolls never re-reads a header.
        print(f"... ({note} of {total} not shown — capped at {len(shown)} by "
              f"GH_CHECK_ANNOTATION_CAP)")
    if total >= PER_PAGE:
        print(
            f"NOTE: this op read the first page only (per_page={PER_PAGE}) and "
            f"it came back full, so {total} is a floor, not a total. Page the "
            f"rest by hand if the count matters."
        )


def _show_check(check_id: str) -> int:
    got = _gh(["gh", "api", _api_path(f"check-runs/{check_id}")])
    if not got.ok:
        if _gh_error_kind(got.error) == "repo":
            print(_repo_target.no_repo_error(f"gh-check:{check_id}"))
            return 1
        if got.absent:
            print(_not_found_message(check_id, _job_probe(check_id)))
            return 1
        print(f"ERROR: could not read check run #{check_id}: {got.error}")
        return 1
    check = got.data if isinstance(got.data, dict) else {}
    return render_check(check_id, check)


def render_check(check_id: str, check: dict, *, routed_from: str = "",
                 mode_note: str = "") -> int:
    """Render an already-fetched check-run object. The one renderer, shared.

    Split out of `_show_check` for #827 so `gh-job` can route an id the Actions
    namespace disowned *without* re-fetching the object it already probed, and
    without a second copy of this render drifting from this one. A job renders
    as a log; a check run renders as status + output + annotations, and forcing
    either into the other's template is how the output starts lying.
    """
    status, conclusion = _print_header(check_id, check, routed_from, mode_note)

    ann = _gh(["gh", "api",
               _api_path(f"check-runs/{check_id}/annotations?per_page={PER_PAGE}")])
    if not ann.ok:
        # Never fall through to an empty list here. "The fetch failed" and
        # "there are none" are the two sentences this whole issue is about.
        print(
            f"\nERROR: the check run was read, but its annotations were not: "
            f"{ann.error}. Whether this check flagged any line is UNKNOWN — "
            f"this is not zero annotations. Retry, or: gh api "
            f"{_api_path('check-runs/' + check_id + '/annotations')}"
        )
        return 1
    annotations = ann.data if isinstance(ann.data, list) else []
    _print_annotations(annotations, conclusion, status)
    return 0


_MARK = {"success": "✓", "failure": "✗", "cancelled": "⊘", "skipped": "–",
         "neutral": "•", "timed_out": "✗", "action_required": "!"}


def _list_pr(pr: str) -> int:
    """Check runs on a PR's head commit — the id `gh-pr:N:status` does not print.

    `gh-pr:N:status` names a failing `CodeQL` leg by name and no id (the id
    rides on `detailsUrl` only for Actions legs, `_checks.github_job_id`), so
    without this form the id-taking form above is unreachable from the merge
    gate that needs it.
    """
    head = _gh(["gh", "pr", "view", pr, *_repo_target.gh_args(),
                "--json", "headRefOid"])
    if not head.ok:
        if _gh_error_kind(head.error) == "repo":
            print(_repo_target.no_repo_error(f"gh-check:pr:{pr}"))
            return 1
        print(f"ERROR: could not read PR #{pr}: {head.error}")
        return 1
    data = head.data if isinstance(head.data, dict) else {}
    sha = str(data.get("headRefOid") or "")
    if not sha:
        print(
            f"ERROR: PR #{pr} answered without a head commit SHA, so there is "
            f"nothing to list check runs against. This is not an empty check "
            f"list — the lookup never happened. Retry, or: "
            f"gh pr view {pr} --json headRefOid"
        )
        return 1

    got = _gh(["gh", "api",
               _api_path(f"commits/{sha}/check-runs?per_page={PER_PAGE}")])
    if not got.ok:
        print(f"ERROR: could not list check runs for PR #{pr} (head {sha}): "
              f"{got.error}")
        return 1
    payload = got.data if isinstance(got.data, dict) else {}
    runs = payload.get("check_runs")
    runs = runs if isinstance(runs, list) else []
    print(f"# Check runs on PR #{pr} — head commit {sha}")
    if not runs:
        print(
            f"0 check runs are attached to the head commit {sha}. That is a "
            f"statement about this commit, not about the PR: a check attached "
            f"to the merge ref, or one that has not been created yet, does not "
            f"appear here. Cross-check with: ./supertool 'gh-pr:{pr}:status'"
        )
        return 0
    print(f"{len(runs)} check run{'s' if len(runs) != 1 else ''} on the head commit.")
    for r in runs:
        if not isinstance(r, dict):
            continue
        conclusion = str(r.get("conclusion") or r.get("status") or "?")
        mark = _MARK.get(conclusion, "?")
        rid = str(r.get("id") or "")
        name = str(r.get("name") or "?")
        line = f"  {mark} {conclusion:<12} {name}"
        if rid:
            line += f"  #{rid}"
        print(line)
    print(f"\nRead one with: ./supertool 'gh-check:<id>'  — annotations "
          f"(path:line, title, message) are where a scanning check keeps its "
          f"finding.")
    return 0


def _usage() -> int:
    print("ERROR: usage: gh-check:CHECK_RUN_ID  |  gh-check:pr:NUMBER\n"
          "  gh-check:ID       — one check run: status, output, annotations\n"
          "  gh-check:pr:N     — check runs on PR N's head commit, with ids\n"
          "For an Actions job id use gh-job — they are two id namespaces.")
    return 1


def main() -> int:
    args = sys.argv[1:]
    if not args or not args[0].strip():
        return _usage()
    first = args[0].strip()
    if first.lower() == "pr":
        pr = args[1].strip() if len(args) > 1 else ""
        if not pr.isdigit():
            print("ERROR: usage: gh-check:pr:NUMBER (a PR number)")
            return 1
        return _list_pr(pr)
    if not first.isdigit():
        print(f"ERROR: {first!r} is not a check-run id. "
              f"Usage: gh-check:CHECK_RUN_ID | gh-check:pr:NUMBER")
        return 1
    return _show_check(first)


if __name__ == "__main__":
    sys.exit(main())
