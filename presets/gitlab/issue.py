#!/usr/bin/env python3





from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  

import _body  
import _checks  
import _image_root  
import _repo_target  
import _secrets  
import _untrusted  
import _classify_render  
import _auth_probe  
import _status_probe  

DESCRIPTION_MAX = 3000


_CLASSIFY_LEVEL = _classify_render.level_from_env()



RELATED_MRS_MAX = 10



CLOSING_MRS_MAX = 10
COMMENT_MAX = 1000






IMAGE_DIR = _image_root.default_root()


def _glab(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:





    return subprocess.run(
        ["glab"] + args + _repo_target.gl_args(),
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:

    s = stderr.lower()
    if _status_probe.says_not_found(s):
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.gl_not_found_hint()}")






    if (_auth_probe.says_not_authenticated(s, _auth_probe.GITLAB_MARKERS)
            or _secrets.mentions_gitlab_token(s)):
        return "ERROR: glab not authenticated. Run: glab auth login"
    if _status_probe.says_forbidden(s):
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."



    return (f"ERROR: glab failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _glab_api(endpoint: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:





    return subprocess.run(
        ["glab", "api", _repo_target.gl_api_path(endpoint)],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _pipeline_line(mr: dict) -> str:

























    pipeline = mr.get("head_pipeline")
    status = pipeline.get("status") if isinstance(pipeline, dict) else None
    if not status:
        return f"pipeline: {_checks.NO_PIPELINE}"
    pid = pipeline.get("id")
    marker = "" if _checks.bucket(status) == "passed" else f" {_checks.NOT_GREEN}"
    ref = f" (#{pid})" if pid else ""
    return f"pipeline: {_untrusted.flat(str(status))}{ref}{marker}"


def _mr_list_unknown(label: str, reason: str) -> None:

















    print(f"{chr(10)}{label}: unknown — could not query "
          f"({_untrusted.flat(reason) or 'no detail from glab'})")


def _fetch_mr_list(endpoint: str, label: str) -> "list | None":












    try:
        mr_result = _glab_api(endpoint)
    except (subprocess.TimeoutExpired, OSError) as exc:
        _mr_list_unknown(label, f"glab api failed: {type(exc).__name__}")
        return None

    if mr_result.returncode != 0:








        reason = _untrusted.split_lines((mr_result.stderr or "").strip())
        _mr_list_unknown(label, reason[0] if reason else "glab api exited non-zero")
        return None

    try:
        mrs = json.loads(mr_result.stdout)
    except json.JSONDecodeError:
        _mr_list_unknown(label, "glab api returned output that is not JSON")
        return None

    if not isinstance(mrs, list):
        _mr_list_unknown(label, "glab api returned an unexpected shape")
        return None

    if not mrs:
        print(f"{chr(10)}{label}: none")
        return None

    return mrs


def _print_related_mrs(iid: object, full: bool) -> None:








    mrs = _fetch_mr_list(
        f"projects/:id/issues/{iid}/related_merge_requests", "Related MRs")
    if mrs is None:
        return

    shown_mrs = mrs if full else mrs[:RELATED_MRS_MAX]
    hidden_mrs = len(mrs) - len(shown_mrs)
    if hidden_mrs:
        print(
            f"{chr(10)}Related MRs: {len(shown_mrs)} of {len(mrs)} shown "
            f"({hidden_mrs} not listed — count limit of "
            f"{RELATED_MRS_MAX}; use :full for all)"
        )
    else:
        print(f"{chr(10)}Related MRs: {len(mrs)}")
    for mr in shown_mrs:
        if not isinstance(mr, dict):
            continue
        mr_iid = mr.get("iid", "?")
        mr_title = mr.get("title", "?")
        mr_state = mr.get("state", "?")
        mr_branch = mr.get("source_branch", "?")
        print(f"  !{mr_iid} ({mr_state}) {_untrusted.flat(mr_title)}")
        print(f"    branch: {_untrusted.flat(mr_branch)}")
        print(f"    {_pipeline_line(mr)}")
    if hidden_mrs:
        print(
            f"  ... ({hidden_mrs} more related MR(s) not shown — "
            f"use :full)"
        )


def _print_closing_mrs(iid: object, full: bool) -> None:












    mrs = _fetch_mr_list(
        f"projects/:id/issues/{iid}/closed_by", "Closing MRs")
    if mrs is None:
        return

    shown_mrs = mrs if full else mrs[:CLOSING_MRS_MAX]
    hidden_mrs = len(mrs) - len(shown_mrs)
    if hidden_mrs:
        print(
            f"{chr(10)}Closing MRs: {len(shown_mrs)} of {len(mrs)} shown "
            f"({hidden_mrs} not listed — count limit of "
            f"{CLOSING_MRS_MAX}; use :full for all)"
        )
    else:
        print(f"{chr(10)}Closing MRs: {len(mrs)}")
    for mr in shown_mrs:
        if not isinstance(mr, dict):
            continue
        mr_iid = mr.get("iid", "?")
        mr_title = mr.get("title", "?")
        mr_state = mr.get("state", "?")
        mr_branch = mr.get("source_branch", "?")
        print(f"  !{mr_iid} ({mr_state}) {_untrusted.flat(mr_title)}")
        print(f"    branch: {_untrusted.flat(mr_branch)}")
        print(f"    {_pipeline_line(mr)}")
    if hidden_mrs:
        print(
            f"  ... ({hidden_mrs} more closing MR(s) not shown — "
            f"use :full)"
        )


def _extract_image_urls(text: str) -> list[str]:






    patterns = [
        r'!\[[^\]]*\]\((/uploads/[^\)]+)\)',
        r'!\[[^\]]*\]\((https?://[^\)]*?/uploads/[^\)]+)\)',
    ]
    urls: list[str] = []
    for pattern in patterns:
        urls.extend(re.findall(pattern, text))
    return urls


def _is_inside(candidate: str, directory: str) -> bool:




















    return _image_root.is_inside(candidate, directory)


def _download_images(image_urls: list[str], issue_number: str) -> list[str]:



























    if not image_urls:
        return []




    if not re.fullmatch(r"[0-9]+", str(issue_number)):
        print(f"note: skipped {len(image_urls)} attachment(s) — the issue id "
              f"{_untrusted.flat(str(issue_number))!r} from the API reply is "
              "not numeric, so no download directory was chosen")
        return []




    root, why = _image_root.ensure(IMAGE_DIR)
    if root is None:
        print(f"note: skipped {len(image_urls)} attachment(s) — no attachment "
              f"root this process owns could be established: {why}")
        return []

    out_dir = os.path.join(root, str(issue_number))
    if not _is_inside(out_dir, root):



        print(f"note: skipped {len(image_urls)} attachment(s) — the download "
              f"directory did not resolve inside {root}")
        return []






    out_dir, why = _image_root.ensure(out_dir)
    if out_dir is None:
        print(f"note: skipped {len(image_urls)} attachment(s) — the per-issue "
              f"directory could not be established: {why}")
        return []

    downloaded: list[str] = []
    for url in image_urls:

        match = re.search(r'(/uploads/[^\s\)]+)', url)
        if not match:
            continue

        upload_path = match.group(1)




        local_name = os.path.basename(urllib.parse.unquote(upload_path))
        local_path = os.path.join(out_dir, local_name)



        if not (_is_inside(local_path, root)
                and _is_inside(local_path, out_dir)):
            print("note: skipped an attachment whose name resolves outside "
                  f"{root}")
            continue




        api_path = _repo_target.gl_api_path(f"projects/:id{upload_path}")
        try:
            result = subprocess.run(
                ["glab", "api", "--method", "GET", api_path],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                with open(local_path, "wb") as f:
                    f.write(result.stdout)
                downloaded.append(local_path)
        except (subprocess.TimeoutExpired, OSError):
            continue

    return downloaded


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: issue.py NUMBER [full]")
        return 1

    number = sys.argv[1]
    full = len(sys.argv) > 2 and sys.argv[2] == "full"
    desc_max = None if full else DESCRIPTION_MAX
    comment_max = None if full else COMMENT_MAX


    try:
        result = _glab(["issue", "view", number, "--output", "json"])
    except FileNotFoundError:
        print("ERROR: glab not found — install the GitLab CLI")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "Issue", number))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from glab\n{result.stdout[:500]}")
        return 1


    title = _untrusted.flat(d.get("title", "?"))
    state = d.get("state", "?")
    labels = _untrusted.flat(", ".join(d.get("labels", [])) or "none")
    milestone = _untrusted.flat((d.get("milestone") or {}).get("title", "none"))
    assignees = _untrusted.flat(", ".join(a.get("username", "?") for a in d.get("assignees", [])) or "none")
    author = _untrusted.flat((d.get("author") or {}).get("username", "?"))
    iid = d.get("iid", number)
    web_url = d.get("web_url", "")


    description = re.sub(
        r'\{width=\d+\s+height=\d+\}', '', d.get("description") or ""
    )
    description_total = len(description)
    description, description_withheld = _body.cut(description, desc_max)
    project_id = d.get("project_id", "")



    print(_untrusted.banner())
    print(f"# #{iid} {title}")
    print(f"State: {state} | Author: {author}")
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    print(f"Assignees: {assignees}")
    if web_url:
        print(f"URL: {web_url}")
    if description_withheld:


        print(_body.header_notice(
            description, description_total, description_withheld))











    _print_related_mrs(iid, full)




    _print_closing_mrs(iid, full)



    classify_budget = _classify_render.Budget()


    if description:
        print(f"\n## Description\n{_untrusted.fence(description)}")
        if description_withheld:
            print(f"\n{_body.cut_notice(description_withheld)}")
        print(classify_budget.line(description, level=_CLASSIFY_LEVEL))


    all_image_urls = _extract_image_urls(description)
    try:
        notes_result = _glab_api(
            f"projects/:id/issues/{iid}/notes?per_page=50&sort=asc"
        )
        if notes_result.returncode == 0:
            notes = json.loads(notes_result.stdout)
            if isinstance(notes, list):
                human_notes = [n for n in notes if not n.get("system", False)]
                system_count = len(notes) - len(human_notes)
                if human_notes:
                    if full:
                        shown_notes = human_notes
                        print(f"\n## Comments ({len(human_notes)} human, {system_count} system skipped)")
                    else:
                        shown_notes = human_notes[-10:]
                        truncated = len(human_notes) - len(shown_notes)
                        suffix = f", {truncated} earlier truncated — use :full to fetch all" if truncated else ""
                        print(f"\n## Comments ({len(shown_notes)} of {len(human_notes)} human shown{suffix}, {system_count} system skipped)")
                    for note in shown_notes:
                        note_author = _untrusted.flat((note.get("author") or {}).get("username", "?"))
                        body = note.get("body") or ""


                        note_trunc = ""
                        if comment_max is not None and len(body) > comment_max:
                            body = body[:comment_max]
                            note_trunc = _body.comment_cut_notice(comment_max)
                        created = (note.get("created_at") or "")[:10]
                        print(f"\n**{note_author}** ({created}):")
                        print(_untrusted.fence(body))
                        if note_trunc:
                            print(note_trunc)
                        print(classify_budget.line(body, level=_CLASSIFY_LEVEL))

                        all_image_urls.extend(_extract_image_urls(note.get("body") or ""))
                else:
                    print(f"\n## Comments (0 human, {system_count} system skipped)")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass


    if all_image_urls:

        all_image_urls = list(dict.fromkeys(all_image_urls))
        downloaded = _download_images(all_image_urls, str(iid))
        print(f"\n## Images ({len(all_image_urls)} found, {len(downloaded)} downloaded)")
        for path in downloaded:
            print(f"  {path}")
        failed = len(all_image_urls) - len(downloaded)
        if failed > 0:
            print(f"  ({failed} failed to download)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
