#!/usr/bin/env python3






































from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  
import _checks  
import _repo_target  
import _untrusted  
import _auth_probe  
import _status_probe  
import _digits  
import _st_hint  
from _env import env_int  




PER_PAGE = 100


TIMEOUT = 15

SUMMARY_MAX = 600
MESSAGE_MAX = 500


def _api_path(suffix: str) -> str:





    return _repo_target.api_path(suffix)


def _printable_api_path(suffix: str) -> str:







    return _repo_target.api_path_printable(suffix)


def _gh_error_kind(stderr: str) -> str:








    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return "repo"
    if _status_probe.says_not_found(s):
        return "notfound"








    if _auth_probe.says_not_authenticated(s):
        return "auth"
    if "rate limit" in s or "429" in s:
        return "ratelimit"
    if _status_probe.says_forbidden(s):
        return "forbidden"
    return "other"


class GhCall:









    def __init__(self, ok: bool, data: object = None, absent: bool = False,
                 error: str = "") -> None:
        self.ok = ok
        self.data = data
        self.absent = absent
        self.error = error


def _gh(args: list[str], timeout: int = TIMEOUT) -> GhCall:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return GhCall(False, error="gh not found — install the GitHub CLI")
    except subprocess.TimeoutExpired:
        return GhCall(False, error=f"gh timed out after {timeout}s")
    if r.returncode != 0:
        kind = _gh_error_kind(r.stderr)




        detail = (_untrusted.flat(r.stderr.strip())
                  or f"gh exited {r.returncode}")
        return GhCall(False, absent=(kind == "notfound"), error=detail)
    try:
        return GhCall(True, json.loads(r.stdout or "null"))
    except (json.JSONDecodeError, ValueError) as exc:
        return GhCall(False, error=f"gh returned unparseable JSON: {exc}")


def _job_probe(check_id: str) -> GhCall:

    return _gh(["gh", "api", _api_path(f"actions/jobs/{check_id}")])


def _not_found_message(check_id: str, probe: GhCall) -> str:

    scope = _repo_target.not_found_scope()
    if probe.ok:
        meta = probe.data if isinstance(probe.data, dict) else {}
        name = str(meta.get("name") or "?")
        return (
            f"ERROR: No check run #{check_id} {scope} — but an **Actions job** "
            f"with that id does exist ({name}). The id is right and the "
            f"namespace is not: Actions jobs and check runs are two id spaces "
            f"and this op reads the second. Read it with: "
            f"{_st_hint.st_hint(f'gh-job:{check_id}:fail')}"
        )
    if probe.absent:
        return (
            f"ERROR: No check run #{check_id} {scope}, and no Actions job with "
            f"that id either — both namespaces answered 404, so the id is "
            f"wrong. Check the ID. List the check runs on a PR with: "
            f"{_st_hint.st_hint('gh-check:pr:NUMBER')}"
        )
    return (
        f"ERROR: No check run #{check_id} {scope}. Whether it is an Actions job "
        f"instead could not be established — that probe did not answer: "
        f"{probe.error}. This op is not guessing between a wrong id and an id "
        f"in the other namespace. Retry, or read it directly with: "
        f"gh api {_printable_api_path('actions/jobs/' + check_id)}"
    )


def _clip(text: str, limit: int) -> str:







    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit} chars truncated]"


def _print_header(check_id: str, check: dict, routed_from: str = "",
                  mode_note: str = "") -> tuple[str, str]:












    name = _untrusted.flat(str(check.get("name") or "?"))
    status = str(check.get("status") or "?")
    conclusion = str(check.get("conclusion") or "")
    app = check.get("app") if isinstance(check.get("app"), dict) else {}
    slug = _untrusted.flat(str((app or {}).get("slug") or ""))
    print(f"# Check run #{check_id} — {name}")


    print("Source: checks API (a check run, not an Actions job)")
    if routed_from:






        print(routed_from)
    if mode_note:



        print(mode_note)
    print(f"Status: {_untrusted.flat(status)}"
          + (f" / {_untrusted.flat(conclusion)}" if conclusion else ""))
    if slug:
        print(f"App: {slug}")
    head_sha = _untrusted.flat(str(check.get("head_sha") or ""))
    if head_sha:
        print(f"Commit: {head_sha}")
    url = _untrusted.flat(str(check.get("html_url") or ""))
    if url:
        print(f"URL: {url}")
    output = check.get("output") if isinstance(check.get("output"), dict) else {}
    title = _untrusted.flat(_clip(str((output or {}).get("title") or ""), 200))
    summary = _clip(str((output or {}).get("summary") or ""), SUMMARY_MAX)
    if title or summary:
        print("\n## Output")
        if title:
            print(f"Title: {title}")
        if summary:




            print("Summary:")
            print(_untrusted.fence(summary))
    return status, conclusion


