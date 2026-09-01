#!/usr/bin/env python3
"""Create a GitHub issue from a JSON/TOML payload file.

**A GraphQL-mutation-path outage falls back to REST, named as such (#1790).**
`gh issue create` writes through GraphQL. Observed 2026-08-17, six attempts
over ~10 minutes, all identical: `HTTP 503: No server is currently available
to service your request. (https://api.github.com/graphql)` -- while
`gh api rate_limit` answered normally and a raw `gh api -X POST
repos/OWNER/REPO/issues` with the same title, body and labels succeeded on the
first try, four times. So the outage was specific to the mutation transport,
not to GitHub as a whole, and a REST POST is a real alternative route to the
same write.

`gh-pr-edit` (#195, #1739) already made this call for editing a published pull
request: write through REST, read back what landed, and never hide which
transport answered. The same three conditions apply here, and the third is
the one with teeth:

1. **The receipt names which transport answered** -- `transport=graphql` or
   `transport=rest (fallback ...)`. This is not cosmetic: `gh issue create`
   resolves label and milestone *names* through GraphQL, while a REST POST
   needs a milestone *number* and takes label names largely as-is, so the two
   paths can behave differently on a repo whose labels or milestones do not
   all exist. `_resolve_milestone_number` below is the one place that
   difference is bridged, and where it cannot be bridged (no such open
   milestone by that title) the field is named NOT APPLIED rather than
   silently dropped.
2. **This is scoped to `gh-issue-create` alone.** Nothing here changes any
   other writer; a blanket fallback would silently change what every op
   means, which the issue explicitly calls worse than an outage.
3. **A 503 does not prove the mutation did not land**, so a naive retry can
   file the same issue twice -- expensive to unpick on a tracker. Before ever
   POSTing through REST, `_find_open_issue_by_title` looks for an open issue
   with the exact title this call is about to create. A match means the
   earlier GraphQL attempt likely landed despite the 503, and this call
   reports that issue rather than filing a second one. If the lookup itself
   cannot answer (`_gh_json` erroring), this refuses to write blind rather
   than guessing either way -- see `test_dedup_lookup_failure_refuses_to_write_blind`.

**Deliberately not a version floor.** The issue raised the alternative that
`gh` itself might already intend to fall back, the way #195 reasoned about
`cli/cli#13069` (a stale Debian package, fixed by upgrading `gh`, not by this
tool). Checked here: `cli/cli#13069` is unrelated (a `pr edit` deprecation
error caused by an outdated build, closed as "upgrade `gh`"), and a GitHub
code/issue search for a REST-fallback-on-GraphQL-outage feature in `gh`
turned up nothing. There is no known version that fixes this, so a version
floor is not an available fix and the fallback below is the right shape.

**Deliberately narrow detection.** `_is_graphql_transport_failure` only
matches the shape of error actually observed -- "no server is currently
available", or "503" alongside "graphql" -- so an ordinary refusal (bad
`--milestone`, no write access, an auth failure) falls straight through to
the plain ERROR path and never triggers a second write attempt. Widening it
needs its own outage evidence, not a guess at what else GitHub might say.
"""
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
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
import _remote_default as _rd  # noqa: E402
import _repo_target  # noqa: E402  (repo: op precedence over payload's own field -- #1909)
import _untrusted  # noqa: E402  (the GitHub API writes the failure body — #1606)

# The transport that actually answered. Named in every receipt (#1790) so a
# degraded write is never indistinguishable from an ordinary one.
TRANSPORT_GRAPHQL = "graphql"
TRANSPORT_REST_FALLBACK = "rest (fallback -- mutation transport unavailable)"


