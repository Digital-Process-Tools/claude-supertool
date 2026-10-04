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
import _checks  
import _remote_default as _rd  
import _repo_target  
import _payload_keys  
import _untrusted  
import _digits  
import _publish_safety  


REF_OK = "ok"
REF_DROPPED = "dropped"
REF_UNKNOWN = "unknown"


LANDED_EXACT = "exact"
LANDED_NORMALISED = "normalised"
LANDED_MISMATCH = "mismatch"
LANDED_UNKNOWN = "unknown"




TITLE_NOT_SENT = "not sent"
TITLE_EXACT = "exact"
TITLE_MISMATCH = "mismatch"

UNLINK = "unlink"



NOT_APPLIED = ("base", "head", "draft", "labels", "assignees", "reviewers",
               "milestone")








ACCEPTED_KEYS = set(NOT_APPLIED) | {
    "repo", "title", "body", "body_file", "literal_backslashes", "no_close",
}
ALIASES: dict = {}

CRLF = chr(13) + chr(10)
CR = chr(13)
LF = chr(10)






def parse_args(argv: List[str]) -> Tuple[str, str, bool, str]:





    tokens = [t for t in argv if t != ""]
    if not tokens:
        return ("", "", False, (
            "ERROR: gh-pr-edit needs a PR number and a payload — "
            "gh-pr-edit:NUMBER:@FILE, or @- for stdin (the same JSON/TOML "
            "gh-pr-create takes)."))

    number = tokens[0]
    if not _digits.is_ascii_int(number):
        return ("", "", False, (
            f"ERROR: {number!r} is not a PR number. gh-pr-edit takes the "
            f"number of a pull request that already exists — "
            f"gh-pr-edit:NUMBER:@FILE, or gh-pr-edit:NUMBER:@- for stdin."))

    rest = tokens[1:]
    unlink = False
    if rest and rest[-1] == UNLINK:
        unlink = True
        rest = rest[:-1]

    if not rest:
        return ("", "", False, (
            "ERROR: gh-pr-edit needs a payload file — gh-pr-edit:NUMBER:@FILE, "
            "or @- to read it from stdin."))





    if len(rest) > 1 and len(rest[0]) == 2 and rest[0][1:].isalpha():
        rest = [":".join(rest)]
    if len(rest) > 1:
        return ("", "", False, (
            f"ERROR: gh-pr-edit does not take {rest[1]!r}. The only token past "
            f"the payload is `{UNLINK}`, which permits an update that drops a "
            f"closing reference the published body carried. Nothing was "
            f"written."))

    return (number, rest[0], unlink, "")






def validate(payload: dict) -> str | None:

    if not payload.get("repo"):
        return ("ERROR: payload missing required field: repo — and it could not "
                "be resolved from the origin remote either.")
    if "body" in payload and payload.get("body_file"):
        return "ERROR: payload has both body and body_file — use one"
    if "body" not in payload and not payload.get("body_file"):
        return ("ERROR: payload has neither body nor body_file, so this op has "
                'nothing to publish. An absent body is not an empty one — set '
                'body = "" if clearing the pull request body is what you mean.')
    return None


def ignored_fields(payload: dict) -> List[str]:

    return [key for key in NOT_APPLIED
            if payload.get(key) not in (None, "", [], {})]


def title_change(payload: dict, published: str) -> Tuple[dict, List[str]]:










    if "title" not in payload:
        return ({}, ["  title: not touched — the payload carries no title field"])
    new = str(payload.get("title") or "")
    if new == published:
        return ({}, [f"  title: unchanged ({_untrusted.flat(new)})"])
    return ({"title": new}, [
        f"  title: {_untrusted.flat(published)!r}",
        f"      -> {_untrusted.flat(new)!r}",
    ])






