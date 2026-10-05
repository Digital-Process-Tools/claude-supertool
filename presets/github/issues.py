#!/usr/bin/env python3













































































from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _board  
import _filter_tokens  
import _repo_target  
import _untrusted  
import _auth_probe  
from _env import env_int  

DEFAULT_PER_PAGE = 50



CHUNK = 20

REASON_NOPIPE = "skipped by nopipe"


_FLAGS = {"nopipe", "iids", "external", "stale", "nomilestone"}














_ENRICHED_FLAGS = {"external", "stale"}







_FILTER_KEYS = {"author", "assignee", "label", "milestone", "state", "per",
                "iids", "search"}














SEARCH_ENGINE = "GitHub issue search"
SEARCH_SCOPE = "title, body and comments"




_LIST_KEYS = {"iids"}





_LISTING_KEYS = {"author", "assignee", "label", "milestone", "state", "search"}

_STATES = {"open", "closed", "all"}





_VALUE_DOMAINS: dict[str, object] = {
    "state": _STATES,
    "per": _filter_tokens.POSITIVE_INT,
    "iids": _filter_tokens.POSITIVE_INT_LIST,
}





_INSIDE = {"OWNER", "MEMBER", "COLLABORATOR"}

_LIST_FIELDS = (
    "number,title,state,author,labels,assignees,milestone,"
    "createdAt,updatedAt,comments,url"
)


def _get_config() -> dict[str, int]:

    return {
        "per_page": env_int(os.environ.get("SUPERTOOL_PER_PAGE"), "SUPERTOOL_PER_PAGE", DEFAULT_PER_PAGE, minimum=1),
        "chunk": env_int(os.environ.get("SUPERTOOL_ISSUE_CHUNK"), "SUPERTOOL_ISSUE_CHUNK", CHUNK, minimum=1),
    }


def _parse_args(arg_str: str) -> tuple[dict[str, str], set[str], list[str]]:












    return _filter_tokens.parse(arg_str, _FILTER_KEYS, _FLAGS, _LIST_KEYS)


def _unknown_error(unknown: list[str]) -> str:

    return _filter_tokens.unknown_error(unknown, _FILTER_KEYS, _FLAGS)


def _bad_values(filters: dict[str, str]) -> list[tuple[str, str, str]]:

    return _filter_tokens.bad_values(filters, _VALUE_DOMAINS)


def _build_list_cmd(filters: dict[str, str], per_page: int) -> list[str]:





    cmd = (["gh", "issue", "list", "--json", _LIST_FIELDS, "--limit", str(per_page)]
           + _repo_target.gh_args())
    for key, val in filters.items():
        if not val:
            continue
        if key == "state":
            if val in _STATES and val != "open":
                cmd += ["--state", val]
        elif key in {"author", "assignee", "label", "milestone", "search"}:





            cmd += [f"--{key}", val]
    return cmd


def _search_note(query: str) -> str:

    return f"search {query!r} — {SEARCH_ENGINE} over {SEARCH_SCOPE}"






def _external(assoc: object) -> bool | None:






    value = str(assoc or "").strip().upper()
    if not value:
        return None
    return value not in _INSIDE


def _is_stale(newest_comment: object, created_at: object,
              last_edited_at: object) -> bool | None:












    if not newest_comment:
        return False
    body_written = last_edited_at or created_at
    if not body_written:
        return None
    return str(newest_comment) > str(body_written)


def _milestone_of(row: dict) -> str | None:









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

    return any(row.get(key) is None for key in ("_external", "_stale", "_linked"))






def _owner_repo(rows: list[dict]) -> tuple[tuple[str, str] | None, str | None]:





















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






_ENRICH_FIELDS = (
    "lastEditedAt authorAssociation "



    "closedByPullRequestsReferences(first: 5, includeClosedPrs: true) "
    "{ nodes { number state } } "
    "timelineItems(last: 20, itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) "
    "{ nodes { __typename "
    "... on CrossReferencedEvent { source { __typename ... on PullRequest { number state } } } "
    "... on ConnectedEvent { subject { __typename ... on PullRequest { number state } } } "
    "} }"
)


def _graphql_query(owner: str, name: str, numbers: list[int]) -> str:

    fields = "number " + _ENRICH_FIELDS
    parts = " ".join(f"i{n}: issue(number: {n}) {{ {fields} }}" for n in numbers)
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{ {parts} }} }}'


def _fetch_enrichment(owner: str, name: str, numbers: list[int],
                      chunk: int = CHUNK) -> tuple[dict[int, dict], str | None]:







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






