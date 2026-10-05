#!/usr/bin/env python3




















































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
        import tomli as tomllib  
    except ModuleNotFoundError:
        tomllib = None  

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _console import use_utf8_stdout  
import _remote_default as _rd  
import _repo_target  
import _payload_keys  
import _untrusted  
import _digits  
import _publish_safety  



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






def parse_args(argv: List[str]) -> Tuple[str, str, str, str]:









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





    if len(rest) > 1 and len(rest[0]) == 2 and rest[0][1:].isalpha():
        rest = [":".join(rest)]
    if len(rest) > 1:
        return ("", "", "", (
            f"ERROR: gh-issue-comment does not take {rest[1]!r}. The only "
            f"token past the payload is edit=COMMENT_ID. Nothing was "
            f"written."))

    return (number, rest[0], edit_id, "")






def validate(payload: dict) -> str | None:

    if not payload.get("repo"):
        return ("ERROR: payload missing required field: repo -- and it could "
                "not be resolved from the origin remote either.")
    if "body" in payload and payload.get("body_file"):
        return "ERROR: payload has both body and body_file — use one"
    if "body" not in payload and not payload.get("body_file"):
        return ("ERROR: payload has neither body nor body_file, so this op "
                "has nothing to publish. An absent body is not an empty "
                'one -- set body = "" if that is really what you mean.')









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








    if not isinstance(check_response, dict):
        return (f"could not verify the comment belongs to issue #{number} "
                f"before editing it ({check_err or 'no detail'})")



    issue_url = _untrusted.flat(str(check_response.get("issue_url") or ""))
    if not issue_url.endswith(f"/issues/{number}"):
        return (f"that comment belongs to "
                f"{issue_url or '(no issue_url in the response)'}, not "
                f"issue #{number} — refusing to edit it")
    return ""






def _gh_json(args: List[str], stdin: str | None = None,
             timeout: int = 30) -> Tuple[object, str]:
    try:
        result = subprocess.run(["gh"] + args, capture_output=True, text=True,
                                input=stdin, timeout=timeout, encoding="utf-8",
                                errors="replace")
    except FileNotFoundError:
        return (None, "gh not found — install the GitHub CLI")
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