def closing_ref_verdict(old_body: object, new_body: str,
                        old_read_error: str) -> Tuple[str, List[str], str]:













    new_refs = _checks.closing_issue_refs(new_body)

    if old_body is None:
        return (REF_UNKNOWN, [], (
            f"the published body could not be read "
            f"({old_read_error or 'no detail'}), so whether this update drops a "
            f"closing reference is UNKNOWN. That is not 'nothing was dropped'. "
            f"Re-run, or add `{UNLINK}` to write anyway and take the risk "
            f"deliberately."))

    old_refs = _checks.closing_issue_refs(old_body)
    lost = [ref for ref in old_refs if ref not in new_refs]
    if not lost:














        carried = [ref for ref in new_refs if ref in old_refs]
        added = [ref for ref in new_refs if ref not in old_refs]
        if carried and added:
            return (REF_OK, [], (f"carried through: {', '.join(carried)}; "
                                 f"added: {', '.join(added)}"))
        if carried:
            return (REF_OK, [], f"carried through: {', '.join(carried)}")
        if added:
            return (REF_OK, [], (f"added: {', '.join(added)} — the published "
                                 f"body linked none, this one does"))
        return (REF_OK, [],
                "the published body linked no issue, and neither does this one")

    them = "it" if len(lost) == 1 else "them"
    return (REF_DROPPED, lost, (
        f"this update DROPS {', '.join(lost)} — the published body closes "
        f"{them} and the new one does not, so merging would no longer close "
        f"{them}. Nothing was written. If the pull request really has been "
        f"re-scoped, say so: append `{UNLINK}`."))


def may_write(ref_state: str, unlink: bool) -> bool:

    return ref_state == REF_OK or unlink






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
            "the PATCH response carried no body field, so what the server now "
            "holds is UNKNOWN. The write was accepted; whether these bytes are "
            "the ones on it was not established. Read it: `gh-pr:NUMBER:full`."))
    if stored == sent:
        return (LANDED_EXACT,
                f"byte-identical to what was sent ({len(sent)} characters)")
    if _newlines_only(stored) == _newlines_only(sent):
        return (LANDED_NORMALISED, (
            f"identical apart from line endings, which the server normalised "
            f"({len(sent)} characters sent, {len(stored)} stored). Nothing was "
            f"lost."))
    return (LANDED_MISMATCH, (
        f"NOT what was sent: {len(sent)} characters sent, {len(stored)} "
        f"stored. {_first_difference(sent, stored)}"))


def title_verdict(sent_title: object, stored_title: object) -> Tuple[str, str]:






    if sent_title is None:
        return (TITLE_NOT_SENT, "")
    if isinstance(stored_title, str) and stored_title == sent_title:
        return (TITLE_EXACT, "  title verified on the server")
    return (TITLE_MISMATCH, (
        f"  title on the server is NOT what was sent: "
        f"{_untrusted.flat(str(stored_title))!r}"))


def result_line(number: str, ref_state: str, landed_state: str,
                note: str, title_state: str = TITLE_NOT_SENT) -> str:

    if landed_state == LANDED_EXACT:
        landed = "body verified byte-identical on the server"
    elif landed_state == LANDED_NORMALISED:
        landed = "body verified, line endings normalised by the server"
    elif landed_state == LANDED_MISMATCH:
        landed = "body on the server is NOT what was sent — UNVERIFIED"
    else:
        landed = "what the server holds was not read back — UNVERIFIED"
    tail = ""
    if ref_state == REF_DROPPED:
        tail = f" — closing reference dropped, {UNLINK} given"
    elif ref_state == REF_UNKNOWN:
        tail = f" — closing-reference check UNVERIFIED, {UNLINK} given"
    elif note:
        tail = f" — {note}"
    if title_state == TITLE_MISMATCH:
        landed += "; the TITLE on the server is not what was sent"
    elif title_state == TITLE_EXACT:
        landed += "; title verified"
    return f"[result] PR #{number} body updated; {landed}{tail}"


def refusal_line(number: str, ref_state: str, lost: List[str]) -> str:

    if ref_state == REF_DROPPED:
        why = f"closing reference {', '.join(lost)} would be dropped"
    else:
        why = "the closing-reference check could not run"
    return f"[result] PR #{number} NOT updated; {why} — nothing was written"






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
        return (str(payload.get("body") or ""), "")
    if Path(body_file).is_dir():
        return ("", f"ERROR: body_file is a directory, not a file: {body_file}")
    try:
        return (Path(body_file).read_text(encoding="utf-8"), "")
    except FileNotFoundError:
        return ("", f"ERROR: body_file not found: {body_file}")
    except PermissionError as e:
        return ("",
                f"ERROR: permission denied reading body_file: {body_file} — {e}")