def _parse_iids(spec: str) -> tuple[list[int], int]:








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

    return (
        "ERROR: " + ", ".join(f"{k}=" for k in listing)
        + " cannot be combined with iids= — iids names an exact population by "
          "number, and gh has no listing to apply those filters to, so they "
          "would have been dropped and the rows printed as though they had "
          "been applied. Ask for the numbers alone (gh-issues:iids=1,2,3), or "
          "drop iids= and filter the board."
    )


def _lookup_repo() -> tuple[tuple[str, str] | None, str | None]:















    pair = _repo_target.owner_repo()
    if pair is not None:
        return pair, None
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "owner,name"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return None, f"ERROR: gh repo view failed: {exc}"
    if result.returncode != 0:

        err = _untrusted.flat((result.stderr or "").strip()) or "unknown error"
        low = err.lower()


        if _auth_probe.says_not_authenticated(err):
            return None, "ERROR: gh not authenticated. Run: gh auth login"
        if ("github host" in low or "not a git repository" in low
                or "git remotes" in low or "could not determine" in low):
            return None, _repo_target.no_repo_error("gh-issues:iids=1,2,3")
        return None, f"ERROR: gh repo view: {err}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "ERROR: could not parse gh repo view JSON output"
    owner = str(((payload.get("owner") or {}) if isinstance(payload, dict) else {})
                .get("login") or "").strip()
    name = str((payload.get("name") if isinstance(payload, dict) else "") or "").strip()
    if not owner or not name:
        return None, ("ERROR: gh repo view answered without an owner/name this "
                      "op can read")
    return (owner, name), None





_LOOKUP_CORE = (
    "number title state createdAt updatedAt url "
    "author { login } milestone { title } "
    "labels(first: 20) { nodes { name } } "
    "assignees(first: 10) { nodes { login } } "
    "comments(last: 100) { totalCount nodes { createdAt } }"
)


def _lookup_query(owner: str, name: str, numbers: list[int], enrich: bool) -> str:







    fields = _LOOKUP_CORE + ((" " + _ENRICH_FIELDS) if enrich else "")
    parts = " ".join(
        f"i{n}: issue(number: {n}) {{ {fields} }} "
        f"p{n}: pullRequest(number: {n}) {{ number title state }}"
        for n in numbers
    )
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{ {parts} }} }}'


def _graphql_payload(result: object) -> tuple[dict, str | None, set[str]]:



















    stdout = getattr(result, "stdout", "") or ""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        err = (getattr(result, "stderr", "") or "").strip()[:160]
        return {}, err or "gh api graphql returned unparseable JSON", set()
    others = []
    faulted: set[str] = set()
    for err_obj in (payload.get("errors") or []):
        if not isinstance(err_obj, dict):
            continue
        if str(err_obj.get("type") or "") == "NOT_FOUND":
            continue
        others.append(str(err_obj.get("message") or err_obj.get("type") or ""))
        for step in (err_obj.get("path") or []):
            if isinstance(step, str) and step != "repository":
                faulted.add(step)
    repo = ((payload.get("data") or {}).get("repository")) or {}
    reason = "; ".join(m for m in others if m)[:200] or None
    if not repo and reason is None and getattr(result, "returncode", 0) != 0:
        reason = (getattr(result, "stderr", "") or "").strip()[:160] or "gh api graphql failed"
    return repo, reason, faulted


def _fetch_lookup(owner: str, name: str, numbers: list[int], chunk: int,
                  enrich: bool) -> tuple[dict[int, tuple[str, object]], str | None]:








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
            chunk_reason = f"gh api graphql failed: {exc}"
            reason = reason or chunk_reason
            for number in batch:
                results[number] = ("failed", chunk_reason)
            continue
        repo, chunk_reason, faulted = _graphql_payload(result)
        reason = reason or chunk_reason
        if not repo:




            for number in batch:
                results[number] = ("failed", chunk_reason)
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
            if faulted & {f"i{number}", f"p{number}"}:


                results[number] = ("failed", chunk_reason)
                continue


            results[number] = ("absent", None)
    return results, reason


def _row_from_node(node: dict) -> dict:






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







    for row in rows:
        total = row.pop("_comments_total", None)
        if isinstance(total, int):
            row["_comments"] = total


def _unresolved_row(number: int, kind: str, payload: object,
                    reason: str | None) -> dict:







    scope = _repo_target.not_found_scope()
    if kind == "pr":
        title = str((payload or {}).get("title") or "") if isinstance(payload, dict) else ""
        note = f"is a PR {scope}, not an issue"
        status = "✗ is a PR"
    elif kind == "failed":
        cause = (payload if isinstance(payload, str) and payload
                 else reason) or "the call failed"
        title = f"number {number} could not be looked up — {cause}"
        note = f"could not be looked up — {cause}"
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






