#!/usr/bin/env python3
"""Create a GitLab issue from a JSON/TOML payload file."""
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
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
import _remote_default as _rd  # noqa: E402
import _repo_target  # noqa: E402  (repo: op precedence over payload's own field -- #1909)
import _payload_keys  # noqa: E402  (unrecognised-key refusal, shared with the other three @payload ops -- #2123)
import _untrusted  # noqa: E402  (glab relays the API's own error body — #1485)
import _publish_safety  # noqa: E402  (#2100 -- the forge-write disclosure marker)

# Every key this op reads from a payload. Checked against the payload before
# anything is created (#2123) -- a key outside this set is refused rather
# than silently dropped.
ACCEPTED_KEYS = {
    "project", "title", "description", "description_file", "milestone_id",
    "labels", "assignee_ids", "estimate", "links",
}

# `body`/`body_file` -- what `gh-issue-create` and the GitHub API itself
# call the field -- silently created an issue with no description here on
# 2026-09-01 (#2123): this op wants `description`/`description_file`.
# Accepted as an alias rather than only documented, since a misfiled issue
# from that slip is expensive to unpick on a live tracker.
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
    """`labels`, if present, must be a list of non-empty strings.

    A comma-separated string (`labels = "A,B,C"`) is iterated character by
    character by `for label in labels: cmd += ["--label", label]`, sending
    one `--label` flag per *character* to the CLI -- which silently created
    30 junk labels on a sibling tracker before anyone noticed (#2173). This
    refuses before any glab/gh call rather than after, since a wrong label
    write is expensive to unpick.
    """
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
        # supertool's {arg} substitution never omits the placeholder — a
        # missing @FILE arrives here as an empty string, not a missing
        # argv slot, so this must be checked explicitly rather than relying
        # on len(sys.argv). See #620.
        print(
            "ERROR: gl-issue-create needs a payload — "
            "gl-issue-create:@FILE, or gl-issue-create:@- to read it from "
            "stdin (JSON or TOML with title/description)."
        )
        return 1

    # Decide *before* reading rather than catching whatever the OS raises:
    # opening a directory for read raises IsADirectoryError on POSIX but
    # PermissionError on Windows (CreateFileW succeeds, the subsequent read
    # fails with ERROR_ACCESS_DENIED). Catching only IsADirectoryError left
    # #620's traceback alive on Windows — an is_dir() check gives the same
    # message on every platform without depending on which errno the OS
    # happens to pick. See #627.
    if path != "-" and Path(path).is_dir():
        print(f"ERROR: payload path is a directory, not a file: {path}")
        return 1

    try:
        payload = _load_payload(raw_arg)
    except FileNotFoundError:
        print(f"ERROR: payload file not found: {path}")
        return 1
    except IsADirectoryError:
        # Belt-and-suspenders for the TOCTOU window between the is_dir()
        # check above and this read (path became a directory in between).
        print(f"ERROR: payload path is a directory, not a file: {path}")
        return 1
    except PermissionError as e:
        # Deliberately a distinct message from "is a directory": a locked
        # or wrong-ownership file also raises PermissionError, and on
        # Windows it's the *only* thing a directory read raises. Reporting
        # it as "is a directory" would be a confidently wrong disclosure,
        # not just an unhelpful one.
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
        # Same is_dir()-before-read shape as the payload guard above: a
        # directory read raises IsADirectoryError on POSIX but
        # PermissionError on Windows, so the directory verdict must come
        # from is_dir(), not from catching whichever OSError subtype the
        # platform happens to raise. See #620/#627/#630.
        if Path(description_file).is_dir():
            print(f"ERROR: description_file is a directory, not a file: {description_file}")
            return 1
        try:
            body = Path(description_file).read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"ERROR: description_file not found: {description_file}")
            return 1
        except IsADirectoryError:
            # Belt-and-suspenders for the TOCTOU window between the
            # is_dir() check above and this read.
            print(f"ERROR: description_file is a directory, not a file: {description_file}")
            return 1
        except PermissionError as e:
            # Deliberately distinct from "is a directory": a locked or
            # wrong-ownership file also raises PermissionError, and it's
            # the only thing a directory read raises on Windows.
            print(f"ERROR: permission denied reading description_file: {description_file} — {e}")
            return 1
    else:
        body = description

    # #2100: applied before the `/estimate` quick action below, not after --
    # a GitLab quick action needs to be its own trailing paragraph, and
    # appending prose past it risks the API no longer recognising it.
    body, disclosure_state = _publish_safety.apply_forge_disclosure(body)

    if estimate:
        if not re.match(r"^\d+(\.\d+)?[mhdw]\Z", estimate):  # \Z, not $ — #1188
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
        print("ERROR: glab not found — install from https://gitlab.com/gitlab-org/cli")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out")
        return 1

    if result.returncode != 0:
        print(f"ERROR: glab issue create failed (exit {result.returncode})")
        # Whatever glab echoed here was written by the GitLab API, and it prints
        # at column 0 with nothing in front of it — flatten, never relay (#1485).
        print(_untrusted.flat(result.stderr.strip() or result.stdout.strip()))
        return 1

    output = result.stdout.strip()
    url = ""
    iid = ""
    # The direct twin of `github/issue_create.py`, narrowed by #1648 and left
    # here until #1654. Both arms put their value into the `gl-issue-create OK
    # iid=... url=...` receipt at column 0, so both need the same two calls.
    #
    # `split_lines` decides the boundary: the fallback takes `[-1]`, and
    # `str.splitlines()` cut on U+2028, so whoever wrote glab's stdout chose
    # which segment became the whole `url=` and everything before it was
    # dropped out of the receipt rather than disclosed.
    #
    # `flat` is not optional once that split is narrowed. Unlike the GitHub
    # twin, whose matched arm is a regex whose character classes reject U+2028,
    # this arm is a substring test and assigns the whole line - so narrowing
    # alone would put a live separator into a line this tool owns. `iid` is
    # derived from the flattened value on purpose: a forged tail then fails
    # `isdigit()` and the links block declines, saying so.
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
        # #1993: a project carrying `?`, `#` or `%` reached this path with
        # only its slashes escaped, so it spliced into the URL rather than
        # naming a project. quote(..., safe="") is the same path-segment
        # encoding `_repo_target.gl_project()` already uses for the primary
        # `projects/:id` substitution -- one project string, one encoding,
        # not two.
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