def main() -> int:  
    use_utf8_stdout()
    number, raw_arg, unlink, err = parse_args(sys.argv[1:])
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
        print("ERROR: payload is not a table of fields (expected JSON or TOML "
              "with body or body_file)")
        return 1

    key_err = _payload_keys.check(payload, ACCEPTED_KEYS, ALIASES, "gh-pr-edit")
    if key_err:
        print(key_err)
        return 1

    repo_conflict, repo_source = _repo_target.resolve_or_conflict(payload, "gh-pr-edit")
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

    repo = str(payload["repo"])
    endpoint = f"repos/{repo}/pulls/{number}"


    current, read_err = _gh_json(["api", endpoint], timeout=30)
    old_body: object = None
    old_title = ""
    state_line = "state: UNKNOWN — the published pull request could not be read"
    if isinstance(current, dict):
        old_title = str(current.get("title") or "")
        published_state = "merged" if current.get("merged") else str(
            current.get("state") or "?")
        state_line = f"state: {_untrusted.flat(published_state)}"






        if "body" in current:
            old_body = current.get("body") or ""
        else:
            read_err = ("the pull request came back without a body field, so "
                        "there is nothing to compare against")
    elif not read_err:
        read_err = "gh returned no pull request object"

    repo_note = (f"  (repo from {repo_source})"
                 if repo_source not in ("", "payload") else "")
    print(f"# gh-pr-edit — {repo}#{number}{repo_note}")
    print(f"  {state_line}")


    ref_state, lost, ref_msg = closing_ref_verdict(old_body, content, read_err)
    print()
    print("## Closing references")
    print(f"  {ref_msg}")
    if ref_state == REF_OK:
        print(f"  {_checks.linked_issue_line(_checks.closing_issue_refs(content))}")

    if not may_write(ref_state, unlink):
        print()
        print(refusal_line(number, ref_state, lost))
        return 1







    content, disclosure_state = _publish_safety.apply_forge_disclosure(content)


    ignored = ignored_fields(payload)
    title_fields, title_lines = title_change(payload, old_title)


    fields = {"body": content}
    fields.update(title_fields)
    response, write_err = _gh_json(
        ["api", "-X", "PATCH", "-H", "Accept: application/vnd.github+json",
         endpoint, "--input", "-"],
        stdin=json.dumps(fields), timeout=60)

    if not isinstance(response, dict):
        print()
        print(f"ERROR: the update was refused ({write_err or 'no detail'})")
        print(f"[result] PR #{number} NOT updated; nothing was written")
        return 1

    landed_state, landed_msg = landed_verdict(content, response.get("body"))

    print()
    print("## What landed")
    print(f"  {landed_msg}")
    print(f"  disclosure: {disclosure_state}")
    for line in title_lines:
        print(line)
    title_state, title_msg = title_verdict(
        title_fields.get("title"), response.get("title"))
    if title_msg:
        print(title_msg)
    url = _untrusted.flat(str(response.get("html_url") or ""))
    print(f"  URL: {url or '(not returned by gh)'}")

    if ignored:
        print()
        print("## Not applied")
        print(f"  {', '.join(ignored)} — this op updates the body and the "
              f"title, nothing else. These fields were read and NOT sent.")

    print()
    print("## Next")
    print(f"  Read it back: `gh-pr:{number}:full`")
    print(f"  Merge:        `gh-pr-merge:{number}` (previews the gate)")
    print()
    print(result_line(number, ref_state, landed_state, "", title_state))
    return 0 if (landed_state in (LANDED_EXACT, LANDED_NORMALISED)
                 and title_state != TITLE_MISMATCH) else 1


if __name__ == "__main__":
    sys.exit(main())
