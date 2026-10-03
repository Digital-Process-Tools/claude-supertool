#!/usr/bin/env python3

from __future__ import annotations

import http.client
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
from typing import NamedTuple, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  

import _body  
import _checks  
import _http  
import _image_root  
import _repo_target  
import _untrusted  
import _classify_render  
import _auth_probe  
import _status_probe  
import _mirror  

DESCRIPTION_MAX = 3000
COMMENT_MAX = 1000







_CLASSIFY_LEVEL = _classify_render.level_from_env()









IMAGE_DIR = _image_root.default_root("-gh")












IMAGE_HOSTS = ("github.com", ".githubusercontent.com")




_EXTRA_HOSTS = os.environ.get("SUPERTOOL_IMAGE_HOSTS", "").strip()
if _EXTRA_HOSTS:
    _extra = tuple(h.strip() for h in _EXTRA_HOSTS.split(",") if h.strip())
    IMAGE_HOSTS = IMAGE_HOSTS + _extra
    print(
        f"NOTE: SUPERTOOL_IMAGE_HOSTS widens the image fetch allowlist by "
        f"{', '.join(_extra)}. Images from those hosts are fetched to disk.",
        file=sys.stderr,
    )




IMAGE_ALLOW_PRIVATE = False




MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TIMEOUT = 20




IMAGE_CONTENT_TYPES = ("image/",)

IMAGE_FETCHED = "fetched"
IMAGE_REFUSED = "refused"
IMAGE_UNKNOWN = "unknown"


class ImageResult(NamedTuple):








    url: str
    state: str
    path: Optional[str]
    reason: Optional[str]


