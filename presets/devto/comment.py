#!/usr/bin/env python3
"""Dev.to comment: devto_comment:ARTICLE_ID_OR_SLUG_OR_URL|MESSAGE[|PARENT_COMMENT_ID[|force]]

Accepts numeric article ID, slug (`author/slug`), or full URL — non-numeric
inputs are resolved to the numeric ID via /api/articles/{slug} before posting.
The Comments write endpoint requires the integer ID.

⚠ SESSION-COOKIE OP — POSSIBLE ToS RISK ⚠
=========================================
The Dev.to public API does NOT expose a comment-write endpoint. This
op posts via the same web endpoint a logged-in browser uses, using
DEVTO_SESSION_COOKIE (env or ~/.config/devto/session_cookie) and a
scraped CSRF token. That is automation against a non-public surface
and may violate Dev.to's Terms of Service. Use at your own risk, on
your own account only. The op refuses to run unless the cookie is
explicitly provided. See _session.py for opt-in instructions.

PARENT_COMMENT_ID is the alphanumeric id_code from devto_comments /
devto_status_since output (e.g. "37d65"). Forem's CommentsController
permits `parent_id` as the numeric DB id only; the public API does
not expose it, so we resolve id_code → numeric id by scraping the
comment URL HTML on demand.

PRE-FLIGHT DUPLICATE CHECK: Before posting, the op fetches existing
comments on the article and aborts if the authenticated user has already
commented (matched by username from DEVTO_API_KEY lookup). Pass `|force`
as the 4th pipe-separated field to bypass: devto_comment:slug|MSG||force

If the pre-flight cannot run — the user cannot be identified, or the
comments lookup fails — the op ABORTS rather than posting (#562). It used
to degrade to "no duplicate", which is the same value as a genuinely clean
article, so a failed check posted a duplicate and said nothing. Declining
is recoverable with `|force`; a duplicate comment on someone else's
article is not. This matches hashnode_comment, which was already
fail-closed.
"""
from __future__ import annotations

import http.client
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _publish_safety
from _outbound import append as track_append
from _resolve import resolve_article_id
from _session import fetch_csrf_token, get_session_cookie, web_post_json
from _publish_safety import safe_resolve_body_path  # noqa: E402
from _http import RedirectRefused, ResponseTooLarge, read_capped, urlopen  # noqa: E402

WEB_BASE = "https://dev.to"
API_BASE = "https://dev.to/api"
_COMMENT_ID_RE = re.compile(r'comment-id="(\d+)"')

_FILE_PREFIX = "file://"


def _resolve_body(arg: str) -> tuple[str, bool]:
    """Resolve a body argument to its text content. Returns (text, from_file).

    - `file://path` MUST be an existing file — errors otherwise (catches
      typos that the legacy bare-path auto-detect would forward as text).
    - bare path that `is_file()` reads the file (backward compat).
    - anything else returned as-is.
    """
    if arg.startswith(_FILE_PREFIX):
        path_str = arg[len(_FILE_PREFIX):]
        resolved = safe_resolve_body_path(path_str)
        if not resolved.is_file():
            sys.stderr.write(
                f"ERROR: file not found: {path_str}\n"
                "(file:// prefix requires the file to exist — typo or wrong path?)\n"
            )
            sys.exit(2)
        return resolved.read_text(encoding="utf-8"), True
    try:
        p = Path(arg)
        if p.is_file():
            resolved = safe_resolve_body_path(arg)
            return resolved.read_text(encoding="utf-8"), True
    except OSError:
        pass
    return arg, False


def parse_args(arg: str) -> tuple[str, str, str | None, bool]:
    parts = arg.split("|")
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        sys.stderr.write("ERROR: usage devto_comment:ARTICLE_ID_OR_SLUG_OR_URL|MESSAGE_OR_FILE[|PARENT_COMMENT_ID[|force]]\n")
        sys.exit(2)
    raw = parts[0].strip()
    text, from_file = _resolve_body(parts[1])
    message = text.strip() if from_file else text
    parent = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    force = len(parts) > 3 and parts[3].strip().lower() == "force"
    return raw, message, parent, force


