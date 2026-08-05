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
    gh-issues:iids                  bare number list
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
_FLAGS = {"nopipe", "iids", "external", "stale"}

_STATES = {"open", "closed", "all"}

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


def _parse_args(arg_str: str) -> tuple[dict[str, str], set[str]]:
    """Split a comma-separated arg string into (filters, flags).

    Same grammar as `gh-prs` — comma-separated so the single supertool arg
    segment never collides with the ':' op tokenizer.
    """
    filters: dict[str, str] = {}
    flags: set[str] = set()
    for tok in (t.strip() for t in arg_str.split(",")):
        if not tok:
            continue
        if "=" in tok:
            key, _, val = tok.partition("=")
            filters[key.strip()] = val.strip()
        elif tok in _FLAGS:
            flags.add(tok)
    return filters, flags


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


def _is_unknown(row: dict) -> bool:
    """True when any rank input is missing, so the row cannot be placed."""
    return any(row.get(key) is None for key in ("_external", "_stale", "_linked"))


# ---------------------------------------------------------------------------
# enrichment — one GraphQL call per chunk, for what `gh issue list` omits
# ---------------------------------------------------------------------------

def _owner_repo(rows: list[dict]) -> tuple[str, str] | None:
    """The repo this board is about, for the GraphQL root.

    A repo target wins outright. Otherwise the answer is already in the rows:
    every issue carries its own `url`, so the owner/name costs no extra call
    and cannot disagree with the list the board is rendering.
    """
    target = _repo_target.owner_repo()
    if target is not None:
        return target
    for row in rows:
        url = str(row.get("url") or "")
        parts = url.split("/")
        if len(parts) >= 5 and parts[2].endswith("github.com"):
            return parts[3], parts[4]
    return None


def _graphql_query(owner: str, name: str, numbers: list[int]) -> str:
    """Aliased single-issue lookups — one call for a whole chunk of the board."""
    fields = (
        "number lastEditedAt authorAssociation "
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


def _linked_prs(node: dict) -> list[dict]:
    """Pull requests connected to this issue, deduped, in timeline order."""
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
        row["_linked"] = _linked_prs(node)
        row["_stale"] = _is_stale(
            row.get("_newest_comment"), row.get("createdAt"), node.get("lastEditedAt"),
        )


# ---------------------------------------------------------------------------
# cells
# ---------------------------------------------------------------------------

def _linked_cell(linked: list[dict] | None) -> str:
    """Linked-PR cell. `?` and `no PR` are different answers and look different."""
    if linked is None:
        return "? unknown"
    if not linked:
        return "· no PR"
    first = linked[0]
    extra = f" +{len(linked) - 1}" if len(linked) > 1 else ""
    state = str(first.get("state") or "").lower()
    return f"✓ #{first.get('number')} {state}{extra}".rstrip()


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
    """`[stale]` when the body was overtaken, `[stale?]` when nobody could say."""
    stale = row.get("_stale")
    if stale is True:
        return " [stale]"
    if stale is None:
        return " [stale?]"
    return ""


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
        status=_linked_cell(row.get("_linked")),
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


def _footer(rows: list[dict], reason: str | None, per_page: int | None = None) -> str:
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
    if per_page is not None and len(rows) >= per_page:
        parts.append(f"capped at --limit {per_page} — more may exist, raise with per=N")
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
    filters, flags = _parse_args(arg_str)
    cfg = _get_config()
    per_page = cfg["per_page"]
    if "per" in filters and filters["per"].isdigit():
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

    # Bare number list — no enrichment needed, so none is paid for.
    if "iids" in flags:
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

    if "external" in flags:
        if any(r.get("_external") is None for r in rows):
            print(_decline("external", "author association", reason), file=sys.stderr)
            return 1
        rows = [r for r in rows if r.get("_external")]
    if "stale" in flags:
        if any(r.get("_stale") is None for r in rows):
            print(_decline("stale", "body-edit time", reason), file=sys.stderr)
            return 1
        rows = [r for r in rows if r.get("_stale")]

    print(_untrusted.banner())
    print(_render_table(rows))
    footer = _footer(rows, reason, per_page)
    if footer:
        print(f"\n{footer}")
    return 0


def main() -> int:
    return main_with_args(sys.argv[1] if len(sys.argv) > 1 else "")


if __name__ == "__main__":
    sys.exit(main())
