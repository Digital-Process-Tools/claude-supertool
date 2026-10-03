

















from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _st_hint  

ENV_VAR = "SUPERTOOL_REPO"








FROM_OP_ENV_VAR = "SUPERTOOL_REPO_FROM_OP"

GITHUB_HOST = "github.com"


def target(explicit: bool = False) -> str | None:

































    if explicit:
        return explicit_target()
    value = (os.environ.get(ENV_VAR) or "").strip()
    return value or None


def from_op() -> bool:








    return os.environ.get(FROM_OP_ENV_VAR) == "1"


def explicit_target() -> str | None:












    return target() if from_op() else None


def owner_repo(explicit: bool = False) -> tuple[str, str] | None:




    value = target(explicit)
    if not value or value.count("/") != 1:
        return None
    owner, name = value.split("/", 1)
    if not owner or not name:
        return None
    return owner, name


def gh_args(explicit: bool = False) -> list[str]:






    value = target(explicit)
    return ["--repo", value] if value else []


def api_path(suffix: str, explicit: bool = False) -> str:








    pair = owner_repo(explicit)
    if pair is None:
        return f"repos/{{owner}}/{{repo}}/{suffix}"
    owner, name = pair
    return f"repos/{owner}/{name}/{suffix}"





_SLUG = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def api_path_for_display(suffix: str, slug: str, explicit: bool = False) -> str:





















    if owner_repo(explicit) is not None or not _SLUG.fullmatch(slug or ""):
        return api_path(suffix, explicit)
    return f"repos/{slug}/{suffix}"


def cwd_slug(timeout: int = 15) -> str:























    if target():

        return ""
    try:
        r = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if r.returncode != 0:
        return ""
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("nameWithOwner") or "")


def effective_slug(timeout: int = 15) -> str:





























    return target() or cwd_slug(timeout)


def api_path_printable(suffix: str, timeout: int = 15) -> str:












    if owner_repo() is not None:
        return api_path(suffix)
    return api_path_for_display(suffix, cwd_slug(timeout))




ABSENT = "absent"



UNKNOWN = "unknown"




































_ABSENT_CWD = (
    "not a git repository",
    "no git remotes",
    "git remotes found",
    "github host",
    "could not determine base repository",
)
_ABSENT_TARGET = (
    "could not resolve to a repository",
    "http 404",
)


_DETAIL_CAP = 200


def _one_line(text: str) -> str:


















    first = ""
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    cleaned = "".join(ch if ch.isprintable() else " " for ch in first).strip()
    if len(cleaned) > _DETAIL_CAP:
        cleaned = cleaned[:_DETAIL_CAP - 1].rstrip() + "…"
    return cleaned


def classify_detail(detail: str, slug: str | None = None) -> str:






    low = (detail or "").lower()
    markers = _ABSENT_TARGET if slug else _ABSENT_CWD
    return ABSENT if any(m in low for m in markers) else UNKNOWN


def no_repo_error(cli_example: str, detail: str | None = None,
                  explicit: bool = False) -> str:







































    value = target(explicit)
    state = classify_detail(detail, value) if detail is not None else ABSENT



    why = _one_line(detail or "") or "gh gave no reason"
    if value:
        if state == UNKNOWN:
            return (
                f"ERROR: repo target {value!r} could not be checked — the "
                f"lookup did not answer ({why!r}). Whether the target is wrong "
                f"and whether gh could reach GitHub are both UNKNOWN from "
                f"here. Retry; if it persists: gh repo view {value}"
            )
        return (
            f"ERROR: repo target {value!r} could not be resolved by gh. "
            f"Check the spelling and your access: gh repo view {value}"
        )
    example = _st_hint.st_hint("repo:OWNER/NAME", cli_example)
    if state == UNKNOWN:
        return (
            f"ERROR: could not work out which GitHub repo this is — the "
            f"lookup did not answer ({why!r}). That is not the same as the cwd "
            f"not being a GitHub repo, and which of the two it is is UNKNOWN "
            f"from here. Retry; if it persists, check gh (gh auth status; "
            f"gh repo view), or name a repo with a leading repo: op "
            f"({example})."
        )
    return (
        "ERROR: cwd is not a GitHub repo and no repo target was given. "
        "cd into a GitHub-cloned repo, name one with a leading repo: op "
        f"({example}), "
        "or run gh directly with --repo OWNER/REPO."
    )


def not_found_scope(explicit: bool = False) -> str:







    value = target(explicit)
    return f"in {value}" if value else "in this repo"


def not_found_hint(explicit: bool = False) -> str:




    value = target(explicit)
    if value:
        return f"Check the number, or the repo target (gh repo view {value})."
    return "Check the number or verify you're in the right repo (gh repo view)."









GL_PROJECT_PLACEHOLDER = "projects/:id"


def gl_project() -> str | None:








    value = target()
    if not value:
        return None
    return urllib.parse.quote(value, safe="")


def gl_args() -> list[str]:





    value = target()
    return ["-R", value] if value else []


def gl_api_path(path: str) -> str:












    project = gl_project()
    if not project or not path.startswith(GL_PROJECT_PLACEHOLDER):
        return path
    rest = path[len(GL_PROJECT_PLACEHOLDER):]
    if rest and rest[0] not in "/?":
        return path
    return f"projects/{project}{rest}"


def gl_not_found_hint() -> str:






    value = target()
    if value:
        return f"Check the number, or the repo target (glab repo view {value})."
    return "Check the number or verify you're in the right repo."







def is_github_host(host: str) -> bool:





















    host = (host or "").lower().rstrip(".")
    return host == GITHUB_HOST or host.endswith("." + GITHUB_HOST)


def url_host(url: str) -> str:










    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def github_owner_repo(url: str) -> tuple[str, str] | None:








    try:
        parts = urllib.parse.urlsplit(url or "")
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if not is_github_host(host):
        return None
    segments = [part for part in parts.path.split("/") if part]
    if len(segments) < 2:
        return None
    return segments[0], segments[1]


def resolve_or_conflict(payload: dict, op: str, key: str = "repo") -> tuple[str | None, str]:


































    repo_target = explicit_target()
    have = str(payload.get(key) or "").strip()
    if not repo_target:
        return None, ("payload" if have else "")
    if not have:
        payload[key] = repo_target
        return None, "repo: op"
    if have.casefold() == repo_target.casefold():
        return None, "payload (agrees with repo: op)"
    return (
        f"repo: {op!r}'s payload names {key} = {have!r}, and the repo: op "
        f"names {repo_target!r} — these disagree, so the target is "
        f"ambiguous. Make them agree, or drop one.\n"
    ), ""