def preflight_comment(aid: int, me: str) -> tuple[bool | None, list[str], str]:
    """Check if `me` has already commented on article `aid`.

    Returns (already_commented, existing_ids, last_date), or (None, [], "")
    when the lookup itself could not be made — the caller must fail closed.

    `False` and `None` used to be the same value here (#562). `False` means
    *we looked and there is no comment of ours*; `None` means *we did not
    look*. Collapsing them made a failed check indistinguishable from a clean
    article, and the only caller reads it to decide whether to post.

    The two sibling imports sit **outside** the `try` deliberately. A missing
    `_auth` or `_rest` is a broken checkout, not a platform hiccup, and it must
    surface as an `ImportError` naming the module — which sends the reader to
    `sys.path` — rather than as a plausible tuple, which sends them to an
    assertion in the wrong file. #555 made that reachable: `sys.path` is now
    restored after each preset load, so the shim eviction every loader performs
    is no longer papered over by a leftover path entry.
    """
    if not me:
        return None, [], ""
    from _auth import get_api_key
    from _rest import request
    try:
        items = request("GET", "/comments", get_api_key(), query={"a_id": aid, "per_page": 1000})
    except Exception:
        return None, [], ""
    if not isinstance(items, list):
        return None, [], ""
    flat: list[dict] = []
    for c in items:
        flat.append(c)
        flat.extend(c.get("children") or [])
    mine = [c for c in flat if (c.get("user") or {}).get("username") == me]
    if not mine:
        return False, [], ""
    ids = [c.get("id_code") or str(c.get("id", "?")) for c in mine]
    last = max((c.get("created_at") or "") for c in mine).split("T")[0]
    return True, ids, last