def _annotation_line(a: dict) -> list[str]:






    path = _untrusted.flat(str(a.get("path") or "?"))
    start = a.get("start_line")
    end = a.get("end_line")
    where = f"{path}:{start}" if start else path
    if end and start and end != start:
        where += f"-{end}"
    level = _untrusted.flat(str(a.get("annotation_level") or ""))
    title = _untrusted.flat(str(a.get("title") or "").strip())
    head = f"  {where}"
    if level:
        head += f"  [{level}]"
    if title:
        head += f"  {title}"
    lines = [head]






    message = _clip(str(a.get("message") or ""), MESSAGE_MAX)
    for part in message.splitlines():
        lines.append(f"      {_untrusted.flat(part)}")
    raw = _clip(str(a.get("raw_details") or ""), MESSAGE_MAX)
    for part in raw.splitlines():
        lines.append(f"      | {_untrusted.flat(part)}")
    return lines


def _print_annotations(annotations: list, conclusion: str, status: str = "") -> None:
    total = len(annotations)
    cap = env_int(os.environ.get("GH_CHECK_ANNOTATION_CAP"), "GH_CHECK_ANNOTATION_CAP", _checks.NAMED_CAP, minimum=1)
    if total == 0:
        print("\n## Annotations (0)")
        if status and status != "completed":






            print(
                f"This check run has not finished — its status is `{status}`. "
                f"Annotations are written while a check runs, so 0 is a "
                f"count taken mid-flight, not a result. Re-read it once the "
                f"check completes."
            )
        elif conclusion and conclusion not in ("success", "neutral", "skipped"):
            print(
                f"This check run published no annotations, and its conclusion "
                f"is `{conclusion}`. That is not an all-clear: a check can "
                f"fail with its detail only in the Output above, or in a "
                f"system this op does not read. Read the Output and the URL "
                f"before treating this as nothing."
            )
        else:
            print(
                f"This check run published no annotations and concluded "
                f"`{conclusion or 'unknown'}` — nothing was flagged on a line."
            )
        return

    shown = annotations[:cap]
    hidden = total - len(shown)
    note = f"+{hidden} more" if hidden else ""
    header = f"\n## Annotations ({total})"
    if hidden:
        header = (f"\n## Annotations ({total} total, {len(shown)} shown — "
                  f"{note}; raise GH_CHECK_ANNOTATION_CAP=N)")
    print(header)
    for a in shown:
        if not isinstance(a, dict):
            continue
        for line in _annotation_line(a):
            print(line)
    if hidden:


        print(f"... ({note} of {total} not shown — capped at {len(shown)} by "
              f"GH_CHECK_ANNOTATION_CAP)")
    if total >= PER_PAGE:
        print(
            f"NOTE: this op read the first page only (per_page={PER_PAGE}) and "
            f"it came back full, so {total} is a floor, not a total. Page the "
            f"rest by hand if the count matters."
        )


def _show_check(check_id: str) -> int:
    got = _gh(["gh", "api", _api_path(f"check-runs/{check_id}")])
    if not got.ok:
        if _gh_error_kind(got.error) == "repo":
            print(_repo_target.no_repo_error(f"gh-check:{check_id}"))
            return 1
        if got.absent:
            print(_not_found_message(check_id, _job_probe(check_id)))
            return 1
        print(f"ERROR: could not read check run #{check_id}: {got.error}")
        return 1
    check = got.data if isinstance(got.data, dict) else {}
    return render_check(check_id, check)


