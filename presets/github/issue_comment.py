#!/usr/bin/env python3
"""Comment on a GitHub issue, and prove what landed (#2078).

`gh-issue-create` opens an issue and `gh-pr-edit` corrects a published pull
request body; there was no op for the third routine write, a follow-up
comment on an issue already filed, so it left supertool for raw
`gh issue comment --body-file`. Observed on `Digital-Process-Tools/claude-oss`:
filed an issue with `gh-issue-create:@FILE`, needed a scoping comment right
after, and had no op for it while every other step of the same task had one.

A trailing `:edit=COMMENT_ID` corrects a comment already posted (#2643).
Observed 2026-09-19: a comment on #1868 went out with a wrong figure, and the
only route to fix it was raw `gh api -X PATCH repos/.../issues/comments/ID
-f body=@file`, hand-verified afterwards -- no byte-identical read-back, and
no re-application of the #2100 disclosure marker if the corrected body
dropped it. `edit=COMMENT_ID` PATCHes `repos/{repo}/issues/comments/{id}`
instead of POSTing a new comment, then runs the same read-back and the same
disclosure step this op already gives a fresh comment. The id is already in
this op own `[result]` line from when the comment was first posted
(`comment id=5741496503`), so the caller has it without another lookup.

A GET of the comment runs before the PATCH (#2665): the release auditor found
that `edit=COMMENT_ID` sent the PATCH built from the id alone, with `NUMBER`
never checked against the comment being edited -- a mistyped or stale id
silently overwrote some other comment in the repo while the `[result]` line
still reported success against the `NUMBER` the caller asked for. The GET's
`issue_url` is compared against `/issues/{NUMBER}`; a mismatch, or a GET that
fails outright, refuses before any PATCH is sent.

`gh-pr-edit` (#1739) set the bar for a write in this family: publish, then
read back what the server actually stored and compare it byte for byte
against what was sent. `gh issue comment` gives no such proof -- a 0 exit
means the API accepted the POST, not that the stored body is the one this
call meant to publish. This carries the same three-part guarantee:

* **The payload route**, because a comment body is free-form prose with
  newlines and colons that cannot survive `:`-tokenization -- the same
  argument `gh-pr-create` and `gh-pr-edit` are built on.
* **The write goes through REST** (`POST
  repos/{repo}/issues/{number}/comments`), which is a plain create with no
  GraphQL field set to go stale under it the way `gh pr edit`'s did (#1739).
* **The receipt.** The POST response carries the stored body, the comment id
  and its URL -- compared against what was sent in the same call, in the
  same four states `gh-pr-edit` uses: EXACT, line endings NORMALISED by the
  server, MISMATCH naming the first differing line, or UNKNOWN when the
  response carried no body field at all. Only EXACT and NORMALISED exit 0.

No closing-reference gate here -- a comment does not replace a published
body, so there is nothing an update could drop. No title: `edit=COMMENT_ID`
is the only mode token this op takes, and it switches the write between the
two shapes above (publish a new comment, or correct one already posted) --
it does not add a third one.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
import _remote_default as _rd  # noqa: E402
import _repo_target  # noqa: E402
import _payload_keys  # noqa: E402  (unrecognised-key refusal, shared with the other @payload ops -- #2123)
import _untrusted  # noqa: E402  (the GitHub API writes the receipt fields -- #1606)
import _digits  # noqa: E402
import _publish_safety  # noqa: E402  (#691 T4 -- the #2100 disclosure marker, wired here too)

# What the server holds, against what was sent -- the same four states
# gh-pr-edit uses for its own read-back (#1739).
LANDED_EXACT = "exact"
LANDED_NORMALISED = "normalised"
LANDED_MISMATCH = "mismatch"
LANDED_UNKNOWN = "unknown"

ACCEPTED_KEYS = {"repo", "body", "body_file"}
ALIASES: dict = {}

CRLF = chr(13) + chr(10)
CR = chr(13)
LF = chr(10)

EDIT_PREFIX = "edit="


# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------

def parse_args(argv: List[str]) -> Tuple[str, str, str, str]:
    """`(number, payload_path, edit_id, error)` -- a non-empty error means stop.

    A trailing `edit=COMMENT_ID` switches this op from posting a new
    comment to correcting one already on the issue (#2643) -- PATCH instead
    of POST, same payload, same read-back. Any other trailing token is
    REFUSED rather than ignored -- the same reasoning `gh-pr-edit` gives for
    its own `unlink` token: an ignored mode word on a writing op runs a call
    nobody typed, at exit 0.
    """
    tokens = [t for t in argv if t != ""]
    if not tokens:
        return ("", "", "", (
            "ERROR: gh-issue-comment needs an issue number and a payload -- "
            "gh-issue-comment:NUMBER:@FILE, or @- for stdin (JSON or TOML "
            "with body or body_file). To correct an already-posted comment, "
            "add :edit=COMMENT_ID."))

    number = tokens[0]
    if not _digits.is_ascii_int(number):
        return ("", "", "", (
            f"ERROR: {number!r} is not an issue number. gh-issue-comment "
            f"takes the number of an issue that already exists -- "
            f"gh-issue-comment:NUMBER:@FILE, or gh-issue-comment:NUMBER:@- "
            f"for stdin."))

    rest = tokens[1:]
    edit_id = ""
    if rest and rest[-1].startswith(EDIT_PREFIX):
        edit_id = rest[-1][len(EDIT_PREFIX):]
        rest = rest[:-1]
        if not _digits.is_ascii_int(edit_id):
            return ("", "", "", (
                f"ERROR: {edit_id!r} is not a comment id. edit=COMMENT_ID "
                f"takes the numeric id gh-issue-comment's own [result] line "
                f"reports (comment id=...)."))

    if not rest:
        return ("", "", "", (
            "ERROR: gh-issue-comment needs a payload file -- "
            "gh-issue-comment:NUMBER:@FILE, or @- to read it from stdin."))
    # A Windows drive letter is reassembled, the same way gh-pr-edit
    # reassembles one: supertool splits the op argument on ':', so
    # `gh-issue-comment:2078:@C:\repo\note.toml` arrives as `@C` plus the
    # rest. Refusing that as an unknown trailing token would misreport a
    # caller whose only mistake was standing on Windows.
    if len(rest) > 1 and len(rest[0]) == 2 and rest[0][1:].isalpha():
        rest = [":".join(rest)]
    if len(rest) > 1:
        return ("", "", "", (
            f"ERROR: gh-issue-comment does not take {rest[1]!r}. The only "
            f"token past the payload is edit=COMMENT_ID. Nothing was "
            f"written."))

    return (number, rest[0], edit_id, "")


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------

def validate(payload: dict) -> str | None:
    """The refusals, before anything is published. None means the payload is fine."""
    if not payload.get("repo"):
        return ("ERROR: payload missing required field: repo -- and it could "
                "not be resolved from the origin remote either.")
    if "body" in payload and payload.get("body_file"):
        return "ERROR: payload has both body and body_file — use one"
    if "body" not in payload and not payload.get("body_file"):
        return ("ERROR: payload has neither body nor body_file, so this op "
                "has nothing to publish. An absent body is not an empty "
                'one -- set body = "" if that is really what you mean.')
    # #2322: a payload whose `body` key parses as a TOML array-of-tables
    # (`[[body]]`, a list of `{"value": ...}` dicts) used to reach
    # `_body_text`'s `str(payload.get("body") or "")` unchecked, which
    # happily stringifies a list -- publishing the literal Python repr
    # (`[{'value': '...'}]`) as the comment body. Confirmed against a real
    # posted comment on issue #2310. #2315 made the same call for
    # gh-issue-create's sibling case: refuse rather than guess at a join.
    # Applied here too, for the same reason -- a published comment cannot
    # be un-sent, so a loud refusal beats a silent garbage write.
    if "body" in payload and not isinstance(payload.get("body"), str):
        return (
            "ERROR: body must be a string, not "
            f"{type(payload['body']).__name__} -- gh-issue-comment does "
            "not accept a TOML [[body]] table-array. Pass body as a "
            "single string, or use body_file to read one from a file."
        )
    return None


def _load_payload(path: str) -> dict:
    if path.startswith("@"):
        path = path[1:]
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(
        encoding="utf-8")
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    if tomllib is None:
        raise ValueError(
            "TOML payload requires Python 3.11+ or `pip install tomli`")
    return tomllib.loads(raw)


def _body_text(payload: dict) -> Tuple[str, str]:
    """`(content, error)` -- the bytes to publish, from `body` or `body_file`.

    `validate()` already refuses a non-string `body` before this runs on the
    real `main()` path (#2322) -- checked again here so a direct caller of
    this function gets the same refusal rather than a stringified `repr()`
    of whatever it was handed.
    """
    body_file = payload.get("body_file")
    if not body_file:
        body = payload.get("body")
        if body is not None and not isinstance(body, str):
            return ("", (
                "ERROR: body must be a string, not "
                f"{type(body).__name__} -- gh-issue-comment does not "
                "accept a TOML [[body]] table-array. Pass body as a "
                "single string, or use body_file to read one from a file."
            ))
        return (str(body or ""), "")
    if Path(body_file).is_dir():
        return ("", f"ERROR: body_file is a directory, not a file: {body_file}")
    try:
        return (Path(body_file).read_text(encoding="utf-8"), "")
    except FileNotFoundError:
        return ("", f"ERROR: body_file not found: {body_file}")
    except PermissionError as e:
        return ("",
                f"ERROR: permission denied reading body_file: {body_file} — {e}")


# ---------------------------------------------------------------------------
# the receipt -- the same read-back gh-pr-edit's landed_verdict performs
# ---------------------------------------------------------------------------

def _newlines_only(text: str) -> str:
    return text.replace(CRLF, LF).replace(CR, LF)


def _first_difference(sent: str, stored: str) -> str:
    sent_lines = _untrusted.split_lines(sent)
    stored_lines = _untrusted.split_lines(stored)
    for i in range(max(len(sent_lines), len(stored_lines))):
        a = sent_lines[i] if i < len(sent_lines) else "(end of body)"
        b = stored_lines[i] if i < len(stored_lines) else "(end of body)"
        if a != b:
            return (f"first difference at line {i + 1}: "
                    f"sent {_untrusted.flat(a[:120])!r}, "
                    f"stored {_untrusted.flat(b[:120])!r}")
    return "the lines match; the difference is in trailing bytes"


def landed_verdict(sent: str, stored: object) -> Tuple[str, str]:
    """`(state, message)` -- what the server holds, against what was sent.

    Four states. `unknown` is the one that matters: a response with no
    `body` field says the write was accepted and says nothing about the
    bytes, and rendering that as success is the receipt failure this op
    exists to close.
    """
    if not isinstance(stored, str):
        return (LANDED_UNKNOWN, (
            "the POST response carried no body field, so what the server "
            "now holds is UNKNOWN. The write was accepted; whether these "
            "bytes are the ones on it was not established."))
    if stored == sent:
        return (LANDED_EXACT,
                f"byte-identical to what was sent ({len(sent)} characters)")
    if _newlines_only(stored) == _newlines_only(sent):
        return (LANDED_NORMALISED, (
            f"identical apart from line endings, which the server "
            f"normalised ({len(sent)} characters sent, {len(stored)} "
            f"stored). Nothing was lost."))
    return (LANDED_MISMATCH, (
        f"NOT what was sent: {len(sent)} characters sent, {len(stored)} "
        f"stored. {_first_difference(sent, stored)}"))


def result_line(number: str, landed_state: str, comment_id: object,
                 edited: bool = False) -> str:
    """One line, no newline, that survives `| tail -1`."""
    if landed_state == LANDED_EXACT:
        landed = "comment verified byte-identical on the server"
    elif landed_state == LANDED_NORMALISED:
        landed = "comment verified, line endings normalised by the server"
    elif landed_state == LANDED_MISMATCH:
        landed = "comment on the server is NOT what was sent — UNVERIFIED"
    else:
        landed = "what the server holds was not read back — UNVERIFIED"
    id_note = f" id={comment_id}" if comment_id not in (None, "") else ""
    verb = "edited" if edited else "posted"
    return f"[result] issue #{number} comment{id_note} {verb}; {landed}"


def refusal_line(number: str, why: str, edited: bool = False) -> str:
    verb = "NOT edited" if edited else "NOT commented"
    return f"[result] issue #{number} {verb}; {why} — nothing was written"


def edit_target_error(check_response: object, check_err: str, number: str) -> str:
    """Empty string means `check_response` is the comment #number owns.

    Anything else is the refusal reason: either the GET that should have
    returned the comment failed outright, or it returned a comment whose own
    `issue_url` does not end in `/issues/{number}` -- the ownership check
    #2665 added ahead of the PATCH, so a mistyped or stale COMMENT_ID can no
    longer silently overwrite a comment on some other issue or PR.
    """
    if not isinstance(check_response, dict):
        return (f"could not verify the comment belongs to issue #{number} "
                f"before editing it ({check_err or 'no detail'})")
    # The GitHub API writes issue_url, same as html_url a few lines below in
    # main() -- flatten it before it can reach column 0 of this op's own
    # receipt (#1606, the reason _untrusted is imported here at all).
    issue_url = _untrusted.flat(str(check_response.get("issue_url") or ""))
    if not issue_url.endswith(f"/issues/{number}"):
        return (f"that comment belongs to "
                f"{issue_url or '(no issue_url in the response)'}, not "
                f"issue #{number} — refusing to edit it")
    return ""


# ---------------------------------------------------------------------------
# gh plumbing
# ---------------------------------------------------------------------------

def _gh_json(args: List[str], stdin: str | None = None,
             timeout: int = 30) -> Tuple[object, str]:
    try:
        result = subprocess.run(["gh"] + args, capture_output=True, text=True,
                                input=stdin, timeout=timeout, encoding="utf-8",
                                errors="replace")
    except FileNotFoundError:
        return (None, "gh not found — install from https://cli.github.com")
    except subprocess.TimeoutExpired:
        return (None, "gh timed out")
    except OSError as e:
        return (None, f"gh could not be run: {e}")
    if result.returncode != 0:
        tail = _untrusted.split_lines((result.stderr or result.stdout).strip())
        return (None, _untrusted.flat(tail[-1]) if tail
                else f"gh exited {result.returncode}")
    try:
        return (json.loads(result.stdout or "null"), "")
    except json.JSONDecodeError:
        return (None, "gh returned invalid JSON")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    use_utf8_stdout()
    number, raw_arg, edit_id, err = parse_args(sys.argv[1:])
    if err:
        print(err)
        return 1

    path = raw_arg[1:] if raw_arg.startswith("@") else raw_arg
    if path != "-" and Path(path).is_dir():
        print(f"ERROR: payload path is a directory, not a file: {path}")
        return 1
    try:
        payload = _load_payload(raw_arg)
    except FileNotFoundError:
        print(f"ERROR: payload file not found: {path}")
        return 1
    except IsADirectoryError:
        print(f"ERROR: payload path is a directory, not a file: {path}")
        return 1
    except PermissionError as e:
        print(f"ERROR: permission denied reading payload: {path} — {e}")
        return 1
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: failed to parse payload: {e} "
              f"(expected JSON or TOML with body or body_file)")
        return 1
    if not isinstance(payload, dict):
        print("ERROR: payload is not a table of fields (expected JSON or "
              "TOML with body or body_file)")
        return 1

    key_err = _payload_keys.check(payload, ACCEPTED_KEYS, ALIASES, "gh-issue-comment")
    if key_err:
        print(key_err)
        return 1

    repo_conflict, repo_source = _repo_target.resolve_or_conflict(payload, "gh-issue-comment")
    if repo_conflict:
        print(repo_conflict)
        return 1
    if not payload.get("repo"):
        auto = _rd.resolve("github_repo", "github.com")
        if auto:
            payload["repo"] = auto
            repo_source = "cwd remote / config default"

    err = validate(payload)
    if err:
        print(err)
        return 1

    content, err = _body_text(payload)
    if err:
        print(err)
        return 1

    # #691 T4: `gh-pr-create`, `gh-pr-edit` and `gh-issue-create` already
    # append the #2100 authorship marker; this op -- the fourth routine
    # forge write, added later by #2078 -- never got it. No closing-reference
    # parse runs on this body (a comment has no `Closes #N` gate), so unlike
    # `gh-pr-edit` there is no "after the parse" ordering constraint here --
    # the marker can be applied as soon as the body to send is known.
    content, disclosure_state = _publish_safety.apply_forge_disclosure(content)

    repo = str(payload["repo"])
    repo_note = (f"  (repo from {repo_source})"
                 if repo_source not in ("", "payload") else "")
    header = f"# gh-issue-comment — {repo}#{number}{repo_note}"
    if edit_id:
        header += f" (editing comment {edit_id})"
    print(header)

    if edit_id:
        check_response, check_err = _gh_json(
            ["api", f"repos/{repo}/issues/comments/{edit_id}"], timeout=30)
        owner_err = edit_target_error(check_response, check_err, number)
        if owner_err:
            print()
            print(f"ERROR: {owner_err}")
            print(refusal_line(number, owner_err, True))
            return 1
        endpoint = f"repos/{repo}/issues/comments/{edit_id}"
        method = "PATCH"
    else:
        endpoint = f"repos/{repo}/issues/{number}/comments"
        method = "POST"

    response, write_err = _gh_json(
        ["api", "-X", method, "-H", "Accept: application/vnd.github+json",
         endpoint, "--input", "-"],
        stdin=json.dumps({"body": content}), timeout=30)

    if not isinstance(response, dict):
        why = write_err or "no detail"
        action = "edit" if edit_id else "comment"
        print()
        print(f"ERROR: the {action} was refused ({why})")
        print(refusal_line(number, why, bool(edit_id)))
        return 1

    landed_state, landed_msg = landed_verdict(content, response.get("body"))
    comment_id = response.get("id")
    if comment_id in (None, "") and edit_id:
        comment_id = edit_id
    url = _untrusted.flat(str(response.get("html_url") or ""))

    print()
    print("## What landed")
    print(f"  {landed_msg}")
    print(f"  disclosure: {disclosure_state}")
    print(f"  URL: {url or '(not returned by gh)'}")
    print()
    print(result_line(number, landed_state, comment_id, bool(edit_id)))
    return 0 if landed_state in (LANDED_EXACT, LANDED_NORMALISED) else 1


if __name__ == "__main__":
    sys.exit(main())