def _linked_cell(linked: list[dict] | None,
                 mentions: list[dict] | None = None) -> str:













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








    out = ""
    stale = row.get("_stale")
    if stale is True:
        out += " [stale]"
    elif stale is None:
        out += " [stale?]"






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






def _rank_key(row: dict) -> tuple[int, int, int, int, str]:








    return (
        0 if _is_unknown(row) else 1,
        0 if row.get("_external") is True else 1,
        0 if row.get("_stale") is True else 1,
        1 if row.get("_linked") else 0,
        str(row.get("createdAt") or ""),
    )


def _sorted(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=_rank_key)






def _row(row: dict) -> str:






    unresolved = row.get("_unresolved")
    if unresolved:



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


def _render_table(rows: list[dict], search: str | None = None) -> str:









    if not rows:
        if search is not None:
            return (f"No issues match {_search_note(search)}. "
                    "The search ran and matched nothing — an empty result, "
                    "not a lookup that failed.")
        return "No issues match."
    return "\n".join(_row(r) for r in _sorted(rows))


def _cap_note(per_page: int | None, fetched: int | None) -> str | None:






    if per_page is None or fetched is None or fetched < per_page:
        return None
    return f"capped at --limit {per_page} — more may exist, raise with per=N"


def _footer(rows: list[dict], reason: str | None, per_page: int | None = None,
            fetched: int | None = None, notes: list[str] | None = None) -> str:



















    unresolved = [r for r in rows if r.get("_unresolved")]
    rankable = [r for r in rows if not r.get("_unresolved")]
    unknown = [r for r in rankable if _is_unknown(r)]
    known = [r for r in rankable if not _is_unknown(r)]
    parts = [f"{len(rows)} issue(s)"]





    against = len(rows) if fetched is None else fetched
    cap = _cap_note(per_page, against)
    if cap:
        parts.append(cap)




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






    return (
        f"ERROR: cannot filter by {flag} — {field} is unknown for one or more "
        f"issues (enrichment {reason or 'incomplete'}). Re-run without "
        f"{flag}, or fix the enrichment call and retry."
    )


def _lookup_iids(
    spec: str, filters: dict[str, str], flags: set[str], per_given: bool,
    per_page: int, cfg: dict[str, int], numbers_only: bool,
) -> int | tuple[list[dict], list[dict], list[str], str | None, int]:








    listing = sorted(k for k in filters if k in _LISTING_KEYS)
    if listing:
        print(_iids_composition_error(listing), file=sys.stderr)
        return 1

    notes: list[str] = []
    numbers, dupes = _parse_iids(spec)
    if dupes:
        notes.append(f"iids: {dupes} duplicate number(s) collapsed")
    requested = len(numbers)




    if per_given and requested > per_page:
        notes.append(
            f"iids capped at per={per_page} — {requested - per_page} of "
            f"{requested} requested number(s) not looked up")
        numbers = numbers[:per_page]

    pair, repo_error = _lookup_repo()
    if pair is None:
        print(repo_error or _repo_target.no_repo_error("gh-issues:iids=1,2,3"),
              file=sys.stderr)
        return 1









    enrich = "nopipe" not in flags and (
        not numbers_only or bool(flags & _ENRICHED_FLAGS))
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


    search = filters.get("search")

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

            err = _untrusted.flat((result.stderr or "").strip()) or "unknown error"
            low = err.lower()


            if _auth_probe.says_not_authenticated(err):
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


        fetched = len(rows)


















    if iids_spec is None and (
            not numbers_only or (flags & _ENRICHED_FLAGS)):
        if "nopipe" in flags:
            reason = REASON_NOPIPE
        elif rows:


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






        if any(_milestone_of(r) is None for r in rows):
            print(_decline("nomilestone", "milestone", reason), file=sys.stderr)
            return 1
        rows = _narrow("nomilestone", rows,
                       [r for r in rows if not _milestone_of(r)])





















    if numbers_only:





        if search is not None:
            print(f"# {_search_note(search)}")
        for note in notes:
            print(f"# {note}")
        cap = _cap_note(per_page, fetched)
        if cap:
            print(f"# {cap}")



        for row in rows:
            if row.get("_unresolved_note"):
                print(f"# {row['number']} {row['_unresolved_note']}")
        for row in rows:
            if row.get("_unresolved_note"):
                continue
            number = row.get("number")
            if number is not None:
                print(number)
        return 0






    if rows:








        cap = _cap_note(per_page, fetched)
        if cap:
            print(f"({cap})")



        if search is not None:
            print(f"({_search_note(search)})")
        print(_untrusted.flat_note("issue titles and labels"))
    print(_render_table(rows, search))
    if search is not None:
        notes.insert(0, _search_note(search))
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
