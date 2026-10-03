#!/usr/bin/env python3

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse
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




ACCEPTED_KEYS = {
    "project", "title", "description", "description_file", "milestone_id",
    "labels", "assignee_ids", "estimate", "links",
}






ALIASES = {
    "body": "description",
    "body_file": "description_file",
}


def _glab(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["glab"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _glab_api(method: str, endpoint: str, *extra: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["glab", "api", "--method", method, endpoint] + list(extra),
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


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
    if not payload.get("project"):
        return "ERROR: payload missing required field: project"
    if not payload.get("title"):
        return "ERROR: payload missing required field: title"
    if payload.get("description") and payload.get("description_file"):
        return "ERROR: payload has both description and description_file — use one"
    return None


def main() -> int:
    use_utf8_stdout()
    raw_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    path = raw_arg[1:] if raw_arg.startswith("@") else raw_arg
    if not path:




        print(
            "ERROR: gl-issue-create needs a payload — "
            "gl-issue-create:@FILE, or gl-issue-create:@- to read it from "
            "stdin (JSON or TOML with title/description)."
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
        print(f"ERROR: failed to parse payload: {e} (expected JSON or TOML with title/description)")
        return 1

    key_err = _payload_keys.check(payload, ACCEPTED_KEYS, ALIASES, "gl-issue-create")
    if key_err:
        print(key_err)
        return 1
    payload, alias_err = _payload_keys.resolve_aliases(payload, ALIASES)
    if alias_err:
        print(alias_err)
        return 1

    repo_conflict, repo_source = _repo_target.resolve_or_conflict(payload, "gl-issue-create", "project")
    if repo_conflict:
        print(repo_conflict)
        return 1
    if not payload.get("project"):
        auto = _rd.resolve("gitlab_project", "gitlab")
        if auto:
            payload["project"] = auto
            repo_source = "cwd remote / config default"

    err = _validate(payload)
    if err:
        print(err)
        return 1

    labels_err = _validate_labels(payload)
    if labels_err:
        print(labels_err)
        return 1

    project = payload["project"]
    title = payload["title"]
    description_file = payload.get("description_file")
    description = payload.get("description", "")
    milestone_id = payload.get("milestone_id")
    labels: list[str] = payload.get("labels") or []
    assignee_ids: list[int] = payload.get("assignee_ids") or []
    estimate: str = payload.get("estimate", "")
    links: list[dict] = payload.get("links") or []

    if description_file:





        if Path(description_file).is_dir():
            print(f"ERROR: description_file is a directory, not a file: {description_file}")
            return 1
        try:
            body = Path(description_file).read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"ERROR: description_file not found: {description_file}")
            return 1
        except IsADirectoryError:


            print(f"ERROR: description_file is a directory, not a file: {description_file}")
            return 1
        except PermissionError as e:



            print(f"ERROR: permission denied reading description_file: {description_file} — {e}")
            return 1
    else:
        body = description




    body, disclosure_state = _publish_safety.apply_forge_disclosure(body)

    if estimate:
        if not re.match(r"^\d+(\.\d+)?[mhdw]\Z", estimate):  
            print(f"ERROR: invalid estimate format: {estimate!r} (expected e.g. '4h', '30m', '2d')")
            return 1
        body = body.rstrip() + f"\n\n/estimate {estimate}"

    cmd = [
        "issue", "create",
        "--repo", project,
        "--title", title,
        "--description", body,
    ]
    for label in labels:
        cmd += ["--label", label]
    if milestone_id is not None:
        cmd += ["--milestone", str(milestone_id)]
    if assignee_ids:
        cmd += ["--assignee", ",".join(str(i) for i in assignee_ids)]

    try:
        result = _glab(cmd, timeout=30)
    except FileNotFoundError:
        print("ERROR: glab not found — install the GitLab CLI")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out")
        return 1

    if result.returncode != 0:
        print(f"ERROR: glab issue create failed (exit {result.returncode})")


        print(_untrusted.flat(result.stderr.strip() or result.stdout.strip()))
        return 1

    output = result.stdout.strip()
    url = ""
    iid = ""















    for line in _untrusted.split_lines(output):
        line = _untrusted.flat(line.strip())
        if "/-/issues/" in line or "/issues/" in line:
            url = line
            parts = line.rstrip("/").split("/")
            if parts:
                iid = parts[-1]
            break

    if not url:
        printed = _untrusted.split_lines(output)
        url = _untrusted.flat(printed[-1]) if printed else "?"
        parts = url.rstrip("/").split("/")
        iid = parts[-1] if parts else "?"

    if links and iid and iid != "?" and not iid.isdigit():
        print(f"gl-issue-create OK iid={iid} url={url}  (links skipped — could not extract numeric iid)", file=sys.stderr)
    elif links and iid and iid != "?":






        encoded_project = urllib.parse.quote(project, safe="")
        for link in links:
            target_iid = link.get("target_iid")
            link_type = link.get("type", "relates_to")
            if target_iid is None:
                continue
            _glab_api(
                "POST",
                f"projects/{encoded_project}/issues/{iid}/links",
                f"--field=target_project_id={encoded_project}",
                f"--field=target_issue_iid={target_iid}",
                f"--field=link_type={link_type}",
            )

    source_note = (f"  (project from {repo_source})"
                    if repo_source not in ("", "payload") else "")
    print(f"gl-issue-create OK iid={iid} url={url} "
          f"disclosure={disclosure_state}{source_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
