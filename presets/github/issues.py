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
    repo:OWNER/NAME gh-issues       another repo's queue (#673)
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
_FILTER_KEYS = {"author", "assignee", "label", "milestone", "state", "per"}

_STATES = {"open", "closed", "all"}

# Keys whose value this op maps rather than forwards. A value with no mapping
# is dropped when the argv is built, so `state=opne` used to return the *open*
# board — the same silent-drop defect as an unknown key, on a key that is
# known (#939).
_VALUE_DOMAINS: dict[str, object] = {
    "state": _STATES,
    "per": _filter_tokens.POSITIVE_INT,
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
    return _filter_tokens.parse(arg_str, _FILTER_KEYS, _FLAGS)


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

# The host a row's `url` must be on before its path is read as owner/name.
_GITHUB_HOST = "github.com"


def _is_github_host(authority: str) -> bool:
    """Exact host, or a `.`-boundary subdomain of it — never a suffix (#1180).

    `endswith("github.com")` also matches `evilgithub.com` and `notgithub.com`.
    The rows come from `gh` today, so nothing hostile reaches here through the
    normal path; what this decides is which repo every subsequent GraphQL call
    is made against, and it decides it from row content rather than from
    configuration. The moment rows arrive from a fixture, a cached board or a
    merged tier, a lookalike host picks the target. Same shape as the `gl-api`
    host check.
    """
    host = authority.lower().partition("@")[2] or authority.lower()
    host = host.partition(":")[0]
    return host == _GITHUB_HOST or host.endswith("." + _GITHUB_HOST)


def _owner_repo(rows: list[dict]) -> tuple[str, str] | None:
    """The repo this board is about, for the GraphQL root.

    A repo target wins outright. Otherwise the answer is already in the rows:
    every issue carries its own `url`, so the owner/name costs no extra call
    and cannot disagree with the list the board is rendering.

    A row whose url is not on GitHub is skipped rather than ending the search:
    one unusable row must not decide that the whole board has no repo.
    """
    target = _repo_target.owner_repo()
    if target is not None:
        return target
    for row in rows:
        url = str(row.get("url") or "")
        parts = url.split("/")
        if len(parts) >= 5 and _is_github_host(parts[2]):
            return parts[3], parts[4]
    return None


def _graphql_query(owner: str, name: str, numbers: list[int]) -> str:
    """Aliased single-issue lookups — one call for a whole chunk of the board."""
    fields = (
        "number lastEditedAt authorAssociation "
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
    unknown = [r for r in rows if _is_unknown(r)]
    known = [r for r in rows if not _is_unknown(r)]
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
    if "per" in filters:
        per_page = int(filters.pop("per"))

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

    # Bare number list — no enrichment needed, so none is paid for.
    #
    # The cap rides along as a `#` comment rather than being dropped: a
    # truncated list stops being the same bytes as a complete one. This early
    # return is why `iids` was the one shape that said nothing about the page
    # boundary (#1067).
    #
    # Not stderr — `_run_custom_op` returns a successful op's stdout and drops
    # its stderr, so a note there is a note nobody receives (#654).
    if "iids" in flags:
        cap = _cap_note(per_page, fetched)
        if cap:
            print(f"# {cap}")
        for row in rows:
            number = row.get("number")
            if number is not None:
                print(number)
        return 0

    reason: str | None = None
    if "nopipe" in flags:
        reason = REASON_NOPIPE
    elif rows:
        pair = _owner_repo(rows)
        if pair is None:
            reason = "repo could not be identified from the listing"
        else:
            numbers = [r["number"] for r in rows if isinstance(r.get("number"), int)]
            data, reason = _fetch_enrichment(pair[0], pair[1], numbers, cfg["chunk"])
            _apply_enrichment(rows, data)

    notes: list[str] = []

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