def main(arg: str) -> None:
    raw, message, parent, force = parse_args(arg)
    aid = resolve_article_id(raw)
    cookie = get_session_cookie()
    if not cookie:
        sys.stderr.write(
            "ERROR: devto_comment requires DEVTO_SESSION_COOKIE (env or "
            "~/.config/devto/session_cookie). The Dev.to API has no public "
            "comment-write endpoint — see _session.py for opt-in instructions.\n"
        )
        sys.exit(2)
    if not force:
        try:
            from _auth import get_api_key
            from _me import get_username
            me = get_username(get_api_key())
        except Exception as exc:
            sys.stderr.write(
                f"ABORT — pre-flight failed (cannot identify user: {exc}). "
                "Use |force as 4th field to bypass and post anyway.\n"
            )
            sys.exit(1)
        already, ids, last = preflight_comment(aid, me)
        if already is None:
            sys.stderr.write(
                f"ABORT — pre-flight lookup failed for article {aid} "
                "(cannot verify whether already commented). "
                "Use |force as 4th field to bypass and post anyway.\n"
            )
            sys.exit(1)
        if already:
            sys.stderr.write(
                f"ABORT — already commented {len(ids)}× on article {aid} "
                f"(ids: {', '.join(ids)}, last {last}). "
                "Use |force as 4th field to override.\n"
            )
            sys.exit(1)
    csrf = fetch_csrf_token(cookie)
    body: dict[str, object] = {
        "comment": {
            "body_markdown": message,
            "commentable_id": aid,
            "commentable_type": "Article",
        }
    }
    if parent:
        if parent.isdigit():
            body["comment"]["parent_id"] = int(parent)  # type: ignore[index]
        else:
            numeric = _resolve_parent_numeric_id(parent)
            if numeric is None:
                sys.stderr.write(
                    f"ERROR: could not resolve parent id_code {parent!r} to numeric id "
                    "(needed for Forem's parent_id field). Comment NOT posted.\n"
                )
                sys.exit(1)
            body["comment"]["parent_id"] = numeric  # type: ignore[index]
    text, status = web_post_json("/comments", cookie, csrf, body)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}
    if isinstance(data, dict) and data.get("error"):
        sys.stderr.write(f"ERROR: Dev.to: {data['error']}\n")
        sys.exit(1)
    cid = data.get("id_code") or data.get("id") or "?"
    url = data.get("path") or data.get("url") or ""
    import datetime as _dt
    track_append({
        "comment_id": cid,
        "article_id": aid,
        "parent_id": parent,
        "posted_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    print(f"(comment posted id={cid} url={url} mode=session)")
    _print_post_confirmation(str(aid), str(cid))


def _print_post_confirmation(aid: str, cid: str) -> None:
    """Re-fetch posted comment from public API and echo its body — guards
    against silent body truncation (e.g. supertool ':' tokenization eating
    part of the message)."""
    try:
        from _auth import get_api_key
        from _rest import request
        items = request("GET", "/comments", get_api_key(), query={"a_id": aid, "per_page": 1000})
    except Exception as exc:  # pragma: no cover — defensive
        print(f"(post-confirm fetch failed: {exc})")
        return
    if not isinstance(items, list):
        return
    found = _find(items, cid)
    if not found:
        print(f"(post-confirm: comment {cid} not yet visible — API replication delay)")
        return
    body = (found.get("body_html") or "").strip()
    print(f"--- posted body ({len(body)} chars) ---\n{body}")


def _resolve_parent_numeric_id(id_code: str, timeout: int = 15) -> int | None:
    """Resolve a comment id_code (alphanumeric) to its numeric DB id.

    The Dev.to public API exposes id_code only. Forem's CommentsController
    permits `parent_id` as the integer DB id, so a reply post fails silently
    (lands as top-level) when given an id_code string.

    Two-hop strategy:
      1. GET /api/comments/{id_code} → user.username
      2. GET /{username}/comment/{id_code} HTML → regex `comment-id="N"`

    Returns None on any failure (caller decides whether to abort).
    """
    try:
        meta_req = urllib.request.Request(
            f"{API_BASE}/comments/{id_code}",
            headers={"User-Agent": "claude-supertool/devto-resolver", "Accept": "application/json"},
        )
        with urlopen(meta_req, timeout=timeout) as resp:
            meta = json.loads(read_capped(resp).decode("utf-8"))
    except (RedirectRefused, ResponseTooLarge) as e:
        # No credential rides on this hop, so the best-effort contract holds and
        # we still return None — but name what was refused, so it does not read
        # as an ordinary miss.
        print(f"(parent resolve: {e})")
        return None
    except (urllib.error.URLError, json.JSONDecodeError, http.client.HTTPException):
        return None
    username = ((meta or {}).get("user") or {}).get("username")
    if not username:
        return None
    try:
        html_req = urllib.request.Request(
            f"{WEB_BASE}/{username}/comment/{id_code}",
            headers={"User-Agent": "Mozilla/5.0 (claude-supertool/devto-resolver)"},
        )
        with urlopen(html_req, timeout=timeout) as resp:
            html = read_capped(resp).decode("utf-8", errors="replace")
    except (RedirectRefused, ResponseTooLarge) as e:
        print(f"(parent resolve: {e})")
        return None
    except (urllib.error.URLError, http.client.HTTPException):
        return None
    m = _COMMENT_ID_RE.search(html)
    return int(m.group(1)) if m else None


def _find(items: list, target: str) -> dict | None:
    for c in items:
        if c.get("id_code") == target:
            return c
        for child in c.get("children") or []:
            r = _find([child], target)
            if r:
                return r
    return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("ERROR: missing arg\n")
        sys.exit(2)
    # Supertool {args} passes parts space-separated; rejoin with ':' so a body
    # containing ':' survives the supertool tokenizer.
    arg = ":".join(sys.argv[1:])
    main(arg)  # parse_args now returns 4-tuple; main() unpacks it internally
