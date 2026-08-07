#!/usr/bin/env python3
"""`gl-api:PATH` — a GET-only read of any GitLab REST path (#831).

The `gl-*` family is specialised — MR, pipeline, job, runners, issue — and the
rest of the API had no op at all, so eight calls in one access-review session
went out as raw `glab api` and tripped the `use-supertool` reminder eight
times. A guard that fires on correct behaviour is a guard that stops being
read, including on the calls where it is right. This op exists so the reminder
can be honest.

It is deliberately not the thin wrapper the issue asked for, in three places.

**Reads only, and the method is pinned rather than defaulted.** The house rule
is every read through supertool, every write through `glab`. `glab api` flips
to POST on its own the moment a `-f`/`-F` is present, so "we did not pass a
method" is not the same claim as "we sent a GET" — the method is passed
explicitly and no flag from the caller is forwarded at all. The op takes one
thing, a path, and anything flag-shaped is refused with the raw `glab` command
named in the refusal. That refusal is the whole containment story: a passthrough
that accepts `-X POST` is not a bigger read op, it is an unaudited write surface
reachable from every alias and batch that can spell `gl-api`, with no
validators, no rollback and no confirmation in front of it. Note the guarantee
is about what supertool sends, not about what GitLab does with a GET.

**A full page is not a complete list.** `projects/:id/members/all` answers with
twenty rows whether the project has twenty members or a hundred and thirty
seven, and nothing in that body says which. Printing it as the answer is this
repo's own defect class — an absence produced by the tool, read as an absence
in the world — and the consequence is not abstract: the session that motivated
the issue was building an access review, where "these are the members" being
quietly "these are twenty of the members" is the failure that matters. So
there are three states and never two:

* fewer rows than `per_page` — there is no next page, and it says `complete`;
* exactly `per_page` rows — **unknown**, and it says `INCOMPLETE` with the
  count, the boundary it compared against, and the two ways to get the rest;
* `:full` — every page followed by `glab --paginate`, and it says so.

The comparison is against `min(per_page, 100)`, because GitLab silently caps
`per_page` at 100: `?per_page=200` returning 100 rows is a *full* page, and
comparing against the 200 that was asked for would call it complete. That is
the same defect one layer down.

**The body is remote text, and there is no shaped subset to mark.** Every other
op here knows which of its fields a stranger wrote. This one cannot, so the
whole body is treated as remote: fenced, with control characters disclosed and
the fence glyphs neutralised on the way in (`_untrusted`). Inside JSON the
encoder does most of that work — re-dumping escapes every C0 — but `ensure_ascii
=False` leaves U+2028 and U+2029 raw, and a non-JSON body has no encoder in
front of it at all, so `scrub()` is what both of those rely on.

A GitLab path that is not JSON (a raw blob, a proxy's HTML login page) is
rendered verbatim and labelled `NOT JSON`, never parsed-with-a-shrug and never
presented as the answer.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _untrusted  # noqa: E402  (the body is written by whoever opened the object)
from _env import env_int  # noqa: E402  (the one numeric-knob reader)

#: GitLab's default page size when a request names none.
DEFAULT_PER_PAGE = 20
#: GitLab's hard ceiling. A larger `per_page` is accepted and then ignored,
#: which is exactly the shape that turns a full page into a false `complete`.
MAX_PER_PAGE = 100

#: The one mode token. `full` rather than `all` because `all` is a real path
#: segment (`projects/:id/members/all`) and `full` is not.
MODE_FULL = "full"

_TIMEOUT = 45

#: A URL scheme at the front of the value: `http:`, `HtTpS://`, `file:`, and
#: anything else RFC 3986 spells the same way. Case-insensitive by construction
#: rather than by lowercasing the value, which would also have to be undone
#: before the value is quoted back to the caller.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

#: What a GitLab API path may contain: RFC 3986 `pchar` plus `/`. `:` is in
#: because glab's own placeholders need it (`projects/:id`), `@` because npm
#: scoped packages are spelled `packages/npm/@scope/name`, `%` because a group
#: path arrives percent-encoded. Everything else — control bytes, `#`, `<`,
#: `\\` — is out, and out is a refusal rather than a strip.
_PATH_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~%!$&'()*+,;=:@/"
)

#: The query may additionally carry `?` and the brackets GitLab's own filter
#: syntax uses (`?not[labels]=bug`).
_QUERY_CHARS = _PATH_CHARS | frozenset("?[]")


def parse_args(raw: str) -> tuple[str, bool]:
    """Split the `{argjoin}` argument into a path and the `:full` flag.

    Core splits an op on every ``:``, so `gl-api:projects/:id/members/all`
    arrives as two tokens and the placeholder `glab` needs would be lost by a
    plain rejoin of `{args}`. `{argjoin}` keeps the token boundaries visible
    (``:::``) and this rejoins them with the ``:`` they came from.

    The trailing token is a mode only when the token before it does not end in
    ``/`` — otherwise `projects/:full` (a path with a placeholder) and
    `users/1/events:full` (a path plus a mode) are indistinguishable.
    """
    tokens = raw.split(":::") if raw else []
    if (len(tokens) > 1 and tokens[-1] == MODE_FULL
            and not tokens[-2].endswith("/")):
        return ":".join(tokens[:-1]), True
    return ":".join(tokens), False


def host_naming_reason(candidate: str) -> str:
    """Why this string names a host of its own, or ``""`` (#1035).

    Three structural facts, in the order that gives the clearest sentence. Each
    one is a thing only an authority needs, and none of them appears in a
    GitLab API path:

    * a backslash — not a path separator here, and folded into ``/`` by the
      WHATWG URL parsing that a good deal of software does;
    * a scheme at the front, with or without the ``//`` after it (``http:host``
      is opaque-but-absolute and glab takes it);
    * a ``//`` anywhere in the path portion, which is what a protocol-relative
      ``//evil.host/x`` and a userinfo ``//gitlab.com@evil.host/x`` both need.

    The query is exempt from the ``//`` rule and only from that one: by the
    time a ``?`` has been seen the host is already decided, and
    ``?url=http://x`` is an ordinary search value.
    """
    head = candidate.split("?", 1)[0]
    if "\\" in candidate:
        return ("a backslash is not a path separator, and the parsers that "
                "disagree read it as one")
    if _SCHEME.match(head):
        return "it opens with a URL scheme, so it names its own host"
    if "//" in head:
        return "a // in the path opens an authority, not a path segment"
    return ""


def path_refusal(path: str) -> str:
    """Why this path is not something the op will send, or ``""``.

    Refusals rather than sanitisation: quietly stripping a `-X POST` off a path
    would send *a* request, and the caller would read the answer as the one
    they asked for. The same reasoning is what makes the URL check below a
    refusal — trimming `http://evil.host/` off the front would leave a path
    that resolves against the configured instance and answers with something,
    and the caller would read that answer as the one they asked for too.
    """
    if not path.strip():
        return ("ERROR: gl-api needs a path — gl-api:projects/:id/members/all. "
                "Run ./supertool 'help:gl-api' for the syntax.")
    if path.startswith("-"):
        return (
            f"ERROR: gl-api takes an API path, not a flag ({path.split()[0]!r}). "
            "It is GET-only by design: reads go through supertool, writes go "
            "through glab. For a write, call glab directly — "
            "glab api -X POST PATH -f key=value."
        )
    if any(c.isspace() for c in path):
        return (
            "ERROR: gl-api takes a single API path and forwards no flags, so a "
            "path containing whitespace is either a typo or a flag in "
            f"disguise: {path!r}. Percent-encode a literal space as %20. For a "
            "write, call glab directly: glab api -X POST PATH -f key=value."
        )
    # A path may not name a host (#1035). `glab api` accepts an absolute URL as
    # its endpoint and attaches the instance's Private-Token to it, so a value
    # carrying a scheme or an authority does not read the API — it hands a live
    # credential to whoever the value names. The check runs on the value and
    # again on its percent-decoded form, because `http%3A%2F%2F` and `%2F%2F`
    # are the same two shapes wearing an encoder.
    reason = (host_naming_reason(path)
              or host_naming_reason(urllib.parse.unquote(path)))
    if reason:
        return (
            f"ERROR: gl-api takes a GitLab API path, not a URL — {path!r} is "
            f"not a path: {reason}. glab attaches your GitLab token to "
            f"whatever host the endpoint names, so this is refused and not "
            f"rewritten: a stripped-down version would send a request you did "
            f"not ask for, and its answer would read as the one you did. Pass "
            f"the path alone — gl-api:projects/:id/members/all. To reach "
            f"another host, call glab yourself with credentials scoped to it."
        )
    head, _, query = path.partition("?")
    for label, part, allowed in (("path", head, _PATH_CHARS),
                                 ("query", query, _QUERY_CHARS)):
        for char in part:
            if char not in allowed:
                return (
                    f"ERROR: gl-api takes a GitLab API path and {char!r} "
                    f"cannot appear in the {label} of one: {path!r}. "
                    f"Percent-encode it as %XX if it is part of a name; a "
                    f"literal one is a typo or an attempt to reach past the "
                    f"API path, and neither is worth guessing between."
                )
    if path.split("?", 1)[0].strip("/") == "graphql":
        return (
            "ERROR: gl-api is GET-only and glab's graphql endpoint needs a "
            "POST body, which this op will not send. Call glab directly: "
            "glab api graphql -f query='...'."
        )
    return ""


def effective_per_page(path: str) -> int:
    """The page size this request will really get, not the one it asked for."""
    query = path.split("?", 1)[1] if "?" in path else ""
    requested = DEFAULT_PER_PAGE
    for field in query.split("&"):
        key, _, value = field.partition("=")
        if key == "per_page":
            try:
                requested = int(value)
            except ValueError:
                return DEFAULT_PER_PAGE
    if requested <= 0:
        return DEFAULT_PER_PAGE
    return min(requested, MAX_PER_PAGE)


def requested_page(path: str) -> int:
    query = path.split("?", 1)[1] if "?" in path else ""
    for field in query.split("&"):
        key, _, value = field.partition("=")
        if key == "page":
            try:
                return int(value)
            except ValueError:
                return 1
    return 1


def decode_documents(raw: str) -> list[Any] | None:
    """Every top-level JSON document in glab's stdout, or None if it is not JSON.

    `--paginate` emits one array per page back to back with no separator, so a
    single `json.loads` chokes on the second `[`. Same walk as
    `gitlab/pipeline.py`'s `_parse_paginated_json`; the two are twins and
    deduplicating them is a separate change to a file another branch is in.
    """
    decoder = json.JSONDecoder()
    docs: list[Any] = []
    idx, length = 0, len(raw)
    while idx < length:
        while idx < length and raw[idx].isspace():
            idx += 1
        if idx >= length:
            break
        try:
            doc, idx = decoder.raw_decode(raw, idx)
        except ValueError:
            return None
        docs.append(doc)
    return docs or None


def classify_error(stderr: str, path: str) -> str:
    """One sentence per remedy — auth, permissions, path, everything else."""
    s = stderr.lower()
    if "404" in s or "not found" in s or "could not resolve" in s:
        return (f"ERROR: GitLab returned not found for {_untrusted.flat(path)!r}. "
                "Check the path and that you are in the right project.")
    if ("401" in s or "unauthenticated" in s or "unauthorized" in s
            or "authenticate" in s or "bad token" in s or "token expired" in s):
        return "ERROR: glab not authenticated. Run: glab auth login"
    if "403" in s or "forbidden" in s:
        return (f"ERROR: permission denied for {_untrusted.flat(path)!r}. "
                "Check your GitLab token scopes.")
    return (f"ERROR: glab failed for {_untrusted.flat(path)!r}: "
            f"{_untrusted.flat(stderr.strip())}")


def completeness(items: list[Any], path: str, paginated: bool,
                 pages: int) -> str:
    """The one line that says whether this is the whole answer.

    Three states, and the middle one is the reason the op is not a passthrough:
    a page that came back exactly full is not evidence of anything, and saying
    `complete` there would be the tool inventing a fact.
    """
    count = len(items)
    page = requested_page(path)
    tail = ""
    if page > 1 and not paginated:
        tail = (f" — page {page} was requested, so pages 1-{page - 1} were "
                f"never fetched")
    if paginated:
        return (f"complete: {count} items across {pages} "
                f"{'page' if pages == 1 else 'pages'} (every page followed)")
    per_page = effective_per_page(path)
    if count < per_page:
        return (f"complete: {count} items — fewer than the page size of "
                f"{per_page}, so GitLab has no next page{tail}")
    return (
        f"INCOMPLETE: {count} items — exactly the page size of {per_page}, so "
        f"GitLab may have more and this is not the whole list{tail}. "
        f"Re-run with :full to follow every page, or add ?per_page=100&page=2."
    )


def _cap_array(items: list[Any], cap: int) -> tuple[str, str]:
    """Render as many items as fit, keeping the emitted JSON valid.

    Cutting an array by bytes leaves a document that does not parse, and a
    reader who pastes it into a decoder gets a syntax error instead of the
    count they were after. Cutting by item costs one number and keeps both.
    """
    body = json.dumps(items, indent=2, ensure_ascii=False)
    if len(body) <= cap or not items:
        return body, ""
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(json.dumps(items[:mid], indent=2, ensure_ascii=False)) <= cap:
            lo = mid
        else:
            hi = mid - 1
    body = json.dumps(items[:lo], indent=2, ensure_ascii=False)
    note = (f"CAPPED: {lo} of {len(items)} items shown — cut to stay under "
            f"GL_API_MAX_BYTES={cap}; the JSON above is a valid array of the "
            f"first {lo}")
    return body, note


def _cap_text(text: str, cap: int, is_json: bool) -> tuple[str, str]:
    if len(text) <= cap:
        return text, ""
    note = (f"TRUNCATED: {cap} of {len(text)} characters shown "
            f"(GL_API_MAX_BYTES={cap})")
    if is_json:
        note += " — the JSON above is cut and does not parse"
    return text[:cap], note


def render(raw_stdout: str, path: str, paginated: bool, cap: int) -> None:
    shown_path = _untrusted.flat(path)
    print(f"# gl-api GET {shown_path}")
    print(_untrusted.banner())

    docs = decode_documents(raw_stdout)
    if docs is None:
        body, note = _cap_text(raw_stdout, cap, is_json=False)
        print(f"NOT JSON: {len(raw_stdout)} characters, shown verbatim below")
        print(_untrusted.fence(body))
        if note:
            print(note)
        return

    if all(isinstance(doc, list) for doc in docs):
        items: list[Any] = []
        for doc in docs:
            items.extend(doc)
        body, note = _cap_array(items, cap)
        print(_untrusted.fence(body))
        if note:
            print(note)
        print(completeness(items, path, paginated, len(docs)))
        return

    value = docs[0] if len(docs) == 1 else docs
    body, note = _cap_text(
        json.dumps(value, indent=2, ensure_ascii=False), cap, is_json=True)
    print(_untrusted.fence(body))
    if note:
        print(note)


def main() -> int:
    raw_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    path, paginated = parse_args(raw_arg)

    refusal = path_refusal(path)
    if refusal:
        print(refusal)
        return 1

    cmd = ["glab", "api", "--method", "GET", path]
    if paginated:
        cmd.append("--paginate")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("ERROR: glab not found — install from "
              "https://gitlab.com/gitlab-org/cli")
        return 1
    except subprocess.TimeoutExpired:
        print(f"ERROR: glab timed out after {_TIMEOUT}s for "
              f"{_untrusted.flat(path)!r} — retry, or narrow the query with "
              f"?per_page=20")
        return 1

    if result.returncode != 0:
        print(classify_error(result.stderr, path))
        return 1

    render(result.stdout, path, paginated,
           env_int("GL_API_MAX_BYTES", 65536, minimum=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