def render_check(check_id: str, check: dict, *, routed_from: str = "",
                 mode_note: str = "") -> int:













    print(_untrusted.banner())
    status, conclusion = _print_header(check_id, check, routed_from, mode_note)

    ann = _gh(["gh", "api",
               _api_path(f"check-runs/{check_id}/annotations?per_page={PER_PAGE}")])
    if not ann.ok:


        print(
            f"\nERROR: the check run was read, but its annotations were not: "
            f"{ann.error}. Whether this check flagged any line is UNKNOWN — "
            f"this is not zero annotations. Retry, or: gh api "
            f"{_printable_api_path('check-runs/' + check_id + '/annotations')}"
        )
        return 1
    annotations = ann.data if isinstance(ann.data, list) else []
    _print_annotations(annotations, conclusion, status)
    return 0


_MARK = {"success": "✓", "failure": "✗", "cancelled": "⊘", "skipped": "–",
         "neutral": "•", "timed_out": "✗", "action_required": "!"}


def _list_pr(pr: str) -> int:







    head = _gh(["gh", "pr", "view", pr, *_repo_target.gh_args(),
                "--json", "headRefOid"])
    if not head.ok:
        if _gh_error_kind(head.error) == "repo":
            print(_repo_target.no_repo_error(f"gh-check:pr:{pr}"))
            return 1
        print(f"ERROR: could not read PR #{pr}: {head.error}")
        return 1
    data = head.data if isinstance(head.data, dict) else {}
    sha = str(data.get("headRefOid") or "")
    if not sha:
        print(
            f"ERROR: PR #{pr} answered without a head commit SHA, so there is "
            f"nothing to list check runs against. This is not an empty check "
            f"list — the lookup never happened. Retry, or: "
            f"gh pr view {pr} --json headRefOid"
        )
        return 1

    got = _gh(["gh", "api",
               _api_path(f"commits/{sha}/check-runs?per_page={PER_PAGE}")])
    if not got.ok:
        print(f"ERROR: could not list check runs for PR #{pr} (head {sha}): "
              f"{got.error}")
        return 1
    payload = got.data if isinstance(got.data, dict) else {}
    runs = payload.get("check_runs")
    runs = runs if isinstance(runs, list) else []
    print(f"# Check runs on PR #{pr} — head commit {sha}")



    print(_untrusted.flat_note("check run names"))
    if not runs:
        print(
            f"0 check runs are attached to the head commit {sha}. That is a "
            f"statement about this commit, not about the PR: a check attached "
            f"to the merge ref, or one that has not been created yet, does not "
            f"appear here. Cross-check with: "
            f"{_st_hint.st_hint(f'gh-pr:{pr}:status')}"
        )
        return 0
    print(f"{len(runs)} check run{'s' if len(runs) != 1 else ''} on the head commit.")
    for r in runs:
        if not isinstance(r, dict):
            continue
        conclusion = str(r.get("conclusion") or r.get("status") or "?")
        mark = _MARK.get(conclusion, "?")
        rid = str(r.get("id") or "")


        name = _untrusted.flat(str(r.get("name") or "?"))
        conclusion = _untrusted.flat(conclusion)
        line = f"  {mark} {conclusion:<12} {name}"
        if rid:
            line += f"  #{rid}"
        print(line)
    print(f"\nRead one with: {_st_hint.st_hint('gh-check:<id>')}  — "
          f"annotations (path:line, title, message) are where a scanning "
          f"check keeps its finding.")
    return 0


def _usage() -> int:
    print("ERROR: usage: gh-check:CHECK_RUN_ID  |  gh-check:pr:NUMBER\n"
          "  gh-check:ID       — one check run: status, output, annotations\n"
          "  gh-check:pr:N     — check runs on PR N's head commit, with ids\n"
          "For an Actions job id use gh-job — they are two id namespaces.")
    return 1


def main() -> int:
    use_utf8_stdout()
    args = sys.argv[1:]
    if not args or not args[0].strip():
        return _usage()
    first = args[0].strip()



    if first.lower() == "pr":
        pr = args[1].strip() if len(args) > 1 else ""
        if not _digits.is_ascii_int(pr):
            print("ERROR: usage: gh-check:pr:NUMBER (a PR number)")
            return 1
        return _list_pr(pr)
    if not _digits.is_ascii_int(first):
        print(f"ERROR: {first!r} is not a check-run id. "
              f"Usage: gh-check:CHECK_RUN_ID | gh-check:pr:NUMBER")
        return 1
    return _show_check(first)


if __name__ == "__main__":
    sys.exit(main())
