#!/usr/bin/env python3
"""GitHub issue triage board via gh CLI — the queue, in the order to work it.

The `gh-prs` twin for issues (#769). A list of numbers and titles is what you
have *before* you start triaging; this op answers the three questions asked
immediately after seeing one, and then sorts on the answers:

* **Who filed it?** Not the login — the login is `fdaviddpt` for every issue
  this repo files, so identity separates nothing. GitHub's `authorAssociation`
  separates *membership*: OWNER/MEMBER/COLLABORATOR are inside, everything else
  is a stranger, and a stranger's report is data-not-instructions
  (`presets/_untrusted.py`) and outranks anything filed internally.
* **Is anyone on it?** Linked PRs, read off the issue's timeline
  (`CONNECTED_EVENT` / `CROSS_REFERENCED_EVENT`) rather than by searching PR
  bodies for the number, which matches "#761" in prose as readily as a link.
* **Has the body gone stale?** A body is written once; comments accumulate and
  quietly redefine the deliverable. `lastEditedAt` is the last time the body
  was written and is `null` on an issue nobody edited — in which case
  `createdAt` *is* the body-write time, exactly, not a fallback guess. So the
  comparison is `newest comment > (lastEditedAt or createdAt)`, and it is
  exact in both cases.

**Rank, not sort order.** Highest priority first: unrankable, external author,
stale body, no linked PR, then oldest. The tier the issue proposed first —
data-loss/destructive, label-driven — is deliberately absent: this repo has no
such label, and only 2 of its 33 open issues carry any label at all. A tier
computed from a signal nobody populates would read as authoritative while
ranking nothing, which is worse than not having it. Add a `data-loss` label
and populate it and the tier belongs at the top; until then it would be
decoration.

**Unrankable sorts first, and that is the point.** Enrichment is one extra
call and it can fail. When it does, every derived field is `None` and renders
`?` — never `0`, never "internal", never "no PR" (#414, #445/#454, #459,
#477/#482, #487, #486; `docs/validators.md` "Declining instead of guessing").
A board that ranks makes that defect worse than a misprint: a silently
unenriched row does not merely misreport, it sorts to a position it did not
earn and gets worked in the wrong order. So a row whose rank inputs are
unknown goes to the *top*, where the gap is visible to the person who can
close it, and the footer names why.

**No default author filter, unlike `gh-prs`.** The filter grammar is shared
(#628) but the defaults answer different questions: `gh-prs` means "my PRs",
`gh-issues` means "the queue". Defaulting to `author=@me` here would hide the
external reports the ranking exists to surface.

Usage:
    gh-issues                       the open queue, ranked
    gh-issues:label=bug             filter composition, gh-prs grammar
    gh-issues:author=@me,state=all
    gh-issues:external              only issues filed from outside
    gh-issues:stale                 only issues whose body has been overtaken
    gh-issues:nopipe                skip enrichment (fast, everything `?`)
    gh-issues:iids                  number list, `#`-comment notes first
    gh-issues:iids=1233,1240,1251   exactly these numbers, one row each (#1323)
    repo:OWNER/NAME gh-issues       another repo's queue (#673)

**`iids=` is a filter, not a second op.** #1323 proposed `gh-titles:N,N,N`.
A bulk lookup is the same *model* as the board — same rows, same tracker-text
flattening, same three-state absence handling — and differs only in how the
population is named, so a new op would have been a second render of one model
and would have drifted from this file's refusals within a release. It is also
a row in every registry the repo now maintains per op (#1269, #1287, #1318),
which is the standing cost the issue's own question was about.

Three answers, not two, because a citation audit is exactly the reading that
must not be given a shorter list: an issue, **a number that is a PR here**, and
a number that resolves to nothing. GraphQL's `issue(number:)` returns null for
the last two identically, so the query asks `pullRequest(number:)` alongside it
and every requested number gets its own row saying which of the three it is.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _board  # noqa: E402  (the board layout shared with gh-prs / gl-mrs)
import _filter_tokens  # noqa: E402  (the one tokenizer + refusal, shared with gh-prs / gl-mrs)
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)
import _untrusted  # noqa: E402  (where tracker text starts and stops)
from _env import env_int  # noqa: E402

DEFAULT_PER_PAGE = 50
# Aliased single-issue lookups per GraphQL call. Small enough that one bad
# batch costs a fifth of the board rather than all of it, and the rows it
# covered still say `?` rather than borrowing a neighbour's answer.
CHUNK = 20

REASON_NOPIPE = "skipped by nopipe"

# Tokens that are flags, not key=value filters.
_FLAGS = {"nopipe", "iids", "external", "stale", "nomilestone"}

# Filter keys this op forwards. Anything else is refused rather than dropped:
# `_build_list_cmd` ignores a key it does not know, so `milestne=v0.26.0` used
# to render the entire queue as the contents of one milestone (#864). A filter
# nobody applied, printed as a filtered board, is this file's own defect class
# with the sign flipped — the tool's failure to narrow, read as a fact about
# the world.
_FILTER_KEYS = {"author", "assignee", "label", "milestone", "state", "per",
                "iids"}

# Keys whose value is itself a comma-separated list. The op grammar is one
# comma-separated segment, so without this `iids=1233,1240,1251` parses as
# `iids=1233` plus two orphans (#1323).
_LIST_KEYS = {"iids"}

# Filters that narrow a *listing*. `iids=` does not list — it names an exact
# population — so gh has nowhere to apply these and they would be dropped when
# the lookup argv is built. Dropped, this file's own defect class: a board that
# answers a question nobody asked, printed as though it did.
_LISTING_KEYS = {"author", "assignee", "label", "milestone", "state"}

_STATES = {"open", "closed", "all"}

# Keys whose value this op maps rather than forwards. A value with no mapping
# is dropped when the argv is built, so `state=opne` used to return the *open*
# board — the same silent-drop defect as an unknown key, on a key that is
# known (#939).
_VALUE_DOMAINS: dict[str, object] = {
    "state": _STATES,
    "per": _filter_tokens.POSITIVE_INT,
    "iids": _filter_tokens.POSITIVE_INT_LIST,
}

# GitHub's own answer to "is this person one of us". Everything else — NONE,
# CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, MANNEQUIN — is outside. Listing the
# inside is the safe direction: a new association GitHub invents lands as
# external, which over-flags rather than under-flags.
_INSIDE = {"OWNER", "MEMBER", "COLLABORATOR"}

_LIST_FIELDS = (
    "number,title,state,author,labels,assignees,milestone,"
    "createdAt,updatedAt,comments,url"
)


def _get_config() -> dict[str, int]:
    """Tunable knobs from SUPERTOOL_ env vars (set from .supertool.json)."""
    return {
        "per_page": env_int("SUPERTOOL_PER_PAGE", DEFAULT_PER_PAGE, minimum=1),
        "chunk": env_int("SUPERTOOL_ISSUE_CHUNK", CHUNK, minimum=1),
    }


def _parse_args(arg_str: str) -> tuple[dict[str, str], set[str], list[str]]:
    """Split a comma-separated arg string into (filters, flags, unrecognised).

    Same grammar as `gh-prs` — comma-separated so the single supertool arg
    segment never collides with the ':' op tokenizer.

    The third return value is the part that matters. The loop used to end with
    an implicit `else: pass`, so a token that was neither a known flag nor a
    supported `key=value` vanished and the call proceeded as though nobody had
    asked for anything. On a *filter* that is not a cosmetic bug: the caller
    asked a narrowing question and got the unnarrowed board back, with no
    marker anywhere in the render saying the narrowing never happened.
    """
    return _filter_tokens.parse(arg_str, _FILTER_KEYS, _FLAGS, _LIST_KEYS)


def _unknown_error(unknown: list[str]) -> str:
    """Name every token that was not applied, and what would have been."""
    return _filter_tokens.unknown_error(unknown, _FILTER_KEYS, _FLAGS)


def _bad_values(filters: dict[str, str]) -> list[tuple[str, str, str]]:
    """Known keys carrying a value this op has no mapping for."""
    return _filter_tokens.bad_values(filters, _VALUE_DOMAINS)


def _build_list_cmd(filters: dict[str, str], per_page: int) -> list[str]:
    """Build the `gh issue list ... --json` argv from parsed filters.

    No default role filter: see the module docstring. `state=open` is gh's
    default, so it emits no flag.
    """
    cmd = (["gh", "issue", "list", "--json", _LIST_FIELDS, "--limit", str(per_page)]
           + _repo_target.gh_args())
    for key, val in filters.items():
        if not val:
            continue
        if key == "state":
            if val in _STATES and val != "open":
                cmd += ["--state", val]
        elif key in {"author", "assignee", "label", "milestone"}:
            cmd += [f"--{key}", val]
    return cmd


# ---------------------------------------------------------------------------
# the three derived signals — each of them three-valued
# ---------------------------------------------------------------------------

def _external(assoc: object) -> bool | None:
    """Is the filer outside the repo? None when GitHub did not say.

    Returning False for a missing association would assert the reporter is one
    of us — the single wrong claim that drops an external report to the bottom
    of the queue and takes the data-not-instructions boundary with it.
    """
    value = str(assoc or "").strip().upper()
    if not value:
        return None
    return value not in _INSIDE


def _is_stale(newest_comment: object, created_at: object,
              last_edited_at: object) -> bool | None:
    """Has discussion overtaken the body? None when the comparison can't be made.

    Zero comments settles it as False without asking GitHub anything: nothing
    was said after the body, whatever `lastEditedAt` turns out to be. That is
    most of this repo's queue, so declining there would decline a question
    already answered.

    Otherwise the body-write time is `lastEditedAt` when the body was edited
    and `createdAt` when it was not — both exact. If neither is known the
    answer is unknown; comparing against nothing and reporting False would
    mark every discussed issue as fresh.
    """
    if not newest_comment:
        return False
    body_written = last_edited_at or created_at
    if not body_written:
        return None
    return str(newest_comment) > str(body_written)


def _milestone_of(row: dict) -> str | None:
    """The row's milestone title, `''` for none, `None` for nobody could say.

    Three states, on a list field rather than an enriched one, so it is never
    unknown by choice. `gh issue list --json milestone` returns `null` for an
    unmilestoned issue and the key is always present — so an absent key means
    the field did not come back, and a dict with no usable title means gh
    answered with something this op cannot read. Both of those are unknown;
    only an explicit null is "this issue has no milestone".
    """
    if "milestone" not in row:
        return None
    value = row["milestone"]
    if value is None:
        return ""
    if isinstance(value, dict):
        title = str(value.get("title") or "").strip()
        return title or None
    title = str(value).strip()
    return title or None


def _is_unknown(row: dict) -> bool:
    """True when any rank input is missing, so the row cannot be placed."""
    return any(row.get(key) is None for key in ("_external", "_stale", "_linked"))


# ---------------------------------------------------------------------------
# enrichment — one GraphQL call per chunk, for what `gh issue list` omits
# ---------------------------------------------------------------------------

def _owner_repo(rows: list[dict]) -> tuple[tuple[str, str] | None, str | None]:
    """The repo this board is about, for the GraphQL root — and why, when there is none.

    A repo target wins outright. Otherwise the answer is already in the rows:
    every issue carries its own `url`, so the owner/name costs no extra call
    and cannot disagree with the list the board is rendering. What decides
    which repo every subsequent GraphQL call is made against is therefore row
    content rather than configuration, which is why the host test is
    `_repo_target.is_github_host` and not a suffix match (#1180): the moment
    rows arrive from a fixture, a cached board or a merged tier, a lookalike
    host picks the target.

    A row whose url is not on GitHub is skipped rather than ending the search:
    one unusable row must not decide that the whole board has no repo.

    **Three states, not two** (#907). A bare `None` was the answer to four
    different situations — an empty listing, rows carrying no url at all, rows
    whose urls are on some other host, and a github.com url too short to hold
    an owner and a name — and the caller renders it into a decline the reader
    is meant to act on. The reason counts rows; it never quotes a url, because
    a url is tracker content and an error line is ours.
    """
    target = _repo_target.owner_repo()
    if target is not None:
        return target, None
    urls = 0
    on_host = 0
    for row in rows:
        url = str(row.get("url") or "")
        if not url:
            continue
        urls += 1
        if not _repo_target.is_github_host(_repo_target.url_host(url)):
            continue
        on_host += 1
        pair = _repo_target.github_owner_repo(url)
        if pair is not None:
            return pair, None
    if not rows:
        return None, "the listing had no rows"
    if not urls:
        return None, f"no row carried a url (checked {len(rows)})"
    if not on_host:
        return None, (
            f"no row url is on {_repo_target.GITHUB_HOST} (checked {urls})"
        )
    return None, (
        f"no row url on {_repo_target.GITHUB_HOST} carried an owner/name path "
        f"(checked {on_host})"
    )


# The three derived signals, as GraphQL. Named once because the `iids=` lookup
# (#1323) asks for them in the same query as the list fields — two spellings of
# one enrichment is how the board and the lookup start disagreeing about what
# `_external` means.
_ENRICH_FIELDS = (
    "lastEditedAt authorAssociation "
    # Ranks on *will this close the issue*, not on *has anyone referenced
    # it*. includeClosedPrs is load-bearing: without it a merged closer
    # vanishes and a shipped fix renders as unclaimed (#782).
    "closedByPullRequestsReferences(first: 5, includeClosedPrs: true) "
    "{ nodes { number state } } "
    "timelineItems(last: 20, itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) "
    "{ nodes { __typename "
    "... on CrossReferencedEvent { source { __typename ... on PullRequest { number state } } } "
    "... on ConnectedEvent { subject { __typename ... on PullRequest { number state } } } "
    "} }"
)


def _graphql_query(owner: str, name: str, numbers: list[int]) -> str:
    """Aliased single-issue lookups — one call for a whole chunk of the board."""
    fields = "number " + _ENRICH_FIELDS
    parts = " ".join(f"i{n}: issue(number: {n}) {{ {fields} }}" for n in numbers)
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{ {parts} }} }}'


def _fetch_enrichment(owner: str, name: str, numbers: list[int],
                      chunk: int = CHUNK) -> tuple[dict[int, dict], str | None]:
    """Per-issue association / body-edit / timeline data, keyed by number.

    Returns only what came back. A chunk that fails contributes nothing rather
    than a default, and its reason is returned so the footer can name it — an
    absence with a stated cause is actionable, an absence rendered as `0` is
    not.
    """
    enriched: dict[int, dict] = {}
    reason: str | None = None
    for start in range(0, len(numbers), chunk):
        batch = numbers[start:start + chunk]
        query = _graphql_query(owner, name, batch)
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            reason = reason or f"gh api graphql failed: {exc}"
            continue
        if result.returncode != 0:
            reason = reason or f"gh api graphql failed: {(result.stderr or '').strip()[:120]}"
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            reason = reason or "gh api graphql returned unparseable JSON"
            continue
        repo = ((payload.get("data") or {}).get("repository")) or {}
        for number in batch:
            node = repo.get(f"i{number}")
            if isinstance(node, dict):
                enriched[number] = node
        if not repo:
            reason = reason or "gh api graphql returned no repository data"
    missing = [n for n in numbers if n not in enriched]
    if missing and reason is None:
        reason = f"{len(missing)} issue(s) absent from the GraphQL response"
    return enriched, reason


# ---------------------------------------------------------------------------
# iids= — the population the caller named, rather than a listing (#1323)
# ---------------------------------------------------------------------------

def _parse_iids(spec: str) -> tuple[list[int], int]:
    """`"1240,1233,1240"` -> `([1240, 1233], 1)`.

    Order is the caller's, because an audit is read against the list it was
    written from. Duplicates collapse and are counted rather than dropped in
    silence: a board of two rows under a request for three numbers is the
    shorter-list reading this whole filter exists to refuse, and the caller
    cannot tell a collapse from a number that vanished.
    """
    numbers: list[int] = []
    seen: set[int] = set()
    dupes = 0
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        value = int(part)
        if value in seen:
            dupes += 1
            continue
        seen.add(value)
        numbers.append(value)
    return numbers, dupes


def _iids_composition_error(listing: list[str]) -> str:
    """`iids=` names a population; a listing filter has nothing to narrow."""
    return (
        "ERROR: " + ", ".join(f"{k}=" for k in listing)
        + " cannot be combined with iids= — iids names an exact population by "
          "number, and gh has no listing to apply those filters to, so they "
          "would have been dropped and the rows printed as though they had "
          "been applied. Ask for the numbers alone (gh-issues:iids=1,2,3), or "
          "drop iids= and filter the board."
    )


def _lookup_repo() -> tuple[str, str] | None:
    """The repo a number lookup is about, when there are no rows to read it off.

    `_owner_repo` derives the target from the board's own rows, which is the
    right answer when a listing produced them. `iids=` has no listing, so the
    repo has to be established before the first call rather than after it —
    a repo target when one was given, otherwise gh's own answer for the cwd.
    """
    pair = _repo_target.owner_repo()
    if pair is not None:
        return pair
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "owner,name"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    owner = str(((payload.get("owner") or {}) if isinstance(payload, dict) else {})
                .get("login") or "").strip()
    name = str((payload.get("name") if isinstance(payload, dict) else "") or "").strip()
    if not owner or not name:
        return None
    return owner, name


# The list-shaped fields `gh issue list --json` would have returned, asked for
# per number instead. Same names, so the row this builds goes through
# `_annotate`, `_apply_enrichment` and `_row` unchanged.
_LOOKUP_CORE = (
    "number title state createdAt updatedAt url "
    "author { login } milestone { title } "
    "labels(first: 20) { nodes { name } } "
    "assignees(first: 10) { nodes { login } } "
    "comments(last: 100) { totalCount nodes { createdAt } }"
)


def _lookup_query(owner: str, name: str, numbers: list[int], enrich: bool) -> str:
    """One call for a chunk of numbers, asking both questions per number.

    `issue(number: N)` returns null for a number that is a PR and for a number
    that does not exist at all, so on its own it cannot tell "you cited the
    wrong kind of thing" from "you cited nothing". `pullRequest(number: N)`
    beside it separates them, at no extra round-trip.
    """
    fields = _LOOKUP_CORE + ((" " + _ENRICH_FIELDS) if enrich else "")
    parts = " ".join(
        f"i{n}: issue(number: {n}) {{ {fields} }} "
        f"p{n}: pullRequest(number: {n}) {{ number title state }}"
        for n in numbers
    )
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{ {parts} }} }}'


def _graphql_payload(result: object) -> tuple[dict, str | None]:
    """`(repository, reason)` from a `gh api graphql` result.

    **A NOT_FOUND alias makes `gh` exit 1 while returning every alias that did
    resolve.** Measured against this repo on 2026-08-11: one missing number in
    a three-alias query exits 1, prints the full `data` block, and lists the
    misses under `errors`. Reading the exit code alone therefore discards a
    whole chunk of good rows because one citation was wrong — which is the
    exact input this filter exists to serve. So the body is parsed first and
    the exit code only decides what to say when there is no body.

    NOT_FOUND is expected here and is an *answer*; anything else — rate limit,
    auth, a field GitHub renamed — is a failed read and is named.
    """
    stdout = getattr(result, "stdout", "") or ""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        err = (getattr(result, "stderr", "") or "").strip()[:160]
        return {}, err or "gh api graphql returned unparseable JSON"
    others = [
        str(e.get("message") or e.get("type") or "")
        for e in (payload.get("errors") or [])
        if isinstance(e, dict) and str(e.get("type") or "") != "NOT_FOUND"
    ]
    repo = ((payload.get("data") or {}).get("repository")) or {}
    reason = "; ".join(m for m in others if m)[:200] or None
    if not repo and reason is None and getattr(result, "returncode", 0) != 0:
        reason = (getattr(result, "stderr", "") or "").strip()[:160] or "gh api graphql failed"
    return repo, reason


def _fetch_lookup(owner: str, name: str, numbers: list[int], chunk: int,
                  enrich: bool) -> tuple[dict[int, tuple[str, object]], str | None]:
    """Per number: `("issue"|"pr"|"absent"|"failed", payload)`.

    Four kinds, not two, and the fourth is the one this repo keeps getting
    wrong. A chunk whose call failed is `failed`, never `absent`: rendering a
    number the tool could not look up as a number that does not exist is an
    absence produced by the tool read as an absence in the world, and on a
    citation audit it invites deleting a reference that was correct.
    """
    results: dict[int, tuple[str, object]] = {}
    reason: str | None = None
    for start in range(0, len(numbers), chunk):
        batch = numbers[start:start + chunk]
        query = _lookup_query(owner, name, batch, enrich)
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace",
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            reason = reason or f"gh api graphql failed: {exc}"
            for number in batch:
                results[number] = ("failed", None)
            continue
        repo, chunk_reason = _graphql_payload(result)
        reason = reason or chunk_reason
        if not repo:
            for number in batch:
                results[number] = ("failed", None)
            continue
        for number in batch:
            issue = repo.get(f"i{number}")
            if isinstance(issue, dict):
                results[number] = ("issue", issue)
                continue
            pr = repo.get(f"p{number}")
            if isinstance(pr, dict):
                results[number] = ("pr", pr)
                continue
            # The alias came back null and no error stopped the chunk: GitHub
            # answered, and the answer is that the number is neither.
            results[number] = ("absent", None)
    return results, reason


def _row_from_node(node: dict) -> dict:
    """A GraphQL issue node in the shape `gh issue list --json` returns.

    Converting here rather than teaching the renderers a second vocabulary is
    what keeps `iids=` a filter: every cell, rank tier and refusal below this
    point sees exactly the row it has always seen.
    """
    labels = [n for n in ((node.get("labels") or {}).get("nodes") or [])
              if isinstance(n, dict)]
    assignees = [n for n in ((node.get("assignees") or {}).get("nodes") or [])
                 if isinstance(n, dict)]
    comments = (node.get("comments") or {})
    row = {
        "number": node.get("number"),
        "title": node.get("title"),
        "state": node.get("state"),
        "url": node.get("url"),
        "createdAt": node.get("createdAt"),
        "updatedAt": node.get("updatedAt"),
        "author": node.get("author"),
        "labels": labels,
        "assignees": assignees,
        "milestone": node.get("milestone"),
        "comments": [c for c in (comments.get("nodes") or []) if isinstance(c, dict)],
    }
    total = comments.get("totalCount")
    if isinstance(total, int):
        row["_comments_total"] = total
    return row


def _apply_comment_totals(rows: list[dict]) -> None:
    """Prefer GitHub's own count over the length of the page we asked for.

    `comments(last: 100)` is a window. `len(nodes)` under it reports 100 for an
    issue with 300 comments — a number the tool produced, read as a fact about
    the issue. `totalCount` is the fact; the window still gives the newest
    timestamp, which is all `_is_stale` needs.
    """
    for row in rows:
        total = row.pop("_comments_total", None)
        if isinstance(total, int):
            row["_comments"] = total


def _unresolved_row(number: int, kind: str, payload: object,
                    reason: str | None) -> dict:
    """A row for a requested number that is not an issue in this repo.

    It is a row, not an omission. An audit reading a list shorter than the one
    it asked about reads it as "all of these check out" — which is how #1233's
    audit would have missed that 12 of its 124 numbers belonged to a different
    repo.
    """
    scope = _repo_target.not_found_scope()
    if kind == "pr":
        title = str((payload or {}).get("title") or "") if isinstance(payload, dict) else ""
        note = f"is a PR {scope}, not an issue"
        status = "✗ is a PR"
    elif kind == "failed":
        title = f"number {number} could not be looked up — {reason or 'the call failed'}"
        note = f"could not be looked up — {reason or 'the call failed'}"
        status = "? lookup failed"
    else:
        title = f"number {number} does not resolve to an issue {scope}"
        note = f"does not resolve to an issue {scope}"
        status = "✗ not an issue"
    return {
        "number": number,
        "title": title,
        "_unresolved": kind,
        "_unresolved_note": note,
        "_unresolved_status": status,
        "_comments": None,
        "_newest_comment": None,
        "_external": None,
        "_stale": None,
        "_linked": None,
    }


def _closing_prs(node: dict) -> list[dict] | None:
    """PRs that will close this issue — the answer to "is anyone on it".

    Not the timeline. A `Closes #N` line in a PR body produces a
    `CrossReferencedEvent` indistinguishable from a prose mention, so the
    timeline conflates "someone is fixing this" with "someone typed this
    number". Measured on this repo: #736 mentions #735 while closing #720, and
    #781 closes #778 — both render as `CrossReferencedEvent` (#782).

    `None` when the field is absent or null, because `_linked` is a rank tier:
    an unknown that renders as "no PR" does not merely misreport, it sorts the
    row to the top of the work queue and gets it worked twice.
    """
    if "closedByPullRequestsReferences" not in node:
        return None
    refs = node["closedByPullRequestsReferences"]
    if refs is None:
        return None
    nodes = refs.get("nodes") if isinstance(refs, dict) else None
    if nodes is None:
        return None
    out: list[dict] = []
    seen: set[object] = set()
    for pr in nodes:
        if not isinstance(pr, dict):
            continue
        number = pr.get("number")
        if number in seen:
            continue
        seen.add(number)
        out.append({"number": number, "state": pr.get("state")})
    return out


def _mentioning_prs(node: dict) -> list[dict]:
    """PRs that reference this issue without closing it — context, not a claim."""
    out: list[dict] = []
    seen: set[object] = set()
    for item in ((node.get("timelineItems") or {}).get("nodes") or []):
        if not isinstance(item, dict):
            continue
        ref = item.get("source") or item.get("subject") or {}
        if not isinstance(ref, dict) or ref.get("__typename") != "PullRequest":
            continue
        number = ref.get("number")
        if number in seen:
            continue
        seen.add(number)
        out.append({"number": number, "state": ref.get("state")})
    return out


def _annotate(rows: list[dict]) -> None:
    """Derive from the list data alone — no extra call, so never unknown by choice.

    Comment count and the newest comment timestamp ship in `gh issue list
    --json comments`. An absent `comments` key is the one case where the count
    is unknown, and it stays `None` rather than becoming a confident 0.
    """
    for row in rows:
        comments = row.get("comments")
        if isinstance(comments, list):
            row["_comments"] = len(comments)
            row["_newest_comment"] = max(
                (str(c.get("createdAt") or "") for c in comments), default="",
            ) or None
        else:
            row["_comments"] = None
            row["_newest_comment"] = None
        row["_external"] = None
        row["_linked"] = None
        row["_stale"] = _is_stale(
            row["_newest_comment"], row.get("createdAt"), None,
        ) if row["_comments"] == 0 else None


def _apply_enrichment(rows: list[dict], data: dict) -> None:
    """Fill the derived fields for the rows the fetch actually covered.

    A row absent from `data` is left exactly as `_annotate` left it. Applying
    the shape of a successful response to a row nobody asked about is how an
    unenriched issue acquires a confident `False`.
    """
    for row in rows:
        node = data.get(row.get("number"))
        if not isinstance(node, dict):
            continue
        row["_external"] = _external(node.get("authorAssociation"))
        row["_linked"] = _closing_prs(node)
        row["_mentions"] = _mentioning_prs(node)
        row["_stale"] = _is_stale(
            row.get("_newest_comment"), row.get("createdAt"), node.get("lastEditedAt"),
        )


# ---------------------------------------------------------------------------
# cells
# ---------------------------------------------------------------------------

def _linked_cell(linked: list[dict] | None,
                 mentions: list[dict] | None = None) -> str:
    """Linked-PR cell. `?` and `no PR` are different answers and look different.

    A *mention* is shown only when there is no closer, and never as a link:
    a PR that references the number without closing it means nobody is on this
    issue, and saying otherwise is what #782 fixed. It is still worth seeing —
    it is usually where the adjacent work happened — so it renders as `~`.

    Neither reference is spelled `#N` (#842). `#` is this board's sigil for
    "the row's subject" — `_row()` spells it once, on `ident`, the issue's own
    number — and a foreign PR number in that same shape, sitting earlier on
    the line, reads as the row's id to anyone taking the first `#N` they see.
    `PR N` says the same thing without borrowing the sigil.
    """
    if linked is None:
        return "? unknown"
    if not linked:
        if mentions:
            extra = f" +{len(mentions) - 1}" if len(mentions) > 1 else ""
            return f"~ PR {mentions[0].get('number')} mention{extra}"
        return "· no PR"
    first = linked[0]
    extra = f" +{len(linked) - 1}" if len(linked) > 1 else ""
    state = str(first.get("state") or "").lower()
    return f"✓ PR {first.get('number')} {state}{extra}".rstrip()


def _ext_cell(external: bool | None) -> str:
    """One character: `!` outside, blank inside, `?` nobody could say."""
    if external is True:
        return "!"
    if external is False:
        return " "
    return "?"


def _comments_cell(count: int | None) -> str:
    return "?c" if count is None else f"{count}c"


def _labels_cell(row: dict) -> str:
    labels = row.get("labels") or []
    names = [str((label or {}).get("name", "")) for label in labels if isinstance(label, dict)]
    return _untrusted.flat(",".join(n for n in names if n))


def _flags(row: dict) -> str:
    """`[stale]` when the body was overtaken, `[stale?]` when nobody could say.

    The milestone rides here rather than in a column of its own. A column costs
    its width on every row of every board, and most issues on most repos carry
    no milestone — so the honest split is: nothing at all when there is none,
    `[m:TITLE]` when there is, and `[m:?]` when gh did not answer. A blank cell
    would have made the third case indistinguishable from the second.
    """
    out = ""
    stale = row.get("_stale")
    if stale is True:
        out += " [stale]"
    elif stale is None:
        out += " [stale?]"
    # State, three-valued. A bare board is `state=open` and every row is open,
    # so this printed nothing for a long time and cost nothing — but `state=all`
    # and `iids=` (#1323) both render closed issues, and a closed issue that
    # looks exactly like an open one is the whole answer to "is this citation
    # still live" given wrongly. `[state:?]` when the field did not come back,
    # for the same reason `[m:?]` exists: no field is unknown by choice here.
    state = str(row.get("state") or "").strip().upper() if "state" in row else ""
    if not state:
        out += " [state:?]"
    elif state != "OPEN":
        out += f" [{state.lower()}]"
    milestone = _milestone_of(row)
    if milestone is None:
        out += " [m:?]"
    elif milestone:
        out += f" [m:{_untrusted.flat(milestone)}]"
    return out


def _age(iso: str) -> str:
    """ISO timestamp → 'Nd'/'Nh'/'Nm'. '' on parse failure, 'now' on skew."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# ---------------------------------------------------------------------------
# rank
# ---------------------------------------------------------------------------

def _rank_key(row: dict) -> tuple[int, int, int, int, str]:
    """Triage order. 0 sorts first at every position.

    Unrankable first (its position would otherwise be invented), then external
    author, then stale body, then no linked PR, then oldest by `createdAt`.
    Age is the last tiebreak rather than the first because "oldest" is the
    ordering a plain list already gives and it is the one that keeps putting
    destructive reports behind cosmetic ones.
    """
    return (
        0 if _is_unknown(row) else 1,
        0 if row.get("_external") is True else 1,
        0 if row.get("_stale") is True else 1,
        1 if row.get("_linked") else 0,
        str(row.get("createdAt") or ""),
    )


def _sorted(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=_rank_key)


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def _row(row: dict) -> str:
    """One triage row through the shared board layout (`presets/_board.py`).

    `watched=False` rather than `None`: there is no `github-issue` watch source
    (#525 proposes one), so no issue can have a live poller. That is a known
    absence, not an unmeasured one, and `?` is reserved for the latter.
    """
    unresolved = row.get("_unresolved")
    if unresolved:
        # Not an issue row with blanks in it. Every cell whose value would be a
        # claim about an issue is `?`, and the title line says which of the
        # three non-answers this number is.
        return _board.render_row(
            sigil="#",
            ident=str(row.get("number", "?")),
            watched=False,
            status=str(row.get("_unresolved_status") or "? unknown"),
            appr="?",
            age="",
            changes="?c",
            branches="",
            flags=" [PR]" if unresolved == "pr" else "",
            title=_untrusted.flat(str(row.get("title", ""))),
        )
    return _board.render_row(
        sigil="#",
        ident=str(row.get("number", "?")),
        watched=False,
        status=_linked_cell(row.get("_linked"), row.get("_mentions")),
        appr=_ext_cell(row.get("_external")),
        age=_age(str(row.get("createdAt", ""))),
        changes=_comments_cell(row.get("_comments")),
        branches=_labels_cell(row),
        flags=_flags(row),
        title=_untrusted.flat(str(row.get("title", ""))),
    )


def _render_table(rows: list[dict]) -> str:
    if not rows:
        return "No issues match."
    return "\n".join(_row(r) for r in _sorted(rows))


def _cap_note(per_page: int | None, fetched: int | None) -> str | None:
    """The page boundary, as one sentence, for every shape that can print it.

    Extracted from `_footer` because the footer is not the only render: `iids`
    returns before one is built, and it is the shape whose output becomes
    another tool's input (#1067).
    """
    if per_page is None or fetched is None or fetched < per_page:
        return None
    return f"capped at --limit {per_page} — more may exist, raise with per=N"


def _footer(rows: list[dict], reason: str | None, per_page: int | None = None,
            fetched: int | None = None, notes: list[str] | None = None) -> str:
    """Counts when they are earned; a named absence when they are not.

    Counting `_external is True` across rows that were never enriched yields
    `0 external`, a sentence that reads as "nobody outside has filed anything".
    So unknown rows suppress the counts they would falsify and say how many
    they are and why.

    A board that came back exactly `--limit` rows long says so. `50 issue(s)`
    under a limit of 50 reads as "the queue is 50 long" when it means "the
    first 50 of an unknown number" — and on a *ranked* board the rows that
    fell off the end were not the least important, they were the ones gh's
    own default ordering happened to put last. Same defect class as the rest
    of this file: a bound produced by the tool, read as a fact about the
    world.
    """
    # A requested number that is not an issue here is unrankable for a reason
    # the caller can act on, and it is NOT a row whose enrichment failed. Two
    # clauses, because "12 of your citations point at nothing" and "12 rows
    # could not be enriched" send the reader to different places (#1323).
    unresolved = [r for r in rows if r.get("_unresolved")]
    rankable = [r for r in rows if not r.get("_unresolved")]
    unknown = [r for r in rankable if _is_unknown(r)]
    known = [r for r in rankable if not _is_unknown(r)]
    parts = [f"{len(rows)} issue(s)"]
    # The cap is a fact about the *fetch*, so it is measured against what came
    # back, not against what survived. Measured post-filter, three rows dropped
    # by `external`/`stale`/`nomilestone` take the count under the limit and
    # the "more may exist" line disappears — from exactly the queries that are
    # asking for completeness (#864).
    against = len(rows) if fetched is None else fetched
    cap = _cap_note(per_page, against)
    if cap:
        parts.append(cap)
    # Client-side narrowing, named with its count. `gh-issues:external` over an
    # all-internal board prints `No issues match.` and `0 issue(s)` — true
    # about the filter and read as a statement about the queue (#1071's shape,
    # in the sibling op).
    parts.extend(notes or [])
    if unresolved:
        prs = sum(1 for r in unresolved if r.get("_unresolved") == "pr")
        failed = sum(1 for r in unresolved if r.get("_unresolved") == "failed")
        clause = f"{len(unresolved)} requested number(s) are not issues here"
        detail = []
        if prs:
            detail.append(f"{prs} PR(s)")
        if failed:
            detail.append(f"{failed} could not be looked up at all")
        if detail:
            clause += " (" + ", ".join(detail) + ")"
        parts.append(clause)
    if unknown:
        parts.append(
            f"{len(unknown)} row(s) unknown — enrichment "
            f"{reason or 'incomplete'}; ranking degraded to oldest-first"
        )
    if known:
        external = sum(1 for r in known if r.get("_external"))
        stale = sum(1 for r in known if r.get("_stale"))
        unlinked = sum(1 for r in known if not r.get("_linked"))
        if external:
            parts.append(f"{external} external")
        if stale:
            parts.append(f"{stale} stale")
        if unlinked:
            parts.append(f"{unlinked} unlinked")
    return " | ".join(parts)


def _decline(flag: str, field: str, reason: str | None) -> str:
    """The message for a filter whose field nobody could establish.

    `gh-issues:external` over unenriched rows could print `No issues match.`,
    and that sentence is a claim there are no external reports — the one thing
    a triage caller must not be told wrongly.
    """
    return (
        f"ERROR: cannot filter by {flag} — {field} is unknown for one or more "
        f"issues (enrichment {reason or 'incomplete'}). Re-run without "
        f"{flag}, or fix the enrichment call and retry."
    )


def _lookup_iids(
    spec: str, filters: dict[str, str], flags: set[str], per_given: bool,
    per_page: int, cfg: dict[str, int], numbers_only: bool,
) -> int | tuple[list[dict], list[dict], list[str], str | None, int]:
    """The `iids=` population: `(rows, unresolved, notes, reason, fetched)`.

    Returns an exit code instead when the request cannot be answered at all —
    a listing filter that has nothing to narrow, a repo that could not be
    established, or a call that failed with nothing to show for it. None of
    those may render as rows: an empty or short board under a request for N
    numbers is read as "these all check out".
    """
    listing = sorted(k for k in filters if k in _LISTING_KEYS)
    if listing:
        print(_iids_composition_error(listing), file=sys.stderr)
        return 1

    notes: list[str] = []
    numbers, dupes = _parse_iids(spec)
    if dupes:
        notes.append(f"iids: {dupes} duplicate number(s) collapsed")
    requested = len(numbers)
    # `per=` on a listing bounds an unknown population; here it bounds one the
    # caller enumerated, so it only applies when they asked for it — and when
    # it does, it names the numbers it did not look up rather than returning a
    # shorter board.
    if per_given and requested > per_page:
        notes.append(
            f"iids capped at per={per_page} — {requested - per_page} of "
            f"{requested} requested number(s) not looked up")
        numbers = numbers[:per_page]

    pair = _lookup_repo()
    if pair is None:
        print(_repo_target.no_repo_error("gh-issues:iids=1,2,3"), file=sys.stderr)
        return 1

    # Under the numbers-only render nothing derived is printed, so the
    # enrichment half of the query is not paid for.
    enrich = "nopipe" not in flags and not numbers_only
    results, reason = _fetch_lookup(pair[0], pair[1], numbers, cfg["chunk"], enrich)

    rows: list[dict] = []
    unresolved: list[dict] = []
    data: dict[int, dict] = {}
    for number in numbers:
        kind, payload = results.get(number, ("failed", None))
        if kind == "issue" and isinstance(payload, dict):
            rows.append(_row_from_node(payload))
            data[number] = payload
        else:
            unresolved.append(_unresolved_row(number, kind, payload, reason))

    if numbers and not rows and reason is not None:
        # Nothing resolved AND the call reported a fault. Rendering N rows of
        # "does not resolve" there would report a failed read as a tracker full
        # of dead citations — the absence this repo keeps mistaking for a fact.
        print(f"ERROR: gh api graphql: {reason}", file=sys.stderr)
        return 1

    _annotate(rows)
    _apply_comment_totals(rows)
    if enrich:
        _apply_enrichment(rows, data)
    else:
        reason = reason or REASON_NOPIPE
    return rows, unresolved, notes, reason, len(rows) + len(unresolved)


def main_with_args(arg_str: str) -> int:
    filters, flags, unknown_tokens = _parse_args(arg_str)
    if unknown_tokens:
        print(_unknown_error(unknown_tokens), file=sys.stderr)
        return 1
    bad = _bad_values(filters)
    if bad:
        print(_filter_tokens.value_error(bad), file=sys.stderr)
        return 1
    cfg = _get_config()
    per_page = cfg["per_page"]
    per_given = "per" in filters
    if per_given:
        per_page = int(filters.pop("per"))
    iids_spec = filters.pop("iids", None)
    numbers_only = "iids" in flags

    rows: list[dict]
    unresolved: list[dict] = []
    lookup_notes: list[str] = []
    reason: str | None = None

    if iids_spec is not None:
        rc = _lookup_iids(iids_spec, filters, flags, per_given, per_page,
                          cfg, numbers_only)
        if isinstance(rc, int):
            return rc
        rows, unresolved, lookup_notes, reason, fetched = rc
        # A named population is finite and complete by construction, so the
        # `--limit N — more may exist` sentence would be false here. `per=`
        # still caps, and says so in `lookup_notes` when it does.
        per_page = None
    else:
        try:
            result = subprocess.run(
                _build_list_cmd(filters, per_page),
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            print(f"ERROR: gh issue list failed: {exc}", file=sys.stderr)
            return 1
        if result.returncode != 0:
            err = (result.stderr or "").strip() or "unknown error"
            low = err.lower()
            if "not logged in" in low or "401" in err:
                print("ERROR: gh not authenticated. Run: gh auth login", file=sys.stderr)
            elif ("github host" in low or "not a git repository" in low
                    or "git remotes" in low):
                print(_repo_target.no_repo_error("gh-issues:label=bug"), file=sys.stderr)
            else:
                print(f"ERROR: gh issue list: {err}", file=sys.stderr)
            return 1

        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError:
            print("ERROR: could not parse gh JSON output", file=sys.stderr)
            return 1
        if not isinstance(rows, list):
            rows = []

        _annotate(rows)
        # What the fetch returned, kept before any client-side filter narrows it —
        # the --limit disclosure is a statement about this number.
        fetched = len(rows)

    # Bare number list — on the listing path no enrichment is paid for at all.
    #
    # The disclosures ride along as `#` comments rather than being dropped: a
    # truncated list stops being the same bytes as a complete one. This early
    # return is why `iids` was the one shape that said nothing about the page
    # boundary (#1067) — and under `iids=` a requested number that resolved to
    # nothing is named here too, because the consumer of this shape is another
    # tool and a shorter list is indistinguishable from a clean one.
    #
    # Not stderr — `_run_custom_op` returns a successful op's stdout and drops
    # its stderr, so a note there is a note nobody receives (#654).
    if numbers_only:
        for note in lookup_notes:
            print(f"# {note}")
        cap = _cap_note(per_page, fetched)
        if cap:
            print(f"# {cap}")
        for row in unresolved:
            print(f"# {row['number']} {row['_unresolved_note']}")
        for row in rows:
            number = row.get("number")
            if number is not None:
                print(number)
        return 0

    # **The listing route's enrichment only** — `iids=` has already done its own,
    # in the same GraphQL call that fetched the rows, and `_lookup_iids` has
    # already set `reason` to None, REASON_NOPIPE or the fault it hit. Running
    # this block over those rows would be a second round-trip for data already
    # in hand, and `_owner_repo` reads the repo off row urls, which the
    # unresolved rows do not carry. The decline it can produce — `repo could not
    # be identified from the listing` — is also a sentence that cannot be true
    # where there was no listing (#1323).
    #
    # `reason` is NOT re-declared here. It is initialised once above, because
    # the two routes now both set it and a fresh `= None` at this point would
    # silently discard whatever the lookup route established.
    if iids_spec is None:
        if "nopipe" in flags:
            reason = REASON_NOPIPE
        elif rows:
            # Only under `rows`: an empty listing has nothing to enrich, so its
            # absence of a repo is not a degradation and must not print as one.
            pair, why = _owner_repo(rows)
            if pair is None:
                reason = f"repo could not be identified from the listing — {why}"
            else:
                numbers = [r["number"] for r in rows if isinstance(r.get("number"), int)]
                data, reason = _fetch_enrichment(pair[0], pair[1], numbers, cfg["chunk"])
                _apply_enrichment(rows, data)

    rows = rows + unresolved
    notes: list[str] = list(lookup_notes)

    def _narrow(flag: str, before: list[dict], keep: list[dict]) -> list[dict]:
        """Apply a client-side filter and record what it removed.

        `before` is passed rather than read from the enclosing scope, and the
        denominator is `fetched` rather than either list. Every call site
        rebinds `rows`, so the closure version reported the second flag of
        `external,stale` against the first flag's survivor count — a number no
        fetch ever returned. Same invariant `_cap_note` states for the page
        boundary: measured against the fetch, not against what survived a
        filter (#864). Each note's numerator stays what *that* flag removed,
        so the notes sum to the rows lost rather than double-counting them.
        """
        dropped = len(before) - len(keep)
        if dropped:
            notes.append(f"{flag} excluded {dropped} of {fetched} fetched")
        return keep

    if "external" in flags:
        if any(r.get("_external") is None for r in rows):
            print(_decline("external", "author association", reason), file=sys.stderr)
            return 1
        rows = _narrow("external", rows, [r for r in rows if r.get("_external")])
    if "stale" in flags:
        if any(r.get("_stale") is None for r in rows):
            print(_decline("stale", "body-edit time", reason), file=sys.stderr)
            return 1
        rows = _narrow("stale", rows, [r for r in rows if r.get("_stale")])
    if "nomilestone" in flags:
        # `gh issue list` can name a milestone; it cannot ask for the absence
        # of one, so this filter is client-side. Which means a row whose
        # milestone did not come back cannot be placed: filtering it in reports
        # a scheduled issue as unscheduled, filtering it out drops the exact
        # kind of gap the query exists to find. Neither is reportable, so the
        # op declines.
        if any(_milestone_of(r) is None for r in rows):
            print(_decline("nomilestone", "milestone", reason), file=sys.stderr)
            return 1
        rows = _narrow("nomilestone", rows,
                       [r for r in rows if not _milestone_of(r)])

    # `flat_note` rather than `banner()` (#819). This render fences nothing —
    # titles and labels are one-line fields and are flattened — so the banner
    # was announcing `⟨remote NONCE⟩` markers no reader would ever find. A
    # disclosure naming a mechanism it does not use teaches the reader to skim
    # the next one.
    if rows:
        # Header as well as footer. A footer is lost by exactly the consumer
        # that truncates (#633, #635, #657), and the cap note fires precisely
        # when the board is at its longest — the case it exists for is the
        # case the footer does not survive. Nothing prints when nothing was
        # cut, so the silence stays a positive claim that the board is whole.
        #
        # Only the cap: the client-side flag notes describe rows the caller
        # asked to lose, which is the same line `iids` draws.
        cap = _cap_note(per_page, fetched)
        if cap:
            print(f"({cap})")
        print(_untrusted.flat_note("issue titles and labels"))
    print(_render_table(rows))
    footer = _footer(rows, reason, per_page, fetched, notes)
    if footer:
        print(f"\n{footer}")
    return 0


def main() -> int:
    extra = _filter_tokens.extra_segments_error(sys.argv, "gh-issues")
    if extra:
        print(extra, file=sys.stderr)
        return 1
    return main_with_args(sys.argv[1] if len(sys.argv) > 1 else "")


if __name__ == "__main__":
    sys.exit(main())
