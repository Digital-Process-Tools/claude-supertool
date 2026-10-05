#!/usr/bin/env python3






















































from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

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
import _publish_safety  



TRANSPORT_GRAPHQL = "graphql"
TRANSPORT_REST_FALLBACK = "rest (fallback -- mutation transport unavailable)"




ACCEPTED_KEYS = {
    "repo", "title", "body", "body_file", "labels", "assignees", "milestone",
    "dry_run",
}






ALIASES = {
    "description": "body",
    "description_file": "body_file",
}


def _gh(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _gh_json(args: list[str], stdin: str | None = None,
             timeout: int = 30) -> tuple[object, str]:








    try:
        result = subprocess.run(["gh"] + args, capture_output=True, text=True,
                                 input=stdin, timeout=timeout, encoding="utf-8",
                                 errors="replace")
    except FileNotFoundError:
        return (None, "gh not found -- install the GitHub CLI")
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


def _is_graphql_transport_failure(text: str) -> bool:







    low = text.lower()
    if "no server is currently available" in low:
        return True
    if "503" in text and "graphql" in low:
        return True
    return False


_DEDUP_PER_PAGE = 100




_DEDUP_MAX_PAGES = 50


def _find_open_issue_by_title(repo: str, title: str,
                               timeout: int = 30) -> tuple[dict | None, str]:















    for page in range(1, _DEDUP_MAX_PAGES + 1):









        data, err = _gh_json(["api", "-X", "GET", f"repos/{repo}/issues",
                              "-f", "state=open",
                              "-f", f"per_page={_DEDUP_PER_PAGE}",
                              "-f", f"page={page}"],
                             timeout=timeout)
        if err:
            return (None, err)
        if not isinstance(data, list):
            return (None, f"unexpected response shape from repos/{repo}/issues")
        for item in data:
            if (isinstance(item, dict) and item.get("title") == title
                    and "pull_request" not in item):
                return (item, "")
        if len(data) < _DEDUP_PER_PAGE:
            return (None, "")
    return (None, f"more than {_DEDUP_MAX_PAGES * _DEDUP_PER_PAGE} open "
                  f"issues -- could not page through all of them via REST "
                  f"to confirm no duplicate exists")


def _resolve_milestone_number(repo: str, name: str,
                               timeout: int = 30) -> tuple[int | None, str]:







    data, err = _gh_json(["api", "-X", "GET", f"repos/{repo}/milestones",
                          "-f", "state=all", "-f", "per_page=100"],
                         timeout=timeout)
    if err:
        return (None, f"could not list milestones via REST ({err})")
    if not isinstance(data, list):
        return (None, f"unexpected response shape from repos/{repo}/milestones")
    for item in data:
        if isinstance(item, dict) and item.get("title") == name:
            number = item.get("number")
            if isinstance(number, int):
                return (number, "")
    return (None, f"no open or closed milestone named {name!r} found via REST "
                  f"lookup (checked {len(data)})")


def _load_payload(path: str) -> dict:
    if path.startswith("@"):
        path = path[1:]
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")

    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    if tomllib is None:
        raise ValueError("TOML payload requires Python 3.11+ or `pip install tomli`")
    return tomllib.loads(raw)


def _validate_labels(payload: dict) -> str | None:
















    if "labels" not in payload:
        return None
    labels = payload["labels"]
    if not isinstance(labels, list):
        return (
            f"ERROR: labels must be an array of label names, got "
            f"{type(labels).__name__} {labels!r}"
        )
    for label in labels:
        if not isinstance(label, str) or not label:
            return (
                f"ERROR: labels must be an array of label names, got a "
                f"{type(label).__name__} element {label!r} in labels"
            )
    return None


def _validate(payload: dict) -> str | None:
    if not payload.get("repo"):
        return "ERROR: payload missing required field: repo"
    if not payload.get("title"):
        return "ERROR: payload missing required field: title"
    if payload.get("body") and payload.get("body_file"):
        return "ERROR: payload has both body and body_file — use one"









    if "body" in payload and not isinstance(payload.get("body"), str):
        return (
            "ERROR: body must be a string, not "
            f"{type(payload['body']).__name__} -- gh-issue-create does "
            "not accept a TOML [[body]] table-array. Pass body as a "
            "single string, or use body_file to read one from a file."
        )



    if "dry_run" in payload and not isinstance(payload.get("dry_run"), bool):
        return (
            "ERROR: dry_run must be a boolean (true/false), got "
            f"{type(payload['dry_run']).__name__} {payload['dry_run']!r}"
        )
    return None


def main() -> int:
    use_utf8_stdout()
    raw_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    path = raw_arg[1:] if raw_arg.startswith("@") else raw_arg
    if not path:




        print(
            "ERROR: gh-issue-create needs a payload — "
            "gh-issue-create:@FILE, or gh-issue-create:@- to read it from "
            "stdin (JSON or TOML with title/body)."
        )
        return 1








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
        print(f"ERROR: failed to parse payload: {e} (expected JSON or TOML with title/body)")
        return 1

    key_err = _payload_keys.check(payload, ACCEPTED_KEYS, ALIASES, "gh-issue-create")
    if key_err:
        print(key_err)
        return 1
    payload, alias_err = _payload_keys.resolve_aliases(payload, ALIASES)
    if alias_err:
        print(alias_err)
        return 1

    repo_conflict, repo_source = _repo_target.resolve_or_conflict(payload, "gh-issue-create")
    if repo_conflict:
        print(repo_conflict)
        return 1
    if not payload.get("repo"):
        auto = _rd.resolve("github_repo", "github.com")
        if auto:
            payload["repo"] = auto
            repo_source = "cwd remote / config default"

    err = _validate(payload)
    if err:
        print(err)
        return 1

    labels_err = _validate_labels(payload)
    if labels_err:
        print(labels_err)
        return 1

    repo = payload["repo"]
    title = payload["title"]
    body_file = payload.get("body_file")
    body = payload.get("body", "")
    labels: list[str] = payload.get("labels") or []
    assignees: list[str] = payload.get("assignees") or []
    milestone: str = payload.get("milestone", "")

    if body_file:





        if Path(body_file).is_dir():
            print(f"ERROR: body_file is a directory, not a file: {body_file}")
            return 1
        try:
            content = Path(body_file).read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"ERROR: body_file not found: {body_file}")
            return 1
        except IsADirectoryError:


            print(f"ERROR: body_file is a directory, not a file: {body_file}")
            return 1
        except PermissionError as e:



            print(f"ERROR: permission denied reading body_file: {body_file} — {e}")
            return 1
    else:
        content = body



    content, disclosure_state = _publish_safety.apply_forge_disclosure(content)

    if payload.get("dry_run"):







        lines = [
            f"gh-issue-create DRY-RUN repo={repo} title={title!r}",
            f"  labels: {', '.join(labels) if labels else '(none)'}",
            f"  assignees: {', '.join(assignees) if assignees else '(none)'}",
            f"  milestone: {milestone or '(none)'}",
            f"  disclosure: {disclosure_state}",
            "  body:",
        ]
        for body_line in (content.splitlines() or [""]):
            lines.append(f"    {body_line}")
        lines.append(
            "  (no gh call made -- remove dry_run, or set it to false, to "
            "actually create the issue)"
        )
        print("\n".join(lines))
        return 0

    tmp_body: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmp_body = f.name

        cmd = [
            "issue", "create",
            "--repo", repo,
            "--title", title,
            "--body-file", tmp_body,
        ]
        if labels:
            cmd += ["--label", ",".join(labels)]
        if assignees:
            cmd += ["--assignee", ",".join(assignees)]
        if milestone:
            cmd += ["--milestone", milestone]

        try:
            result = _gh(cmd, timeout=30)
        except FileNotFoundError:
            print("ERROR: gh not found — install the GitHub CLI")
            return 1
        except subprocess.TimeoutExpired:
            print("ERROR: gh timed out")
            return 1

        if result.returncode != 0:
            combined = (result.stderr or "") + (result.stdout or "")
            if not _is_graphql_transport_failure(combined):
                print(f"ERROR: gh issue create failed (exit {result.returncode})")




                print(_untrusted.flat(result.stderr.strip() or result.stdout.strip()))
                return 1


            detail = _untrusted.split_lines(combined.strip())
            print(f"NOTE: gh issue create (GraphQL) failed with a "
                  f"transport-shaped error — "
                  f"{_untrusted.flat(detail[-1]) if detail else 'no detail'}. "
                  f"Falling back to REST (#1790).")

            existing, dedup_err = _find_open_issue_by_title(repo, title, timeout=30)
            if dedup_err:
                print(f"ERROR: could not check for an existing open issue "
                      f"before writing ({dedup_err}) — refusing to write "
                      f"blind, since a duplicate issue on a tracker is "
                      f"expensive to unpick. Nothing was written. Re-run, "
                      f"or check by hand whether the earlier mutation landed.")
                return 1
            if existing is not None:
                number = str(existing.get("number") or "?")
                url = _untrusted.flat(str(existing.get("html_url") or ""))
                print(f"gh-issue-create OK number={number} url={url} "
                      f"transport={TRANSPORT_REST_FALLBACK} "
                      f"note=an open issue with this exact title already "
                      f"exists — reusing it rather than filing a duplicate "
                      f"(the earlier GraphQL attempt may have landed despite "
                      f"the transport error)")
                return 0

            milestone_number = None
            milestone_note = ""
            if milestone:
                milestone_number, milestone_note = _resolve_milestone_number(
                    repo, milestone, timeout=30)

            fields: dict = {"title": title, "body": content}
            if labels:
                fields["labels"] = labels
            if assignees:
                fields["assignees"] = assignees
            if milestone_number is not None:
                fields["milestone"] = milestone_number

            response, write_err = _gh_json(
                ["api", "-X", "POST", "-H", "Accept: application/vnd.github+json",
                 f"repos/{repo}/issues", "--input", "-"],
                stdin=json.dumps(fields), timeout=30)
            if not isinstance(response, dict):
                print(f"ERROR: the REST fallback also failed "
                      f"({write_err or 'no detail'}) — nothing was written "
                      f"(the mutation transport was down and the REST write "
                      f"failed too)")
                return 1

            number = str(response.get("number") or "?")
            url = _untrusted.flat(str(response.get("html_url") or ""))
            notes = []
            if milestone and milestone_number is None:
                notes.append(f"milestone {milestone!r} NOT APPLIED — {milestone_note}")
            note_str = f" note={'; '.join(notes)}" if notes else ""
            source_note = (f"  (repo from {repo_source})"
                            if repo_source not in ("", "payload") else "")
            print(f"gh-issue-create OK number={number} url={url} "
                  f"transport={TRANSPORT_REST_FALLBACK} "
                  f"disclosure={disclosure_state}{note_str}{source_note}")
            return 0

        match = re.search(r"https?://github\.com/[^/\s]+/[^/\s]+/issues/(\d+)", result.stdout)
        if match:
            url = match.group(0)
            number = match.group(1)
        else:






            printed = _untrusted.split_lines(result.stdout.strip())
            url = _untrusted.flat(printed[-1]) if printed else "?"
            number = url.rstrip("/").split("/")[-1] if "/" in url else "?"

        source_note = (f"  (repo from {repo_source})"
                        if repo_source not in ("", "payload") else "")
        print(f"gh-issue-create OK number={number} url={url} "
              f"transport={TRANSPORT_GRAPHQL} "
              f"disclosure={disclosure_state}{source_note}")
        return 0

    finally:
        if tmp_body and os.path.exists(tmp_body):
            os.unlink(tmp_body)


if __name__ == "__main__":
    sys.exit(main())
