#!/usr/bin/env python3






















































from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  
import _untrusted  
import _auth_probe  
import _status_probe  
from _env import env_int  


DEFAULT_PER_PAGE = 20


MAX_PER_PAGE = 100



MODE_FULL = "full"

_TIMEOUT = 45





_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")






_PATH_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~%!$&'()*+,;=:@/"
)



_QUERY_CHARS = _PATH_CHARS | frozenset("?[]")


def parse_args(raw: str) -> tuple[str, bool]:











    tokens = raw.split(":::") if raw else []
    if (len(tokens) > 1 and tokens[-1] == MODE_FULL
            and not tokens[-2].endswith("/")):
        return ":".join(tokens[:-1]), True
    return ":".join(tokens), False


def _authority_reason(head: str) -> str:











    if _SCHEME.match(head):
        return "it opens with a URL scheme, so it names its own host"
    if "//" in head:
        return "a // in the path opens an authority, not a path segment"
    return ""


def host_naming_reason(candidate: str) -> str:











    if "\\" in candidate:
        return ("a backslash is not a path separator, and the parsers that "
                "disagree read it as one")
    return _authority_reason(candidate.split("?", 1)[0])


def decoded_host_naming_reason(candidate: str) -> str:



















































    folded = candidate.replace("\\", "/")
    return _authority_reason(folded.split("?", 1)[0])


def dot_segment_reason(path: str) -> str:










    segments = path.split("?", 1)[0].split("/")
    if "." in segments or ".." in segments:
        return ("a bare '.' or '..' path segment is not something this op "
                "will send unmodified")
    return ""


def path_refusal(path: str) -> str:









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











    decoded_path = urllib.parse.unquote(path)
    reason = (host_naming_reason(path)
              or decoded_host_naming_reason(decoded_path))
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







    dot_reason = dot_segment_reason(decoded_path)
    if dot_reason:
        return (
            f"ERROR: gl-api takes a GitLab API path and {dot_reason}: "
            f"{path!r}. A normalized version would ask for a different "
            f"endpoint than the one you named, and this op does not silently "
            f"rewrite what it sends. Pass the literal path you mean, or call "
            f"glab directly if a dot segment is genuinely part of a filename."
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

    s = stderr.lower()
    if _status_probe.says_not_found(s):
        return (f"ERROR: GitLab returned not found for {_untrusted.flat(path)!r}. "
                "Check the path and that you are in the right project.")



    if _auth_probe.says_not_authenticated(s, _auth_probe.GITLAB_MARKERS):
        return "ERROR: glab not authenticated. Run: glab auth login"
    if _status_probe.says_forbidden(s):
        return (f"ERROR: permission denied for {_untrusted.flat(path)!r}. "
                "Check your GitLab token scopes.")
    return (f"ERROR: glab failed for {_untrusted.flat(path)!r}: "
            f"{_untrusted.flat(stderr.strip())}")


def completeness(items: list[Any], path: str, paginated: bool,
                 pages: int) -> str:






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
    use_utf8_stdout()
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
        print("ERROR: glab not found — install the GitLab CLI")
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
           env_int(os.environ.get("GL_API_MAX_BYTES"), "GL_API_MAX_BYTES", 65536, minimum=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