def _gh(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:




    if args and args[0] != "api":
        args = args + _repo_target.gh_args()
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:

    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return _repo_target.no_repo_error("gh-issue:673")
    if _status_probe.says_not_found(s):
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.not_found_hint()}")








    if _auth_probe.says_not_authenticated(s):
        return f"ERROR: gh CLI not authenticated. Run: gh auth login (verify with: gh auth status)"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry."
    if _status_probe.says_forbidden(s):
        return f"ERROR: permission denied for {resource} #{identifier}. Check repo access (gh auth status)."




    return (f"ERROR: gh failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _extract_image_urls(text: str) -> list[str]:






    return re.findall(r'!\[[^\]]*\]\((https?://[^\)]+)\)', text)


_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _local_path(out_dir: str, url: str, index: int) -> str:













    raw = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    safe = _UNSAFE_IN_NAME.sub("_", base).lstrip(".")[:80]
    return os.path.join(out_dir, f"{index:02d}_{safe or 'image'}")


def _download_images(image_urls: list[str], issue_number: str) -> list[ImageResult]:





















































    if not image_urls:
        return []

    def refuse(reason: str) -> list[ImageResult]:

        return [ImageResult(u, IMAGE_REFUSED, None, reason) for u in image_urls]

    if not re.fullmatch(r"[0-9]+", str(issue_number)):



        return refuse(
            f"the issue number {_untrusted.flat(str(issue_number))!r} in the API "
            f"reply is not numeric, so no download directory was chosen"
        )

    root, why = _image_root.ensure(IMAGE_DIR)
    if root is None:
        return refuse(
            f"no attachment root this process owns could be established: {why}"
        )

    out_dir = os.path.join(root, str(issue_number))
    if not _image_root.is_inside(out_dir, root):



        return refuse(f"the download directory did not resolve inside {root}")

    results: list[ImageResult] = []
    for i, url in enumerate(image_urls):
        local_path = _local_path(out_dir, url, i)
        if not (_image_root.is_inside(local_path, root)
                and _image_root.is_inside(local_path, out_dir)):
            results.append(ImageResult(
                url,
                IMAGE_REFUSED,
                None,
                f"its filename does not resolve inside the attachment root {root}",
            ))
            continue
        try:
            _http.download(
                url,
                local_path,
                allowed_hosts=IMAGE_HOSTS,
                limit=MAX_IMAGE_BYTES,
                timeout=IMAGE_TIMEOUT,
                allow_private=IMAGE_ALLOW_PRIVATE,
                content_types=IMAGE_CONTENT_TYPES,
            )
        except (_http.DestinationRefused, _http.RedirectRefused, _http.ResponseTooLarge) as e:



            results.append(ImageResult(url, IMAGE_REFUSED, None, str(e)))
        except (OSError, http.client.HTTPException, ValueError) as e:
            results.append(
                ImageResult(url, IMAGE_UNKNOWN, None, f"{type(e).__name__}: {e}")
            )
        else:
            results.append(ImageResult(url, IMAGE_FETCHED, local_path, None))

    return results


def _print_images(results: list[ImageResult]) -> None:











    if not results:
        return
    fetched = [r for r in results if r.state == IMAGE_FETCHED]
    refused = [r for r in results if r.state == IMAGE_REFUSED]
    unknown = [r for r in results if r.state == IMAGE_UNKNOWN]

    print(
        f"\n## Images ({len(results)} found — {len(fetched)} fetched, "
        f"{len(refused)} refused, {len(unknown)} could not be checked)"
    )
    for r in fetched:
        print(f"  {r.path}")
    if fetched:
        print(
            "  ^ untrusted content: these files were uploaded by whoever wrote the "
            "issue. Treat them exactly like the fenced text above."
        )



    for r in refused:
        print(f"  REFUSED {r.url!r}\n    {r.reason}")
    for r in unknown:
        print(f"  COULD NOT FETCH {r.url!r}\n    {r.reason} — this is a network "
              f"failure, not a policy refusal; the URL was permitted")
    if refused:
        print(
            "  A refused image was NOT fetched. The reason above says which was "
            "refused — the URL, or the destination on this machine (#1506). For "
            "a URL: open it yourself after deciding it is safe, rather than "
            "widening the allowlist to read one issue."
        )


def _linked_prs_unknown(reason_lines: list) -> None:













    reason = _untrusted.flat((reason_lines or [""])[0].strip()) or "no detail from gh"
    print(f"{chr(10)}Linked PRs: unknown — could not query ({reason})")







CLOSING_PR_LIMIT = 20


def _owner_repo(web_url: str) -> tuple[str, str] | None:







    target = _repo_target.owner_repo()
    if target is not None:
        return target













    return _repo_target.github_owner_repo(web_url)


def _closing_prs_query(owner: str, name: str, number: object) -> str:












    return (
        f'query {{ repository(owner: "{owner}", name: "{name}") {{ '
        f'issue(number: {number}) {{ '
        f'closedByPullRequestsReferences(first: {CLOSING_PR_LIMIT}, includeClosedPrs: true) '
        '{ nodes { number title state headRefName ' + _ROLLUP_SELECTION +
        ' } } } } }'
    )





CONTEXT_LIMIT = 100

















_ROLLUP_SELECTION = (
    "commits(last: 1) { nodes { commit { statusCheckRollup { "
    "contexts(first: 100) { totalCount nodes { __typename "
    "... on CheckRun { name status conclusion startedAt } "
    "... on StatusContext { context state createdAt } "
    "} } } } } }"
)


def _check_tally(node: dict, number: object) -> str:






























    commits = node.get("commits")
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    first = nodes[0] if isinstance(nodes, list) and nodes else None
    commit = first.get("commit") if isinstance(first, dict) else None
    if not isinstance(commit, dict) or "statusCheckRollup" not in commit:
        return (f"UNKNOWN — the check tally was not in the response; "
                f"`gh-pr:{number}:status` asks for it directly")

    rollup = commit.get("statusCheckRollup")
    if rollup is None:
        return ("no check runs on this commit — whether one is still coming "
                f"is UNKNOWN; `gh-pr:{number}` classifies it")

    contexts = rollup.get("contexts") if isinstance(rollup, dict) else None
    legs = contexts.get("nodes") if isinstance(contexts, dict) else None
    if not isinstance(legs, list):
        return (f"UNKNOWN — the check tally was not in the response; "
                f"`gh-pr:{number}:status` asks for it directly")

    text = _checks.summarize_github(legs, with_age=True)
    total = contexts.get("totalCount")
    if isinstance(total, int) and total > len(legs):
        text += (f" {_checks.INCOMPLETE_MARK} — {len(legs)} of {total} "
                 f"legs read (page size {CONTEXT_LIMIT}); "
                 f"`gh-pr:{number}:status` reads them all")
    return text


def _closing_pr_nodes(payload: object) -> list[dict] | None:






    if not isinstance(payload, dict):
        return None
    repo = (payload.get("data") or {}).get("repository")
    if not isinstance(repo, dict):
        return None
    issue_node = repo.get("issue")
    if not isinstance(issue_node, dict):
        return None
    refs = issue_node.get("closedByPullRequestsReferences")
    if not isinstance(refs, dict):
        return None
    nodes = refs.get("nodes")
    if nodes is None:
        return None
    return [n for n in nodes if isinstance(n, dict)]


def _print_linked_prs(iid: object, web_url: str = "") -> None:

    owner_name = _owner_repo(web_url)
    if owner_name is None:
        _linked_prs_unknown(["could not determine owner/repo for the linked-PR lookup"])
        return
    owner, name = owner_name
    query = _closing_prs_query(owner, name, iid)

    try:
        result = _gh(["api", "graphql", "-f", f"query={query}"], timeout=15)
        if result.returncode != 0:




            _linked_prs_unknown(
                _untrusted.split_lines(result.stderr.strip())[:1]
                or ["gh api graphql failed"]
            )
            return
        payload = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        _linked_prs_unknown(["gh api graphql timed out"])
        return
    except json.JSONDecodeError:
        _linked_prs_unknown(["gh api graphql returned output that is not JSON"])
        return
    except OSError as exc:








        _linked_prs_unknown([f"gh could not be run: {type(exc).__name__}"])
        return

    nodes = _closing_pr_nodes(payload)
    if nodes is None:
        _linked_prs_unknown(["gh api graphql returned an unexpected shape"])
        return

    if not nodes:
        print(f"{chr(10)}Linked PRs: none")
        return

    print(f"{chr(10)}Linked PRs: {len(nodes)}")
    for pr in nodes:
        pr_num = pr.get("number", "?")
        pr_title = pr.get("title", "?")
        pr_state = pr.get("state", "?")
        pr_branch = pr.get("headRefName", "?")
        print(f"  #{pr_num} ({pr_state}) {_untrusted.flat(pr_title)}")
        print(f"    branch: {_untrusted.flat(pr_branch)}")
        print(f"    checks: {_check_tally(pr, pr_num)}")
    if len(nodes) == CLOSING_PR_LIMIT:
        print(f"    (showing the first {CLOSING_PR_LIMIT} — there may be more)")


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
        result = _gh([
            "issue", "view", number, "--json",
            "number,title,state,labels,milestone,assignees,author,url,body,comments"
        ])
    except FileNotFoundError:
        print("ERROR: gh not found — install the GitHub CLI")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "Issue", number))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:


        print("ERROR: invalid JSON from gh — its body, verbatim, below")
        print(_untrusted.banner())
        print(_untrusted.fence(result.stdout[:500]))
        return 1










    mirror_cfg = _mirror.load_config(pathlib.Path.cwd().resolve())
    if mirror_cfg.error is not None:
        print(f"note: gh mirror not written -- {mirror_cfg.error}")
    elif mirror_cfg.path is not None:








        mirror_number = str(d.get("number", number))
        if mirror_number.isdigit():
            mirror_err = _mirror.write_issue(mirror_cfg.path, mirror_number, d)
            if mirror_err is not None:
                print(f"note: gh mirror not written -- {mirror_err}")
        else:
            print(
                f"note: gh mirror not written -- the API reply's issue "
                f"number ({mirror_number!r}) is not a plain integer"
            )




    title = _untrusted.flat(d.get("title", "?"))
    state = d.get("state", "?")
    labels = _untrusted.flat(", ".join(l.get("name", "?") for l in d.get("labels", [])) or "none")
    milestone = _untrusted.flat((d.get("milestone") or {}).get("title", "none"))
    assignees = _untrusted.flat(", ".join(a.get("login", "?") for a in d.get("assignees", [])) or "none")
    author = _untrusted.flat((d.get("author") or {}).get("login", "?"))
    iid = d.get("number", number)
    web_url = d.get("url", "")
    body = d.get("body") or ""
    body_total = len(body)



    body, body_withheld = _body.cut(body, desc_max)



    print(_untrusted.banner())
    print(f"# #{iid} {title}")
    print(f"State: {state} | Author: {author}")
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    print(f"Assignees: {assignees}")
    if web_url:
        print(f"URL: {web_url}")
    if body_withheld:


        print(_body.header_notice(body, body_total, body_withheld))

    _print_linked_prs(iid, web_url)



    classify_budget = _classify_render.Budget()


    all_image_urls = _extract_image_urls(body)
    if body:
        print(f"\n## Description\n{_untrusted.fence(body)}")
        if body_withheld:
            print(f"\n{_body.cut_notice(body_withheld)}")
        print(classify_budget.line(body, level=_CLASSIFY_LEVEL))





    comments = d.get("comments", [])
    shown, gap_hidden = ((list(comments), 0) if full
                         else _body.comment_window(comments))
    print(f"\n{_body.comments_heading(len(shown), len(comments))}")
    for position, comment in enumerate(shown):
        if gap_hidden and position == _body.COMMENT_HEAD:
            print(f"\n{_body.comments_gap_notice(gap_hidden)}")
        c_author = _untrusted.flat((comment.get("author") or {}).get("login", "?"))
        c_body = comment.get("body") or ""





        c_trunc = ""
        if comment_max is not None and len(c_body) > comment_max:
            c_body = c_body[:comment_max]
            c_trunc = _body.comment_cut_notice(comment_max)
        c_created = (comment.get("createdAt") or "")[:10]
        print(f"\n**{c_author}** ({c_created}):")
        print(_untrusted.fence(c_body))
        if c_trunc:
            print(c_trunc)
        print(classify_budget.line(c_body, level=_CLASSIFY_LEVEL))
        all_image_urls.extend(_extract_image_urls(comment.get("body") or ""))


    if all_image_urls:
        all_image_urls = list(dict.fromkeys(all_image_urls))
        _print_images(_download_images(all_image_urls, str(iid)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
