#!/usr/bin/env python3
"""GitLab merge request details via glab CLI.

Fetches MR metadata, pipeline status, reviewer/approval info,
diff stats, and human comments. Dashboard-style output.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))  # for mrs._conflict_label
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _checks (#619)
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)

# Imported by name, not `import mrs` — main() already binds a local `mrs`
# (the branch-lookup result list), which would shadow a module import.
from mrs import _conflict_label  # noqa: E402
import _body  # noqa: E402  (the one body cap + disclosure — #698)
import _untrusted  # noqa: E402  (the fence around tracker text — #694)
import _classify_render  # noqa: E402  (the verdict beside the fence — #2049)
import _auth_probe  # noqa: E402  (does this stderr *state* that the credential is unusable? - #1846)
import _status_probe  # noqa: E402  (does this stderr *state* the target is missing or access denied? - #1864)
import _checks  # noqa: E402  (named_disclosure/NAMED_CAP — shared with gh-pr, #619)
import _branch_locale  # noqa: E402  (where the branch is checked out — shared by all five #850)
import _refname  # noqa: E402  (the one ordinary-refname rule — #694/#924)
import _repo_target  # noqa: E402  (the project this call is about, if not cwd's — #676)
import _secrets  # noqa: E402  (the one GitLab token-prefix list — #1645)

DESCRIPTION_MAX = 2000
COMMENT_MAX = 500
COMMENT_TOTAL_MAX = 2000

# See presets/github/issue.py's identical constant for the reasoning (#2049).
_CLASSIFY_LEVEL = _classify_render.level_from_env()
TAIL_COMMENTS = 2
NAMESTATUS_DISPLAY_MAX = 50
NAMESTATUS_FETCH_CAP = 500


def _relative_age(iso: str) -> str:
    """Format an ISO timestamp as 'Nd ago', 'Nh ago', or 'Nm ago'.

    Returns '?' on parse failure. Used for MR created/updated lines so the
    agent gets stale-MR signal in one round-trip.
    """
    if not iso:
        return "?"
    try:
        from datetime import datetime, timezone
        # GitLab ISO format: 2026-05-08T10:00:00.000Z
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, ImportError):
        return "?"


def _glab(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a glab subcommand, against the targeted project when there is one.

    `-R` is appended here rather than at the call site so a subcommand added
    later cannot forget it and read the cwd's project under a target (#676).
    """
    return subprocess.run(
        ["glab"] + args + _repo_target.gl_args(),
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _glab_api(endpoint: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a glab api call, against the targeted project when there is one.

    `glab api` has no repo flag — the project is a path segment — so the
    target is substituted into `projects/:id` on the way through (#676).
    """
    return subprocess.run(
        ["glab", "api", _repo_target.gl_api_path(endpoint)],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _glab_fail_detail(r: subprocess.CompletedProcess[str]) -> str:
    """One line naming why a `glab api` call failed, for a decline message.

    `glab` writes its error to stderr in a boxed multi-line form and leaves
    stdout empty, so the reason is there to be had — it was simply never read.
    """
    # `split_lines` decides the boundary, `flat` below spells what is inside
    # it (#1654). `str.splitlines()` cut on a U+2028 too, and the first
    # non-empty segment was then whatever the writer put before it — the
    # remainder, `403 forbidden` and all, never reached the decline.
    for line in _untrusted.split_lines(r.stderr or ""):
        line = _untrusted.flat(line.strip())
        if line and line != "ERROR":
            return f"glab exit {r.returncode}: {line[:120]}"
    return f"glab exit {r.returncode}"


def _fetch_json(
    endpoint: str, noun: str, timeout: int = 10,
) -> tuple[object | None, str | None]:
    """`(payload, None)` when GitLab answered, `(None, reason)` when it did not.

    The four ways a `glab api` call fails to produce a payload, each named in
    its own sentence, in one place. #720 wrote these branches out by hand for
    the approvals line and every sibling block in this file kept a two-state
    `except ...: pass` instead — which is #812. The abstraction is the fix as
    much as the wording is: a block that has to *remember* to decline is a
    block that will stop remembering.

    The sentences stay distinct on purpose. A timeout is a retry; an
    unparseable body is a bug report; a non-zero exit is usually auth or
    permissions; a missing binary is an install. Collapsing them into one
    "could not fetch" would be a smaller copy of the mistake this exists to
    fix, so the four remedies keep four sentences.

    What is *not* caught here is as deliberate: there is no bare
    `except Exception`. An AttributeError or TypeError from this module's own
    logic is a defect in supertool, and turning it into a printed decline
    would trade the loud bug for the quiet one.
    """
    try:
        r = _glab_api(endpoint, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"{noun} API timed out"
    except OSError as e:  # FileNotFoundError included — glab absent, or an errno
        # Listed on its own authority, same as _approvals_line: #507 was filed
        # as a silent decline and the fatal thing inside it was an unlisted
        # OSError. Every sibling caller here caught TimeoutExpired and
        # JSONDecodeError only, so an errno mid-render was a traceback.
        return None, f"could not run glab ({e})"
    if r.returncode != 0:
        return None, f"{noun} API failed ({_glab_fail_detail(r)})"
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError:
        return None, f"{noun} API returned no parseable JSON"


def _fetch_array(
    endpoint: str, noun: str, timeout: int = 10,
) -> tuple[list | None, str | None]:
    """`_fetch_json` for the endpoints documented as returning an array.

    A body that parses but is not an array is a fifth way to have no answer,
    and it is the one that used to reach `.get` on a string and take the whole
    render down (#735). It names the type it got, because "the discussions
    endpoint returned an object" is a sentence someone can act on and "could
    not read discussions" is not.
    """
    data, reason = _fetch_json(endpoint, noun, timeout)
    if reason is not None:
        return None, reason
    if not isinstance(data, list):
        return None, (f"{noun} API returned a {type(data).__name__}, "
                      f"expected an array")
    return data, None


#: The cap is read back off the endpoint string that was actually sent, not
#: from a constant beside it. #1505 fixed the GitHub side by making the cap
#: constant build the query so the render's inference could not drift from what
#: was asked; the GitLab side has no such constant — `per_page=100` is a
#: literal inside an f-string URL — so the same guarantee is bought by reading
#: the sent URL instead of a second copy of the number.
_PER_PAGE = re.compile(r"[?&]per_page=(\d+)")


def _page_cap(endpoint: str) -> "int | None":
    """The `per_page` this endpoint asked for, or ``None`` when it asked for none.

    ``None`` is not zero and not "uncapped by us": an endpoint with no
    `per_page` gets GitLab's own default page size, which this code does not
    know, so nothing can be concluded about truncation from the row count. The
    callers below treat it as "not measured" and hedge nothing — a hedge on
    every number is what #1505's reviewer raised and had argued down.
    """
    match = _PER_PAGE.search(endpoint or "")
    return int(match.group(1)) if match else None


def _fetch_tally(
    endpoint: str, noun: str, timeout: int = 10,
) -> "tuple[list | None, str | None, bool]":
    """`_fetch_array` for a caller that COUNTS what comes back (#1517).

    Third value: the page came back exactly full, so the count taken off it is
    a floor and not a total. `_fetch_array` does not paginate, and it is shared
    with callers that do not tally — `pipelines?per_page=1` wants precisely one
    row — so paginating in there would buy them round-trips nobody asked for
    and would still leave each render inferring its own truncation. The fact is
    returned alongside the array instead, which is the answer #1508 gave to the
    same question one platform over.

    A fetch that failed is not a capped page. It has its own sentence already,
    and reporting `capped` beside a `reason` would be a second claim about a
    read that did not happen.
    """
    rows, reason = _fetch_array(endpoint, noun, timeout)
    if reason is not None or rows is None:
        return None, reason, False
    cap = _page_cap(endpoint)
    return rows, None, cap is not None and len(rows) >= cap


def _floor(count: int, capped: bool) -> str:
    """`>=N` off a full page, plain `N` off a short one.

    The same spelling `gh-prs`'s release gate uses for a lower bound, and the
    asymmetry is the point: a tally over a page that was NOT capped must still
    print as exact, or the disclosure stops meaning anything.
    """
    return f">={count}" if capped else str(count)


#: Why the truncation is worst here specifically: GitLab returns discussions
#: oldest-first, so the page that was never fetched holds the newest and least
#: resolved threads — the failure runs in the direction nobody checks.
_PAGE_FULL = ("  ! PAGE FULL — this came off ONE unpaginated page and GitLab "
              "returned exactly its per_page limit, so the count(s) above are "
              "a LOWER BOUND, not a total. GitLab pages oldest-first, so what "
              "is missing is the newest.")


def _as_dict(value: object) -> dict:
    """A remote field documented as an object, or `{}` when it came back as
    something else.

    Replaces the `(x or {})` idiom, which guards `null` and nothing else — a
    string or a list walks straight through it and raises `AttributeError` on
    the very next `.get` (#735).
    """
    return value if isinstance(value, dict) else {}


def _dict_elements(seq: object) -> tuple[list[dict], int]:
    """`(objects, how_many_were_not)` for a remote array.

    Every endpoint `gl-mr` reads is documented as returning an array of
    objects, and checking `isinstance(seq, list)` says nothing about what is
    inside it. Returning the skipped count rather than swallowing it is the
    whole point: see `_unreadable`.
    """
    if not isinstance(seq, list):
        return [], 0
    kept = [e for e in seq if isinstance(e, dict)]
    return kept, len(seq) - len(kept)


def _array_elements(value: object) -> tuple[list[dict], int, int]:
    """`(objects, unreadable, total)` for a field documented as an array of objects.

    A field that is not an array *at all* counts as one unreadable element
    rather than as an empty list. Without that, guarding the elements would
    turn a crash into `Reviewers: none` — a claim about the MR, made from a
    payload nobody could read, which is the trade this fix exists to avoid.

    An absent field (`None`) is a real empty answer and stays silent.
    """
    if value is None:
        return [], 0, 0
    if not isinstance(value, list):
        return [], 1, 1
    kept, bad = _dict_elements(value)
    return kept, bad, len(value)


def _unreadable(skipped: int, total: int, noun: str) -> str:
    """Disclosure line for elements `_dict_elements` dropped — `''` when none were.

    The guard on its own turns a loud crash into a quiet undercount: nine
    threads reported where twelve came back, with nothing in the output saying
    a narrowing happened. That is this repo's most-filed defect class, and the
    three-state contract in `docs/validators.md` covers it — an answer, a
    finding, or declining, never a silent partial.

    The disclosure costs nothing in the case that actually happens every day:
    `skipped == 0` prints nothing at all, so a healthy render is byte-identical
    to the one before this guard existed.
    """
    if skipped <= 0:
        return ""
    return f"  ! {skipped} of {total} {noun} had a shape supertool could not read"


def _print_unreadable(skipped: int, total: int, noun: str) -> None:
    """`_unreadable` straight to stdout, printing nothing when nothing was skipped."""
    note = _unreadable(skipped, total, noun)
    if note:
        print(note)


def _approver_name(entry: object) -> str:
    """`username` out of one `approved_by` entry, or `?` for a shape we do not know."""
    if not isinstance(entry, dict):
        return "?"
    user = entry.get("user")
    if not isinstance(user, dict):
        return "?"
    name = user.get("username")
    return str(name) if name else "?"


def _approvals_line(iid: str | int) -> str:
    """The `Approved by: ...` line — one line, three states, never raises.

    GitLab documents `GET /projects/:id/merge_requests/:iid/approvals` as
    returning a JSON **object** carrying `approved_by`, on every tier including
    Free. Every branch below that is not that object is therefore not GitLab
    answering — a `glab` that could not ask, a body that is not JSON, a
    responder that is not GitLab — and **none of them mean nobody approved this
    MR**. The line used to spell that third state three different ways, all of
    them wrong (#720):

    - a non-zero `glab` exit printed **no line at all**, so the most ordinary
      failure on this call — an unauthenticated CLI, which exits 1 with empty
      stdout — silently removed a line whose neighbours (`Reviewers:`,
      `Assignees:`) print `none` precisely so that absence is signal;
    - a timeout or a decode failure fell into `except: pass`, same silence;
    - anything that parsed to a non-object hit `.get` on it and raised
      `AttributeError` **out of the whole render**, taking the threads,
      pipeline, conflicts, linked issue, description and comments sections with
      it — none of which are about approvals. #507's precedent in this same op:
      the loud failure was hiding inside the quiet one.

    Declining is the fix rather than defaulting, per `docs/validators.md`
    §"Declining instead of guessing" — suppressing this into `[]` would trade a
    crash for `Approved by: none`, which is a wrong answer rather than a missing
    one, and is the defect class this tracker is mostly made of.
    """
    unknown = "Approved by: UNKNOWN"
    # The four fetch/parse states moved to `_fetch_json` unchanged, word for
    # word, when #812 generalised them to this file's other blocks. They are
    # #720's contract, not an implementation detail free to be reworded on the
    # way past — `test_approvals_line_keeps_its_720_wording` pins that.
    approvals, reason = _fetch_json(
        f"projects/:id/merge_requests/{iid}/approvals", "approvals")
    if reason is not None:
        return f"{unknown} — {reason}"
    if not isinstance(approvals, dict):
        return (f"{unknown} — approvals API returned a "
                f"{type(approvals).__name__}, expected an object")
    if "approved_by" not in approvals:
        note = approvals.get("message") or approvals.get("error") or ""
        detail = f": {str(note)[:120]}" if note else ""
        return f"{unknown} — approvals payload carries no approved_by field{detail}"
    approved_by = approvals["approved_by"]
    if not isinstance(approved_by, list):
        return (f"{unknown} — approved_by is a "
                f"{type(approved_by).__name__}, expected a list")
    if not approved_by:
        return "Approved by: none"
    return f"Approved by: {', '.join(_approver_name(a) for a in approved_by)}"


# Statuses that resolve on their own — a job here will move to success/failed/
# etc. without anyone naming it. Everything NOT in this set (and not
# "success") gets its own line: `failed`/`canceled`/`skipped`/`manual` are
# this platform's spelling of #445/#454's defect class — a leg that is
# neither passing nor still moving must never be silently absent from the
# answer (#619).
#
# This set decides which legs get *named*, and only that. It is not a
# classifier and must not become one: the tally above the names goes through
# `_checks.summarize()`, the same judgement `gh-pr:N:status` sums its rollup
# with (#958 — the render may differ per platform, the classification may
# not). That reuse is safe precisely because `summarize()` does not enumerate:
# GitLab's `canceled` (one L) is in none of that module's four state sets, so
# it lands in the leftover term and is named there rather than mapped onto
# GitHub's `CANCELLED` (two Ls). Nothing is guessed; the sum is what carries
# a vocabulary neither side has taught the other.
_GL_JOB_RESOLVES_ITSELF = {"running", "pending", "created", "scheduled"}

#: A pipeline that exists and has no jobs at all. Deliberately not
#: `_checks.NO_CHECKS`, whose words are "no check runs on this commit" — the
#: GitHub unit and the GitHub anchor. Sharing the classifier does not mean
#: sharing a sentence about a different object.
_NO_JOBS = "none — the jobs API reports no job on this pipeline"


def _pipeline_leg_lines(pipe_id: str | int,
                        cap: int = _checks.NAMED_CAP) -> list[str]:
    """`  legs: <tally>` plus a named line per non-passing status. ONE fetch.

    The tally is #1607 item 1 and it is the reason this function exists in the
    render at all: `pipeline: success` is one word, and #445/#454's arithmetic
    is what turns it into a statement a merge can be decided on. Every term
    after `N total` sums back to N, so a status nobody has taught this tool
    about surfaces as its own term instead of evaporating — see
    `presets/_checks.summarize`, which is called rather than re-implemented.

    The names below answer a different question ("what do I go and look at"),
    mirror `gl-pipeline:ID:failed`'s shape rather than inventing a second one,
    and come off the same fetch, so they cost no further round trip.

    One `glab api` call, and the caller buys it whenever the MR has a pipeline
    at all. Not gated on the pipeline being red, because a green pipeline with
    four `skipped` legs is exactly the case the tally exists for and gating on
    redness cannot see it. The in-repo precedent for paying on the merge-gate
    render is `presets/github/pr.py`'s `_declared_for_commit`, which fires 1+N
    requests on every `gh-pr:N:status` including the green ones; #815 forbids
    buying a round trip *silently*, and this one is in the op's own docs.
    """
    if not pipe_id:
        return []
    jobs, reason, capped = _fetch_tally(
        f"projects/:id/pipelines/{pipe_id}/jobs?per_page=100", "jobs")
    if reason is not None:
        # Was four `return []` branches. This list is only ever fetched once
        # the caller has decided the pipeline is worth naming legs for, so an
        # empty return read as "nothing to name" on exactly the render where
        # something needed naming — and slim is the poll-loop render, read
        # most often and looked at least closely (#812). A zeroed tally would
        # be the same defect with arithmetic on top: `0 failed, 0 pending`
        # reads as "everything is accounted for" over a read nobody completed.
        return [f"  legs: UNKNOWN — {reason}"]

    entries, skipped = _dict_elements(jobs)
    # An element `_dict_elements` could not read is still a leg of this
    # pipeline. Tallying only what parsed would make the terms sum to fewer
    # than the pipeline has and call the difference nothing; `None` normalises
    # to UNKNOWN and takes its own term, so the count stays honest and the
    # `_unreadable` note below says why. (Same fix, two surfaces: #1517 added
    # the note, this adds the arithmetic it belongs to.)
    states = [j.get("status") for j in entries] + [None] * skipped
    lines = [f"  legs: {_checks.summarize(states) if states else _NO_JOBS}"]

    groups: dict[str, list[tuple[str, str]]] = {}
    for j in entries:
        status = str(j.get("status") or "").strip().lower()
        if not status or status == "success" or status in _GL_JOB_RESOLVES_ITSELF:
            continue
        # A job name is written in the source branch's .gitlab-ci.yml, so
        # whoever opened the MR chose it. This block is the `:status` render —
        # the one a poll loop reads every tick and looks at least closely — and
        # a name carrying a separator would otherwise be its own line in it
        # (#982). `gl-pipeline` already flattens the same field from the same
        # endpoint; this is the second reader adopting the first one's rule.
        name = _untrusted.flat(str(j.get("name") or "?"))
        job_id = str(j.get("id") or "")
        groups.setdefault(status, []).append((name, job_id))

    named = 0
    for label in sorted(groups):
        items = groups[label]
        shown = items[:cap]
        parts = [f"{n} (job #{jid})" if jid else n for n, jid in shown]
        text = ", ".join(parts)
        if len(items) > cap:
            text += f", +{len(items) - cap} more"
        lines.append(f"  {_untrusted.flat(label)}: {text}")
        named += 1
    if not named and states:
        lines.append("  jobs: none non-passing reported for this pipeline")
    if capped:
        # The one that has to be said out loud: "none non-passing" off a full
        # page is a statement about the first hundred jobs, and it reads as a
        # statement about the pipeline (#1517).
        lines.append(_PAGE_FULL)
    note = _unreadable(skipped, len(jobs), "pipeline jobs")
    if note:
        lines.append(note)
    return lines


def _unresolved_thread_lines(discussions: list, capped: bool) -> list[str]:
    """`Unresolved threads: N / M`, plus whatever qualifies it.

    Pulled out of `main` so the tally can be asserted at all (#1517). Both
    numbers used to be printed bare off one `per_page=100` page — on a merge
    blocker, in the render a poll loop reads every tick.
    """
    threads, bad_threads = _dict_elements(discussions or [])
    bad_notes = notes_total = resolvable = unresolved = 0
    for entry in threads:
        # A `notes` field that is not an array counts as one unreadable note:
        # `(x or [])` let a string through and the loop iterated its characters.
        thread_notes, bad, seen = _array_elements(entry.get("notes"))
        bad_notes += bad
        notes_total += seen
        marked = [n for n in thread_notes if n.get("resolvable")]
        if not marked:
            continue
        resolvable += 1
        if not all(n.get("resolved") for n in marked):
            unresolved += 1
    lines = [f"Unresolved threads: {_floor(unresolved, capped)} / "
             f"{_floor(resolvable, capped)}"]
    if capped:
        lines.append(_PAGE_FULL)
    for note in (_unreadable(bad_threads, len(discussions or []), "discussions"),
                 _unreadable(bad_notes, notes_total, "discussion notes")):
        if note:
            lines.append(note)
    return lines


def _failed_jobs_block(jobs: list, capped: bool) -> list[str]:
    """The failed-jobs section, in one place so its count can be asserted.

    A failed pipeline whose failed-jobs list is empty is a real and surprising
    answer — a blocked stage, a runner that never picked anything up. Printing
    nothing rendered it as though the block had never been asked for.
    """
    named, bad_jobs = _dict_elements(jobs or [])
    if named:
        lines = [f"Failed jobs ({_floor(len(named), capped)}):"]
        lines.extend(_failed_job_lines(named))
    else:
        lines = ["Failed jobs: none — the jobs API reports no failed job "
                 "on this failed pipeline"]
    if capped:
        lines.append(_PAGE_FULL)
    note = _unreadable(bad_jobs, len(jobs or []), "failed jobs")
    if note:
        lines.append(note)
    return lines


def _failed_job_lines(named: list[dict]) -> list[str]:
    """`  #{id} | {name} | {stage}` per failed job, one job to one line.

    Extracted from `main()` so the flattening has somewhere to be asserted:
    `name` and `stage` are both the MR author's words out of .gitlab-ci.yml
    (#982), and this is the block a reader scans to decide what broke.
    """
    flat = _untrusted.flat
    return [
        f"  #{flat(str(j.get('id', '?')))} | {flat(str(j.get('name', '?')))} "
        f"| {flat(str(j.get('stage', '?')))}"
        for j in named
    ]


def _local_branch_check(source: str) -> str:
    """Return a one-line local-branch-vs-MR-source check.

    Delegated to `_branch_locale` (#850): a branch held by a linked worktree is
    neither a match nor a MISMATCH, and saying MISMATCH there prescribed a
    checkout git refuses. The DVSI project this op targets is worktree-capable,
    so the defect was latent here rather than absent.
    """
    return _branch_locale.check(source)


_MERGE_TREE_NOISE_PREFIXES = (
    "Auto-merging ",
    "CONFLICT ",
    "warning:",
    "hint:",
    "error:",
)


def _get_conflicting_files(source: str, target: str) -> list[str]:
    """Find files in conflict between source and target via local git.

    Uses `git merge-tree --name-only --write-tree` (git 2.38+). Returns
    deduplicated list of conflicting paths, or empty list on any failure
    (not a git repo, refs not fetched, old git version, cwd outside the
    repo). Caller falls back to the plain "Conflicts: YES" message in
    that case.
    """
    try:
        result = subprocess.run(
            ["git", "merge-tree", "--name-only", "--write-tree",
             f"origin/{target}", f"origin/{source}"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    # Exit code 0 = clean merge; >0 = conflicts, file list + git status messages on stdout.
    if result.returncode == 0:
        return []
    files: list[str] = []
    seen: set[str] = set()
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        if re.fullmatch(r"[0-9a-f]{40}", line):
            # First line of merge-tree output is the merged tree hash.
            continue
        if any(line.startswith(p) for p in _MERGE_TREE_NOISE_PREFIXES):
            # Git's status messages get mixed into stdout; drop them.
            continue
        files.append(line)
        seen.add(line)
    return files


_MERGE_TREE_HEADER_RE = re.compile(
    r"^(changed in both|added in (?:local|remote)|removed in (?:local|remote))\b"
)
_MERGE_TREE_PATH_RE = re.compile(  # anchored-ok: matched per line of merge-tree output
    r"^  (?:base|our|their)\s+\d+\s+[0-9a-f]+\s+(.+)$"
)
HUNK_LINES_PER_FILE = 40
BINARY_HUNK_NOTE = "(binary file — conflict hunks not shown; resolve by picking a version)"

HUNK_TIMEOUT_BASE = 15
HUNK_TIMEOUT_PER_FILE = 5
HUNK_TIMEOUT_MAX = 60


def _hunk_timeout(file_count: int) -> int:
    """Seconds to allow `git merge-tree` for a hunk preview.

    Wall time is git's merge computation, and that scales with how many
    files it has to merge, so a flat 15s is a floor rather than a budget:
    generous for the one-conflicted-text-file case, thin for a branch with
    a dozen conflicted files or a cold object cache — which is exactly
    where the preview is worth most (#507). Grows 5s per conflicted file,
    capped at 60s so a pathological repo still returns a dashboard in
    bounded time rather than hanging the op.
    """
    return min(HUNK_TIMEOUT_MAX, max(HUNK_TIMEOUT_BASE, HUNK_TIMEOUT_PER_FILE * file_count))


def _hunk_display_lines(block: str) -> list[str]:
    """A conflict hunk as the rows the render prints under `  `.

    The other half of #1119's narrowing, and the reason it is a fix rather than
    a quieter bug. `_get_conflict_hunks` no longer consumes the eight
    separators `str.splitlines()` honours, so they arrive alive in the block —
    deliberately, because a caller of that function wants the conflicted file's
    own bytes. They are disclosed here instead, one step before they would
    move the terminal to a fresh row with no `  ` indent and produce a line the
    reader attributes to supertool (#851).

    Called after `_is_binary_hunk`, never before: a mangled blob is labelled
    and skipped rather than pictured character by character.

    Tabs are kept — a hunk is a block and its indentation is the file's
    content, the same call `presets/gitlab/job.py:_log_lines` makes.
    """
    return [_untrusted.visible(line, keep=chr(9))
            for line in _untrusted.split_lines(block)]


def _is_binary_hunk(block: str) -> bool:
    """True when a hunk block came from a non-text blob.

    `git merge-tree` writes conflicting blob *content* to stdout, so a
    conflicted image/PDF/font lands here as raw bytes. Those are decoded
    with errors="replace" (see `_get_conflict_hunks`), which turns every
    undecodable byte into U+FFFD — the marker this checks for, alongside a
    literal NUL for blobs that happen to decode. Printing 40 lines of
    mojibake helps nobody; the caller prints BINARY_HUNK_NOTE instead, so
    the file is still named rather than silently skipped.
    """
    return "\x00" in block or "�" in block


def _get_conflict_hunks(
    source: str, target: str, file_count: int = 0,
) -> tuple[dict[str, str], str | None]:
    """Return per-file conflict diff for hunk preview, plus why it is missing.

    Uses the older `git merge-tree BASE TARGET SOURCE` syntax which
    produces unified-diff-style output with `<<<<<<< / ======= / >>>>>>>`
    conflict markers. Each per-file block is split off the section
    headers ("changed in both", "added in local", etc.).

    Returns `(hunks, skip_reason)`. `skip_reason` is None when git
    answered — including when the honest answer was "no hunks" — and a
    short human-readable cause when it did not: a timeout, a non-zero
    exit, or an OS-level failure to run git at all. The two absences are
    not the same fact, and collapsing them into a bare `{}` is what made
    a timed-out preview render identically to a genuinely empty one
    (#507). The caller renders them differently.

    A skip never means the conflicted *file list* is wrong: that comes
    from `_get_conflicting_files`, a separate `--write-tree --name-only`
    call which carries no blob content and so cannot hit this timeout
    (#501).

    stdout here is blob content, not porcelain, so it is not text: one
    conflicted PNG puts a 0x89 on the stream and a strict UTF-8 decode
    takes the whole op down mid-render (#498). Decoding is therefore
    explicitly utf-8 with errors="replace" — the parser needs line
    structure, not exact bytes, and the caller labels the mangled blocks
    via `_is_binary_hunk` rather than printing them. errors= alone would
    still leave the codec at the locale default, so the encoding is
    pinned too: git writes UTF-8 regardless of what LANG says.
    """
    try:
        base_result = subprocess.run(
            ["git", "merge-base", f"origin/{target}", f"origin/{source}"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {}, "git merge-base timed out after 5s"
    except OSError as exc:
        return {}, f"could not run git: {exc}"
    if base_result.returncode != 0 or not base_result.stdout.strip():
        return {}, (
            "git merge-base found no common ancestor "
            f"(origin/{target} and origin/{source} may not be fetched — try: git fetch origin)"
        )
    base = base_result.stdout.strip()

    timeout = _hunk_timeout(file_count)
    try:
        result = subprocess.run(
            ["git", "merge-tree", base,
             f"origin/{target}", f"origin/{source}"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {}, f"git merge-tree timed out after {timeout}s"
    except OSError as exc:
        return {}, f"could not run git: {exc}"
    if result.returncode != 0 and not result.stdout:
        # The third of this file's stderr relays, swept with the two #1654
        # named. `flat` was already here and answers forgery; `split_lines`
        # answers loss. A U+2028 in git's one line made `str.splitlines()`
        # keep only what sat before it, and `merge-tree`'s arguments are
        # `origin/<source>` — a branch name the MR's author chose.
        detail = _untrusted.split_lines((result.stderr or "").strip())
        suffix = f": {_untrusted.flat(detail[0])}" if detail else ""
        return {}, f"git merge-tree failed (exit {result.returncode}){suffix}"
    if not result.stdout:
        return {}, None

    blocks: dict[str, list[str]] = {}
    current_path: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        if current_path:
            blocks.setdefault(current_path, []).extend(current_lines)

    # `_untrusted.split_lines`, never `str.splitlines()` (#1119, the audit that
    # issue asked for rather than the site it named). This stream is not
    # porcelain: it is the two branches' CONFLICTED FILE CONTENT, verbatim,
    # decoded with errors="replace". Both readers below anchor at column 0, and
    # a `_MERGE_TREE_HEADER_RE` match calls `_flush()` and clears
    # `current_path` — so a line of a conflicted file carrying U+2028 before
    # the text `changed in both` ended that file's hunk early and dropped every
    # line after it. The render then printed a conflict preview under a heading
    # naming the file, with the conflict itself missing: an absence produced by
    # the tool, read as an absence in the world (docs/validators.md).
    #
    # Verified against real `git merge-tree` output, not reasoned about. The
    # sibling `_get_conflicting_files` is deliberately NOT changed: git
    # octal-quotes non-ASCII bytes in any path it prints, so a filename cannot
    # carry a separator into that split, and narrowing it would assert a
    # guarantee that call does not need.
    #
    # `visible()` is applied at the render (`  {line}` under `### path`), not
    # here, because a caller of this function wants the file's own bytes.
    for line in _untrusted.split_lines(result.stdout):
        if _MERGE_TREE_HEADER_RE.match(line):
            _flush()
            current_path = None
            current_lines = []
            continue
        path_match = _MERGE_TREE_PATH_RE.match(line)
        if path_match:
            # Header is followed by 1-3 path lines (base/our/their) — same path.
            if current_path is None:
                current_path = path_match.group(1)
            continue
        if current_path is not None:
            current_lines.append(line)
    _flush()

    return (
        {p: "\n".join(lines).strip() for p, lines in blocks.items() if any(lines)},
        None,
    )


# #924 lifted these to `presets/_refname.py`. They stayed private here from
# #694 until `_branch_locale` needed the same rule and could not reach it, and
# a rule whose entire value is being the same rule at every sink is the wrong
# thing to hold two copies of. The names are kept so this file reads as it did.
_ORDINARY_REF = _refname.ORDINARY_REF
_ordinary_ref = _refname.ordinary
_shell_ref = _refname.shell_ref
_ref_warning = _refname.warning


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if _status_probe.says_not_found(s):
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.gl_not_found_hint()}")
    # `_secrets.mentions_gitlab_token`, not a literal: this line read `glpat_`
    # until #1645, GitLab mints `glpat-`, and the only test over it used the
    # same wrong spelling. One list, cited to GitLab's docs, in one file.
    # A status, never a number (#1846). go-gitlab echoes the request URL into
    # every error string, so a project, job or pipeline id containing `401`
    # made a 500 or a throttle render as a missing credential.
    if (_auth_probe.says_not_authenticated(s, _auth_probe.GITLAB_MARKERS)
            or _secrets.mentions_gitlab_token(s)):
        return "ERROR: glab not authenticated. Run: glab auth login"
    if _status_probe.says_forbidden(s):
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."
    # The remote host wrote this text — flattened, never relayed raw (#1485).
    return (f"ERROR: glab failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _render_note(note: dict, cap: int | None = COMMENT_MAX, *,
                  level: str = _CLASSIFY_LEVEL,
                  budget: "_classify_render.Budget | None" = None) -> str:
    """Format one MR note for printing, saying so when the body is cut.

    A `cap` of None is the `:full` path, which had no way to ask for one: every
    note was sliced at COMMENT_MAX with no marker, on `:full` as well, while the
    op's docs promised `:full` uncapped "the file list and the comments". It
    uncapped how many comments printed, never how much of each — the same
    half-working escape hatch #698 found in this file's description handling,
    caught by the check #719 asked for.

    The body is fenced (#694): a note reproducing this very format string used
    to render as a second, earlier note that the MR never held. The cut notice
    is supertool's own, so it is appended after the fence closes rather than
    inside it — see the same call in gh-issue.

    `level`/`budget` are the classify verdict for this note (#2049). `level`
    defaults to the module-level `_CLASSIFY_LEVEL` (read from
    `SUPERTOOL_CLASSIFY` at import time), never a hardcoded `full` — a
    caller that does not pass `level` explicitly (a unit test exercising
    this function directly, say) must still respect the same suite-wide
    `SUPERTOOL_CLASSIFY=off` default `tests/conftest.py` sets, or it silently
    reaches a real model spawn regardless of that default (found composing
    against a rebase onto #2064: `tests/test_gitlab_mr.py::
    test_budgeted_comments_hidden_bytes_counts_utf8` is a pre-existing test
    that calls this function directly and never passed `level`).
    `budget`
    is `None` only for a caller that never intends to spend the call's shared
    spawn budget here — every real caller below passes the one `Budget`
    instance for the whole call, so the cap applies across notes rather than
    per note.
    """
    author = _untrusted.flat(_as_dict(note.get("author")).get("username", "?"))
    body = note.get("body") or ""
    trunc = ""
    if cap is not None and len(body) > cap:
        body = body[:cap]
        trunc = f"\n{_body.comment_cut_notice(cap)}"
    created = (note.get("created_at") or "")[:10]
    if budget is not None:
        verdict = budget.line(body, level=level)
    else:
        verdict = _classify_render.verdict_line(body, level=level)
    return f"\n**{author}** ({created}):\n{_untrusted.fence(body)}{trunc}\n{verdict}\n"


def _fmt_kb(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes}B"
    return f"{nbytes / 1024:.1f}KB"


def _budgeted_comments(notes: list, budget: int, tail: int, *,
                        classify_level: str = _CLASSIFY_LEVEL,
                        classify_budget: "_classify_render.Budget | None" = None,
                        ) -> tuple[list[str], int, int]:
    """Pick rendered notes fitting a total-char budget, keeping the last `tail` for recency.

    Returns (rendered_lines, hidden_count, hidden_bytes). Notes are assumed
    sorted ascending (oldest first) — same order as the GitLab API.

    `classify_level`/`classify_budget` reach every note's own `_render_note`
    call (#2049). A real limitation, stated rather than hidden: every note
    is rendered — and so classified — before this function decides which
    ones the char budget below actually keeps, so a note later hidden here
    can still have spent classify's own per-call spawn budget. Fixing the
    ordering would mean classifying only after selection, which this
    function's own char-budget math cannot do (it needs each note's
    rendered length, verdict line included, to decide what fits) without
    rendering twice.
    """
    rendered_all = [_render_note(n, level=classify_level, budget=classify_budget)
                     for n in notes]
    if not rendered_all:
        return [], 0, 0
    tail_keep = min(tail, len(rendered_all))
    if tail_keep >= len(rendered_all):
        return rendered_all, 0, 0
    tail_slice = rendered_all[-tail_keep:] if tail_keep else []
    head_pool = rendered_all[:-tail_keep] if tail_keep else rendered_all
    tail_size = sum(len(r) for r in tail_slice)
    remaining = max(0, budget - tail_size)
    head_kept: list[str] = []
    used = 0
    for r in head_pool:
        if used + len(r) > remaining:
            break
        head_kept.append(r)
        used += len(r)
    hidden = head_pool[len(head_kept):]
    hidden_bytes = sum(len(r.encode("utf-8")) for r in hidden)
    return head_kept + (["__GAP__"] if hidden else []) + tail_slice, len(hidden), hidden_bytes


def _name_status_flag(f: dict) -> str:
    """Map a GitLab diff entry to a one-char change type (A/D/R/M)."""
    if f.get("new_file"):
        return "A"
    if f.get("deleted_file"):
        return "D"
    if f.get("renamed_file"):
        return "R"
    return "M"


class _NameStatus(NamedTuple):
    """Diff entries for an MR, plus the entries that were not objects (#735)."""

    entries: list[tuple[str, str]]
    skipped: int
    reason: str | None = None
    """Why the list is short or absent — `None` when the endpoint answered.

    An empty `entries` with no reason is "this MR changes nothing"; an empty
    `entries` with a reason is "nobody could read the diff". They rendered
    identically — as no `## Files` block at all — until #812."""

    @property
    def total(self) -> int:
        return len(self.entries) + self.skipped


def _get_name_status(iid: str | int, fetch_all: bool) -> _NameStatus:
    """Return per-file (flag, path) for an MR via the paginated diffs endpoint.

    Default fetches only the first page (100 files) — enough for the display
    cap. With fetch_all (gl-mr:N:full) it paginates up to NAMESTATUS_FETCH_CAP
    files.

    Carries `reason` for the same purpose `_get_conflict_hunks` carries
    `skip_reason`: the four `break`s here used to return a bare `[]`, and the
    caller omitted the whole `## Files` block on it — so a diffs endpoint that
    timed out rendered exactly like an MR that changed nothing (#812). A
    failure on page 3 is worse than either, because the entries already
    collected are real and the shortfall is invisible; the reason survives
    alongside them so the caller can say the list is short and why.
    """
    entries: list[tuple[str, str]] = []
    skipped = 0
    reason: str | None = None
    page = 1
    while True:
        diffs, reason = _fetch_array(
            f"projects/:id/merge_requests/{iid}/diffs?per_page=100&page={page}",
            "diffs")
        if reason is not None or not diffs:
            break
        files, bad = _dict_elements(diffs)
        skipped += bad
        for f in files:
            flag = _name_status_flag(f)
            new_path = f.get("new_path") or ""
            old_path = f.get("old_path") or ""
            if flag == "R" and old_path and new_path and old_path != new_path:
                path = f"{old_path} → {new_path}"
            else:
                path = new_path or old_path or "?"
            entries.append((flag, path))
        if not fetch_all or len(diffs) < 100 or len(entries) >= NAMESTATUS_FETCH_CAP:
            break
        page += 1
    return _NameStatus(entries, skipped, reason)


def _coerce_count(changes: object) -> int | None:
    """Return the leading integer of GitLab's changes_count.

    changes_count comes back as a string — "18" on normal MRs, "1000+" when
    capped. Returns the leading int (1000 for "1000+"), or None when there are
    no leading digits, so callers can fall back to the fetched-entry count.
    """
    m = re.match(r"\d+", str(changes))
    return int(m.group()) if m else None


def _render_name_status(
    name_status: _NameStatus, changes: object, full: bool, iid: str | int
) -> list[str]:
    """Build the '## Files' block lines from name-status entries.

    Returns [] only when the fetch succeeded and found nothing — the one case
    where omitting the block is honest, because the block was never asked for.
    An empty list carrying a `reason` prints the heading and the decline: the
    caller reached here because `changes_count` said there were files, so a
    missing block is a claim that contradicts the line above it (#812).

    The total file count drives the "+N more" overflow line: it comes from
    changes_count (authoritative, survives the display cap and single-page
    fetch) and falls back to the fetched count when changes_count is missing
    or smaller.
    """
    entries = name_status.entries
    reason = name_status.reason
    if not entries:
        if reason is None:
            return []
        heading = f"\n## Files ({changes})"
        return [heading, f"  ! file list unavailable — {reason}"]
    shown = entries if full else entries[:NAMESTATUS_DISPLAY_MAX]
    total = _coerce_count(changes)
    if total is None or total < len(entries):
        total = len(entries)
    lines = [f"\n## Files ({changes})"]
    lines.extend(f" {flag}  {path}" for flag, path in shown)
    hidden = total - len(shown)
    if hidden > 0:
        if reason is not None:
            # Not a display cap, so it must not be described as one: the old
            # line blamed the cap and pointed at `:full`, advice that cannot
            # work for a shortfall the tool itself caused (#812).
            lines.append(f" … +{hidden} more not fetched — {reason}")
        elif full:
            lines.append(f" … +{hidden} more (output capped at {NAMESTATUS_FETCH_CAP} files)")
        else:
            lines.append(f" … +{hidden} more (use gl-mr:{iid}:full)")
    elif reason is not None:
        lines.append(f"  ! file list may be incomplete — {reason}")
    note = _unreadable(name_status.skipped, name_status.total, "changed files")
    if note:
        lines.append(note)
    return lines


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: mr.py NUMBER [status|full]")
        return 1

    arg = sys.argv[1]
    flags = sys.argv[2:]
    slim = "status" in flags
    full = "full" in flags

    # If not all digits, treat as branch name and resolve to MR number
    if not arg.isdigit():
        try:
            branch_result = _glab_api(
                f"projects/:id/merge_requests?source_branch={arg}&state=opened&per_page=1"
            )
            if branch_result.returncode == 0:
                mrs = json.loads(branch_result.stdout)
                found, unreadable = _dict_elements(mrs)
                if found:
                    arg = str(found[0].get("iid", arg))
                elif unreadable:
                    print(f"ERROR: branch lookup for {arg!r} returned {unreadable} MR(s) "
                          f"with a shape supertool could not read")
                    return 1
                else:
                    # Try all states if no open MR found
                    branch_result2 = _glab_api(
                        f"projects/:id/merge_requests?source_branch={arg}&per_page=1"
                    )
                    if branch_result2.returncode == 0:
                        mrs2 = json.loads(branch_result2.stdout)
                        found2, unreadable2 = _dict_elements(mrs2)
                        if found2:
                            arg = str(found2[0].get("iid", arg))
                        elif unreadable2:
                            print(f"ERROR: branch lookup for {arg!r} returned {unreadable2} "
                                  f"MR(s) with a shape supertool could not read")
                            return 1
                        else:
                            print(f"ERROR: no MR found for branch {arg!r}")
                            return 1
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"ERROR: branch lookup failed: {e}")
            return 1

    try:
        result = _glab(["mr", "view", arg, "--output", "json"])
    except FileNotFoundError:
        print("ERROR: glab not found — install from https://gitlab.com/gitlab-org/cli")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "MR", arg))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from glab\n{result.stdout[:500]}")
        return 1

    # `glab mr view --output json` is documented as returning one object. It was
    # parsed and then used with no type check at all — the top-level gap #720
    # closed on the approvals and issue payloads, still open on the payload the
    # whole render is built from (#735).
    if not isinstance(d, dict):
        print(f"ERROR: glab mr view returned a {type(d).__name__}, expected an object")
        return 1

    def _latest_pipeline(iid: str | int) -> tuple[dict, str | None]:
        """Freshest pipeline for the MR, and why there is none.

        `glab mr view` can return a stale `head_pipeline`, so this asks the
        pipelines endpoint directly. `reason` is None when that endpoint
        answered — *including* when the honest answer was "this MR has no
        pipelines" — and a sentence when it did not.

        Collapsing those two into a bare `{}` is the defect in #812: the
        caller's `pipeline.get("status", "none")` then printed `Pipeline: none`
        for a lookup nobody could complete, which is indistinguishable from an
        MR that genuinely never started one. Worth naming plainly, because the
        two point opposite ways: "no pipeline" is a reason to merge and "could
        not read the pipeline" is a reason not to.
        """
        pipes, reason = _fetch_array(
            f"projects/:id/merge_requests/{iid}/pipelines?per_page=1", "pipelines")
        if reason is not None:
            return {}, reason
        found, _ = _dict_elements(pipes or [])
        return (found[0] if found else {}), None

    def _pipe_meta(pipeline: dict) -> str:
        """One-liner: SHA + when + source + user + duration/elapsed + coverage."""
        bits = []
        sha = (pipeline.get("sha") or "")[:8]
        if sha:
            bits.append(sha)
        # Source (push, merge_request_event, schedule, web, api, trigger, ...)
        source = pipeline.get("source")
        if source and source not in ("push",):
            bits.append(source)
        # Who triggered
        user = _as_dict(pipeline.get("user")).get("username")
        if user:
            bits.append(f"by {user}")
        # Time: running pipelines show elapsed; others show finished/updated
        status = pipeline.get("status", "")
        if status == "running":
            started = pipeline.get("started_at") or pipeline.get("created_at")
            if started:
                from datetime import datetime, timezone
                try:
                    dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - dt).total_seconds()
                    if elapsed > 60:
                        bits.append(f"running {int(elapsed // 60)}m")
                    else:
                        bits.append(f"running {int(elapsed)}s")
                except (ValueError, TypeError):
                    pass
        else:
            updated = (pipeline.get("updated_at") or pipeline.get("created_at") or "")[:19].replace("T", " ")
            if updated:
                bits.append(updated)
        duration = pipeline.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            mins, secs = divmod(int(duration), 60)
            bits.append(f"{mins}m{secs:02d}s" if mins else f"{secs}s")
        coverage = pipeline.get("coverage")
        if coverage is not None:
            bits.append(f"cov {coverage}%")
        return " | ".join(bits)

    if slim:
        iid = d.get("iid", arg)
        state = d.get("state", "?")
        # Same expression as `:full` below, and it used to be `.get(k, "?")`
        # (#628). Two ways that answered worse on the render read most often:
        # a present-but-empty `merge_status` never reached the default, so the
        # line rendered blank — which reads as a rendering bug rather than an
        # unanswered question; and GitLab deprecated `merge_status` in favour
        # of `detailed_merge_status`, so an instance that has stopped
        # populating the old key got `?` over a value already in this payload.
        merge_status = (d.get("merge_status")
                        or d.get("detailed_merge_status") or "?")
        has_conflicts = d.get("has_conflicts", False)
        fresh, pipe_reason = _latest_pipeline(iid)
        pipeline = fresh or _as_dict(d.get("pipeline")) or _as_dict(d.get("head_pipeline"))
        pipe_status = pipeline.get("status", "none")
        pipe_id = pipeline.get("id", "")
        merged_at = d.get("merged_at") or "-"
        merge_commit = d.get("merge_commit_sha") or d.get("squash_commit_sha") or ""
        web_url = d.get("web_url", "")
        print(f"!{iid} | state: {state} | merge_status: {merge_status} | conflicts: {'yes' if has_conflicts else 'no'}")
        print(f"branch: {_untrusted.flat(d.get('source_branch') or '?')} -> "
              f"{_untrusted.flat(d.get('target_branch') or '?')}")
        # One extra `glab api` round trip, bought unconditionally — the same
        # #815 disclosure the leg tally below already makes (#1607 item 2).
        # `:status` is the poll-loop render a maintainer calls ad hoc to
        # decide whether an MR is mergeable, and approval state answers
        # exactly that question; `_approvals_line` already carries its own
        # three states (approved/none/UNKNOWN), so reusing it here rather
        # than reporting "no approvals" for "did not ask" costs nothing new
        # to get right. NOT paid by the gitlab-mr watch poller, which reads
        # the MR endpoint directly rather than calling this op (poller.py).
        #
        # `_approvals_line`'s own wording ("Approved by: ...") is #720's pinned
        # contract for the FULL dashboard and is left untouched; slim's own
        # casing convention is lowercase/underscored labels (`merge_status:`,
        # `pipeline:`), the same split documented for every other slim field,
        # so the prefix is reformatted to match rather than mixing the two
        # styles on one render — the value and its three states are identical.
        print(_approvals_line(iid).replace("Approved by:", "approved_by:", 1))
        if pipe_reason is not None and not pipeline:
            # Slim is the poll-loop render — read most often, looked at least
            # closely — so it is the one where `pipeline: none` from a failed
            # lookup does the most damage (#812).
            print(f"pipeline: UNKNOWN — {pipe_reason}")
        else:
            pipe_str = pipe_status + (f" (#{pipe_id})" if pipe_id else "")
            meta = _pipe_meta(pipeline)
            if meta:
                pipe_str += f" | {meta}"
            print(f"pipeline: {pipe_str}")
            if pipe_reason is not None:
                print(f"  ! live pipeline lookup declined ({pipe_reason}) — status "
                      f"above comes from the MR payload and can be stale")
        # One extra `glab api` round trip, bought whenever there is a pipeline
        # to count — including a green one (#1607).
        #
        # It used to be gated on `pipe_status not in (success, "", pending,
        # created, scheduled)`, citing #619 and `gh-pr`'s "only when the rollup
        # is empty" discipline. That gate is right for *naming* jobs and wrong
        # for *counting* them, and the two had been merged into one condition:
        # a pipeline GitLab calls `success` still reports `skipped`, `manual`
        # and allow-failure `canceled` jobs, so the legs that never ran were
        # invisible on precisely the poll a maintainer merges on. The GitHub
        # mirror of a tally is not the rollup (free, already in the payload) —
        # it is `_declared_for_commit`, which pays 1+N requests on every
        # `gh-pr:N:status`, green ones included, on #804's argument that a
        # request is cheaper than a merge on four green CodeQL legs.
        for line in _pipeline_leg_lines(pipe_id):
            print(line)
        print(f"merged_at: {merged_at}")
        if merge_commit:
            print(f"merge_commit: {merge_commit[:12]}")
        if web_url:
            print(f"url: {web_url}")
        return 0

    # One-line fields are flattened rather than fenced — see presets/_untrusted.py.
    title = _untrusted.flat(d.get("title", "?"))
    state = d.get("state", "?")
    iid = d.get("iid", arg)
    source = _untrusted.flat(d.get("source_branch", "?"))
    target = _untrusted.flat(d.get("target_branch", "?"))
    author = _untrusted.flat(_as_dict(d.get("author")).get("username", "?"))
    web_url = d.get("web_url", "")
    raw_labels = d.get("labels")
    labels = _untrusted.flat(", ".join(
        str(label) for label in raw_labels) if isinstance(raw_labels, list) else "none")
    labels = labels or "none"
    milestone = _untrusted.flat(_as_dict(d.get("milestone")).get("title", "none"))
    merge_status = d.get("merge_status") or d.get("detailed_merge_status") or "?"
    merge_commit = d.get("merge_commit_sha") or d.get("squash_commit_sha") or ""
    draft = d.get("draft", False) or d.get("work_in_progress", False)

    # Pipeline — fetch latest from MR pipelines endpoint (head_pipeline can be stale).
    # Three states, not two (#812, generalising #720): a status, a verified
    # `none`, and a stated UNKNOWN naming why nobody could tell.
    fresh_pipeline, pipe_reason = _latest_pipeline(iid)
    pipeline = (fresh_pipeline or _as_dict(d.get("pipeline"))
                or _as_dict(d.get("head_pipeline")))
    pipe_status = pipeline.get("status", "none")
    pipe_id = pipeline.get("id", "")
    pipe_meta = _pipe_meta(pipeline)
    # A live lookup that declined but left a usable payload fallback is not the
    # same fact as one that left nothing: the first prints a real status with
    # the staleness disclosed, the second has no status to print.
    pipe_unknown = pipe_reason is not None and not pipeline
    pipe_stale = pipe_reason is not None and bool(pipeline)

    # Diff stats
    changes = d.get("changes_count") or 0
    diff_stats = _as_dict(d.get("diff_stats"))
    additions = diff_stats.get("additions", 0)
    deletions = diff_stats.get("deletions", 0)

    # Reviewers
    reviewers, reviewers_skipped, reviewers_total = _array_elements(d.get("reviewers"))
    reviewer_names = [r.get("username", "?") for r in reviewers]

    # Description is cut here rather than at its print site, because the
    # disclosure belongs in the header below — and because :full documented
    # itself as uncapping the file list and comments while the description
    # stayed capped regardless (#698).
    description_raw = d.get("description") or ""
    description_total = len(description_raw)
    description, description_withheld = _body.cut(
        description_raw, None if full else DESCRIPTION_MAX)

    # Header. The fence convention is declared before the first thing inside a
    # fence — the reader this protects is the one who acts on the first line.
    draft_marker = " [DRAFT]" if draft else ""
    print(_untrusted.banner())
    print(f"# !{iid} {title}{draft_marker}")
    print(f"State: {state} | Author: {author}")
    print(f"Branch: {source} -> {target}")
    local_check = _local_branch_check(source)
    if local_check:
        print(local_check)
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    if description_withheld:
        # In the header, before ## Description — a footer-only notice is read
        # by nobody in exactly the case it exists for (#681, #698).
        print(_body.header_notice(
            description, description_total, description_withheld))

    # Assignees (distinct from reviewers on GitLab)
    assignees, assignees_skipped, assignees_total = _array_elements(d.get("assignees"))
    assignee_names = [a.get("username", "?") for a in assignees]
    print(f"Assignees: {', '.join(assignee_names) if assignee_names else 'none'}")
    _print_unreadable(assignees_skipped, assignees_total, "assignees")

    # Reviewers + approvals — always print so absence is signal, not silence
    print(f"Reviewers: {', '.join(reviewer_names) if reviewer_names else 'none'}")
    _print_unreadable(reviewers_skipped, reviewers_total, "reviewers")

    # Age — created/updated, for stale-MR signal
    created_at = d.get("created_at") or ""
    updated_at = d.get("updated_at") or ""
    if created_at:
        age_str = f"Created: {_relative_age(created_at)}"
        if updated_at and updated_at != created_at:
            age_str += f" | Updated: {_relative_age(updated_at)}"
        print(age_str)

    # Fetch approvals via API (glab mr view doesn't include this). Always one
    # line, in all three states, and never an exception — see _approvals_line.
    print(_approvals_line(iid))

    # Unresolved discussion threads — distinct blocker from comments, and one
    # line whatever happens (#812). The `except ...: pass` and the unentered
    # `isinstance` below it used to remove the line entirely, so a timeout on
    # this endpoint rendered as no `Unresolved threads:` at all: not a zero,
    # not a decline, nothing. An unresolved thread is a merge blocker, which
    # makes silence here the most expensive of the four.
    discussions, disc_reason, disc_capped = _fetch_tally(
        f"projects/:id/merge_requests/{iid}/discussions?per_page=100", "discussions")
    if disc_reason is not None:
        print(f"Unresolved threads: UNKNOWN — {disc_reason}")
    else:
        for line in _unresolved_thread_lines(discussions, disc_capped):
            print(line)

    # Pipeline + changes
    if pipe_unknown:
        print(f"Pipeline: UNKNOWN — {pipe_reason}")
    else:
        pipe_str = pipe_status
        if pipe_id:
            pipe_str += f" (#{pipe_id})"
        if pipe_meta:
            pipe_str += f" | {pipe_meta}"
        print(f"Pipeline: {pipe_str}")
        if pipe_stale:
            print(f"  ! live pipeline lookup declined ({pipe_reason}) — status "
                  f"above comes from the MR payload and can be stale")

    # Failed jobs — asked for exactly when the pipeline says it failed, so from
    # here on it prints a line whatever comes back (#812). It cannot fire on an
    # UNKNOWN pipeline: `pipe_status` is "none" there, which is the point —
    # a section prints nothing only when it was never asked for.
    if pipe_status == "failed" and pipe_id:
        jobs, jobs_reason, jobs_capped = _fetch_tally(
            f"projects/:id/pipelines/{pipe_id}/jobs?per_page=100&scope=failed",
            "jobs")
        if jobs_reason is not None:
            print(f"Failed jobs: UNKNOWN — {jobs_reason}")
        else:
            for line in _failed_jobs_block(jobs, jobs_capped):
                print(line)

    if additions or deletions:
        print(f"Changes: {changes} files, +{additions} -{deletions}")
    elif changes:
        # glab mr view omits diff_stats on large MRs (typically 1000+ files)
        print(f"Changes: {changes} files (line counts unavailable on large MRs)")

    # File-level name-status — the deletion/addition list is the high-signal
    # scan when reviewing an MR. Default-capped; gl-mr:N:full uncaps.
    if changes:
        for line in _render_name_status(_get_name_status(iid, full), changes, full, iid):
            print(line)

    # Conflicts — has_conflicts is a straight alias for cannot_be_merged?, which
    # GitLab also sets when the source branch has no commits (#494). This view
    # already fetches the full MR payload (detailed_merge_status, sha,
    # diff_refs), so _conflict_label can tell a real conflict from an empty
    # diff without an extra request. _get_conflicting_files only runs once we
    # know there is a diff to run it on.
    conflict_files: list[str] = []
    conflict_label = _conflict_label({**d, "_diff_refs": d.get("diff_refs")})
    if conflict_label == "conflict":
        conflict_files = _get_conflicting_files(source, target)
        if conflict_files:
            print(f"Conflicts: YES — cannot merge ({len(conflict_files)} file{'s' if len(conflict_files) != 1 else ''})")
        else:
            print("Conflicts: YES — cannot merge")
    elif conflict_label == "empty":
        print("Conflicts: NO — cannot merge: source branch has no commits, so there is nothing to merge")
    else:
        print(f"Merge status: {merge_status}")

    # Merge commit (if merged)
    if merge_commit:
        print(f"Merge commit: {merge_commit[:12]}")

    if web_url:
        print(f"URL: {web_url}")

    # Conflict file list + hunks — only when conflicts exist and we computed them
    if conflict_files:
        plural = "s" if len(conflict_files) != 1 else ""
        print(f"\n## Conflicts ({len(conflict_files)} file{plural})")
        for path in conflict_files:
            print(f"  {path}")

        hunks, hunks_skipped = _get_conflict_hunks(source, target, len(conflict_files))
        no_preview: list[str] = []
        for path in conflict_files:
            block = hunks.get(path, "")
            if not block:
                no_preview.append(path)
                continue
            if _is_binary_hunk(block):
                print(f"\n### {path}")
                print(f"  {BINARY_HUNK_NOTE}")
                continue
            lines = _hunk_display_lines(block)
            truncated = ""
            if len(lines) > HUNK_LINES_PER_FILE:
                extra = len(lines) - HUNK_LINES_PER_FILE
                lines = lines[:HUNK_LINES_PER_FILE]
                truncated = f"\n  ... ({extra} more lines)"
            print(f"\n### {path}")
            for line in lines:
                print(f"  {line}")
            if truncated:
                print(truncated)

        if hunks_skipped:
            # The tool failed to answer — say so rather than letting the
            # missing hunks read as "this conflict has none" (#507). The file
            # list above came from a different call and is untouched by this.
            print(f"\n  Hunk preview unavailable: {hunks_skipped}.")
            print("  The conflicted file list above is still accurate — it comes from a")
            print("  separate `git merge-tree --write-tree --name-only` call that carries")
            print("  no blob content, so it cannot fail this way.")
            print("  To see the hunks, run the merge locally with the commands below.")
        elif no_preview:
            plural_np = "s" if len(no_preview) != 1 else ""
            print(
                f"\n  No hunk preview for {len(no_preview)} file{plural_np}: "
                f"{', '.join(no_preview)}"
            )
            print("  — git merge-tree returned no conflict content there (add/add,")
            print("  delete/modify and rename conflicts have no inline hunks).")

        print("\nTo resolve:")
        ref_warning = _ref_warning([source, target, *conflict_files])
        if ref_warning:
            print(ref_warning)
        print(
            f"  git checkout {_shell_ref(source)} && git fetch origin "
            f"&& git merge origin/{_shell_ref(target)}"
        )
        files_arg = " ".join(_shell_ref(f) for f in conflict_files)
        print(f"  # Resolve <<<<<<< markers in the files above, then:")
        print(f"  git add {files_arg} && git commit && git push")

    # Linked issue — extract from the *uncut* description, so a reference the
    # cap happened to remove is still resolved.
    issue_match = re.search(r'#(\d{4,})', description_raw)
    if issue_match:
        issue_iid = issue_match.group(1)
        # Same three states as the approvals line above, and the same sweep
        # (#720): a non-zero exit printed nothing at all — the MR names an
        # issue and the section promising to describe it simply was not there —
        # and a payload that parsed to a non-object raised `AttributeError` out
        # of the render, this time taking the description and comments with it.
        unavailable = f"\nIssue: #{issue_iid} — details unavailable"
        try:
            issue_result = _glab_api(f"projects/:id/issues/{issue_iid}")
            if issue_result.returncode != 0:
                print(f"{unavailable} ({_glab_fail_detail(issue_result)})")
            else:
                issue_data = json.loads(issue_result.stdout)
                if not isinstance(issue_data, dict):
                    print(f"{unavailable} (issues API returned a "
                          f"{type(issue_data).__name__}, expected an object)")
                else:
                    issue_title = issue_data.get("title") or "?"
                    issue_state = issue_data.get("state") or "?"
                    raw_labels = issue_data.get("labels")
                    issue_labels = ", ".join(
                        str(label) for label in raw_labels
                    ) if isinstance(raw_labels, list) and raw_labels else "none"
                    raw_assignees = issue_data.get("assignees")
                    issue_assignees = ", ".join(
                        (a.get("username") or "?") if isinstance(a, dict) else "?"
                        for a in raw_assignees
                    ) if isinstance(raw_assignees, list) and raw_assignees else "none"
                    print(f"\n## Issue #{issue_iid} — {issue_title}")
                    print(f"State: {issue_state} | Labels: {issue_labels} | Assignees: {issue_assignees}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"{unavailable} ({type(e).__name__})")
        except OSError as e:
            print(f"{unavailable} (could not run glab: {e})")

    # Classify budget for this call (#2049) -- one call, one budget, spent
    # across the description and every note below, never per-block.
    classify_budget = _classify_render.Budget()

    # Description
    if description:
        print(f"\n## Description\n{_untrusted.fence(description)}")
        if description_withheld:
            print(f"\n{_body.cut_notice(description_withheld)}")
        print(classify_budget.line(description, level=_CLASSIFY_LEVEL))
    else:
        print("\n## Description\n_(empty)_")

    # Human comments (notes) — always print header so absence is signal,
    # not silence. Mirrors gh-pr behavior.
    human_notes: list = []
    notes_skipped = notes_seen = 0
    notes, notes_reason, notes_capped = _fetch_tally(
        f"projects/:id/merge_requests/{iid}/notes?per_page=50&sort=asc", "notes")
    if notes_reason is not None:
        # #812 files this one among the sections that vanish. It is not one:
        # the heading is unconditional, so a failed fetch rendered as
        # `## Comments (0)` — a *count*, which is a claim that the MR has none.
        # That is the `Pipeline: none` defect wearing a different hat, and the
        # worse of the two shapes: a missing line is at least visibly missing,
        # while a wrong number is indistinguishable from a right one.
        print("\n## Comments (UNKNOWN)")
        print(f"  ! comments could not be read — {notes_reason}")
        return 0
    readable, notes_skipped = _dict_elements(notes or [])
    notes_seen = len(notes or [])
    human_notes = [n for n in readable if not n.get("system", False)]

    # A fourth site of #1517's class, in the same render and not named by the
    # issue: 50 comments off a page of 50 made the heading claim the MR has
    # exactly 50. The comment on the UNKNOWN branch above is the whole argument
    # — a wrong number is indistinguishable from a right one, where a missing
    # line is at least visibly missing.
    print(f"\n## Comments ({_floor(len(human_notes), notes_capped)})")
    if notes_capped:
        print(_PAGE_FULL)
    _print_unreadable(notes_skipped, notes_seen, "comments")
    if full:
        for r in (_render_note(n, None, level=_CLASSIFY_LEVEL, budget=classify_budget)
                  for n in human_notes):
            print(r, end="")
    else:
        rendered, hidden_count, hidden_bytes = _budgeted_comments(
            human_notes, COMMENT_TOTAL_MAX, TAIL_COMMENTS,
            classify_level=_CLASSIFY_LEVEL, classify_budget=classify_budget,
        )
        for r in rendered:
            if r == "__GAP__":
                print(
                    f"\n... {hidden_count} more comment(s) hidden ({_fmt_kb(hidden_bytes)})."
                    f" Use gl-mr:{iid}:full for everything."
                )
                continue
            print(r, end="")

    return 0


if __name__ == "__main__":
    sys.exit(main())