def _gh(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _gh_json(args: list[str], stdin: str | None = None,
             timeout: int = 30) -> tuple[object, str]:
    """`(parsed, error)` for a `gh api` call whose stdout is JSON.

    Used only for the REST fallback path -- the dedup lookup, the milestone
    lookup, and the POST itself. A non-empty error means `parsed` is not
    usable; the caller decides what "could not tell" means for its own step
    (#1790's condition 3 needs this to be a refusal, not a guess, when the
    dedup lookup itself fails).
    """
    try:
        result = subprocess.run(["gh"] + args, capture_output=True, text=True,
                                 input=stdin, timeout=timeout, encoding="utf-8",
                                 errors="replace")
    except FileNotFoundError:
        return (None, "gh not found -- install from https://cli.github.com")
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
    """Whether `text` (gh's stderr+stdout) looks like the #1790 outage.

    Matched signatures, from the outage actually observed in one minute on
    2026-08-17: "no server is currently available" (gh's own relay of the
    GraphQL 503), or "503" alongside "graphql" (the endpoint named in the
    same message). Deliberately narrow -- see the module docstring.
    """
    low = text.lower()
    if "no server is currently available" in low:
        return True
    if "503" in text and "graphql" in low:
        return True
    return False


_DEDUP_PER_PAGE = 100
# 50 pages of 100 -- 5,000 open issues -- is far beyond anything this repo or
# any repo this tool manages has ever carried; past it this refuses rather
# than paging forever against a repo (or a broken response) that always
# reports a full page. See the refusal message below for what a caller sees.
_DEDUP_MAX_PAGES = 50


def _find_open_issue_by_title(repo: str, title: str,
                               timeout: int = 30) -> tuple[dict | None, str]:
    """`(existing issue, error)` -- an open issue with this exact title, so a
    REST fallback never files a duplicate of a mutation that actually landed
    despite the 503 it reported (#1790 condition 3).

    A non-empty error means the lookup itself could not answer; the caller
    must refuse to write rather than treat that as "no match".

    Pages through REST rather than reading one page (#2021): a page that
    comes back exactly `per_page` items long is not proof the title is not
    on the *next* page -- GitHub's REST default ordering is newest-first, so
    the entries pushed off the first page are exactly the long-lived ones a
    stalled maintainer loop is most likely to be re-filing after an outage.
    A page shorter than `per_page` is proof there is no next page, so the
    loop stops there rather than issuing a request it does not need.
    """
    for page in range(1, _DEDUP_MAX_PAGES + 1):
        # `gh api` defaults to POST the instant a `-f`/`-F` parameter is
        # added (its own --help: "adding request parameters will
        # automatically switch the request method to POST"). Without an
        # explicit `-X GET` this call silently becomes `POST
        # repos/{repo}/issues` -- the *create* endpoint, with no `title` in
        # the body -- so it 422s on every real invocation and the dedup
        # guard refuses to write on every genuine transport outage, which is
        # the opposite of what condition 3 asks for. Caught in review,
        # before it ever reached a real `gh` binary (#1790).
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
    """`(number, note)` -- REST issue creation needs a milestone *number*,
    while the payload (and `gh issue create --milestone`) carries a *name*.
    `note` is empty on success; on a miss it explains why, so the caller can
    name the field NOT APPLIED rather than silently drop it.
    """
    # Same `-f` -> POST trap as `_find_open_issue_by_title` above -- an
    # explicit `-X GET` is not optional here either (#1790).
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


def _validate(payload: dict) -> str | None:
    if not payload.get("repo"):
        return "ERROR: payload missing required field: repo"
    if not payload.get("title"):
        return "ERROR: payload missing required field: title"
    if payload.get("body") and payload.get("body_file"):
        return "ERROR: payload has both body and body_file — use one"
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
            "ERROR: gh-issue-create needs a payload — "
            "gh-issue-create:@FILE, or gh-issue-create:@- to read it from "
            "stdin (JSON or TOML with title/body)."
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
        print(f"ERROR: failed to parse payload: {e} (expected JSON or TOML with title/body)")
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

    repo = payload["repo"]
    title = payload["title"]
    body_file = payload.get("body_file")
    body = payload.get("body", "")
    labels: list[str] = payload.get("labels") or []
    assignees: list[str] = payload.get("assignees") or []
    milestone: str = payload.get("milestone", "")

    if body_file:
        # Same is_dir()-before-read shape as the payload guard above: a
        # directory read raises IsADirectoryError on POSIX but
        # PermissionError on Windows, so the directory verdict must come
        # from is_dir(), not from catching whichever OSError subtype the
        # platform happens to raise. See #620/#627/#630.
        if Path(body_file).is_dir():
            print(f"ERROR: body_file is a directory, not a file: {body_file}")
            return 1
        try:
            content = Path(body_file).read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"ERROR: body_file not found: {body_file}")
            return 1
        except IsADirectoryError:
            # Belt-and-suspenders for the TOCTOU window between the
            # is_dir() check above and this read.
            print(f"ERROR: body_file is a directory, not a file: {body_file}")
            return 1
        except PermissionError as e:
            # Deliberately distinct from "is a directory": a locked or
            # wrong-ownership file also raises PermissionError, and it's
            # the only thing a directory read raises on Windows.
            print(f"ERROR: permission denied reading body_file: {body_file} — {e}")
            return 1
    else:
        content = body

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
            print("ERROR: gh not found — install from https://cli.github.com")
            return 1
        except subprocess.TimeoutExpired:
            print("ERROR: gh timed out")
            return 1

        if result.returncode != 0:
            combined = (result.stderr or "") + (result.stdout or "")
            if not _is_graphql_transport_failure(combined):
                print(f"ERROR: gh issue create failed (exit {result.returncode})")
                # Whatever gh echoed here was written by the GitHub API, and
                # it prints at column 0 with nothing in front of it —
                # flatten, never relay (#1606). Both arms: the stdout
                # fallback is a second relay.
                print(_untrusted.flat(result.stderr.strip() or result.stdout.strip()))
                return 1

            # ---- #1790: the mutation transport looks down, REST does not --
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
                  f"transport={TRANSPORT_REST_FALLBACK}{note_str}{source_note}")
            return 0

        match = re.search(r"https?://github\.com/[^/\s]+/[^/\s]+/issues/(\d+)", result.stdout)
        if match:
            url = match.group(0)
            number = match.group(1)
        else:
            # The regex arm above cannot be forged — its `[^/\s]` classes
            # reject U+2028, which Python's `\s` matches. This is the fallback,
            # and it takes whatever `gh` last printed into an `OK` receipt at
            # column 0: so the boundary is `split_lines`'s rather than
            # `str.splitlines()`'s, and the segment it selects is flattened
            # (#1648).
            printed = _untrusted.split_lines(result.stdout.strip())
            url = _untrusted.flat(printed[-1]) if printed else "?"
            number = url.rstrip("/").split("/")[-1] if "/" in url else "?"

        source_note = (f"  (repo from {repo_source})"
                        if repo_source not in ("", "payload") else "")
        print(f"gh-issue-create OK number={number} url={url} "
              f"transport={TRANSPORT_GRAPHQL}{source_note}")
        return 0

    finally:
        if tmp_body and os.path.exists(tmp_body):
            os.unlink(tmp_body)


if __name__ == "__main__":
    sys.exit(main())
