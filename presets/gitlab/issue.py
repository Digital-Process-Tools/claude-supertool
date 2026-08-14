#!/usr/bin/env python3
"""GitLab issue details via glab CLI.

Fetches issue metadata, human comments, related MRs, and downloads
any images found in description/comments to a local temp directory.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)

import _body  # noqa: E402  (the one body cap + disclosure — #698)
import _checks  # noqa: E402  (the shared CI vocabulary, incl. NO_PIPELINE — #815)
import _image_root  # noqa: E402  (the attachment root, created and proven ours — #1493)
import _repo_target  # noqa: E402  (the project this call is about, if not cwd's — #676)
import _secrets  # noqa: E402  (the one GitLab token-prefix list — #1645)
import _untrusted  # noqa: E402  (the fence around tracker text — #694)

DESCRIPTION_MAX = 3000
# Related MRs are listed, not summarised, so the list is capped. A *count* cut
# it — the total was always printed correctly above a short list, which reads as
# the numbers being wrong rather than as a ceiling being hit (#635).
RELATED_MRS_MAX = 10
COMMENT_MAX = 1000
# Not a literal, and not shared. `/tmp/supertool-images` was a fixed name in a
# world-writable directory — any local user could take it first, as a directory
# of their own or as a symlink — and it was POSIX-only besides (#1493). The root
# is per-user, under the platform temp directory, and `_image_root.ensure` is
# what establishes it before anything is written; see that module for the
# trade-off against a per-invocation `mkdtemp()`.
IMAGE_DIR = _image_root.default_root()


def _glab(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a glab command and return the result.

    `-R` is appended here rather than at each call site so a subcommand added
    later cannot forget it and read the cwd's project under a target (#676).
    """
    return subprocess.run(
        ["glab"] + args + _repo_target.gl_args(),
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "404" in s or "not found" in s or "could not resolve" in s:
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.gl_not_found_hint()}")
    # `_secrets.mentions_gitlab_token`, not a literal: this line read `glpat_`
    # until #1645, GitLab mints `glpat-`, and the only test over it used the
    # same wrong spelling. One list, cited to GitLab's docs, in one file.
    if ("401" in s or "unauthorized" in s or "authenticate" in s
            or "bad token" in s or "token expired" in s
            or _secrets.mentions_gitlab_token(s)):
        return "ERROR: glab not authenticated. Run: glab auth login"
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."
    # `glab` echoes the GitLab API's own error body here, so the writer of this
    # text is the remote host — flattened, never relayed raw (#1485). The shape
    # is `presets/gitlab/api.py`'s `classify_error`.
    return (f"ERROR: glab failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _glab_api(endpoint: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a glab api call, against the targeted project when there is one.

    `glab api` has no repo flag — the project is a path segment — so the
    target is substituted into `projects/:id` on the way through (#676).
    """
    return subprocess.run(
        ["glab", "api", _repo_target.gl_api_path(endpoint)],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _pipeline_line(mr: dict) -> str:
    """One MR's pipeline verdict — three states, never two (#815).

    #815 was filed against `gh-issue` printing a linked PR's state and nothing
    about its CI; its second comment verified the identical gap here, and asks
    for both families in one change or the fix becomes the parity gap it was
    meant to close.

    **A status, not a leg tally, and that asymmetry is deliberate.**
    `head_pipeline` is already in the payload this op fetches — verified live
    on 2026-08-07 against `projects/:id/issues/12634/related_merge_requests`,
    which returns the MR *detail* representation. GitLab puts no per-job
    breakdown in it, so matching `gh-issue`'s `13 passed, 1 failed` shape here
    would mean `projects/:id/pipelines/N/jobs` once per related MR. That is a
    real, uncapped, per-MR request on a list that can hold ten of them —
    exactly the cost #815 says not to incur without saying so. A status GitLab
    already handed us beats a tally bought at N requests; `gl-mr:!N` is one
    call away for the reader who wants the legs.

    A missing or null `head_pipeline` declines rather than reporting an
    absence. GitLab creates a pipeline at push time, so "no pipeline" is
    equally "no job matched this ref" and "the head was just pushed" and "this
    payload does not carry it" — `_checks.NO_PIPELINE` is the sentence this
    repo already settled on for that, and reusing it is what keeps `gl-mr` and
    `gl-issue` from drifting into two different words for one state.
    """
    pipeline = mr.get("head_pipeline")
    status = pipeline.get("status") if isinstance(pipeline, dict) else None
    if not status:
        return f"pipeline: {_checks.NO_PIPELINE}"
    pid = pipeline.get("id")
    marker = "" if _checks.bucket(status) == "passed" else f" {_checks.NOT_GREEN}"
    ref = f" (#{pid})" if pid else ""
    return f"pipeline: {_untrusted.flat(str(status))}{ref}{marker}"


def _related_mrs_unknown(reason: str) -> None:
    """Say the lookup could not be answered, rather than printing nothing.

    **This is a second defect, on the same lines, and it is worse than the one
    #815 was filed about.** `if mr_result.returncode == 0:` had no `else` and
    the enclosing `except (TimeoutExpired, JSONDecodeError): pass` swallowed
    the rest, so a failed related-MR query printed *no section at all* — and a
    missing `Related MRs` line reads as "this issue has no MRs", which is the
    signal that an issue is unclaimed and the action that invites is
    delegating work already done. That is #780 item 1, fixed on the GitHub
    side, still live here; GitHub at least printed `Linked PRs: unknown`.

    #815's second comment states that neither family omits the section. That
    is true at *zero*, which is what was tested there. It was not true on
    failure.
    """
    print(f"{chr(10)}Related MRs: unknown — could not query "
          f"({_untrusted.flat(reason) or 'no detail from glab'})")


def _print_related_mrs(iid: object, full: bool) -> None:
    """Render the related-MR section, pipeline included."""
    try:
        mr_result = _glab_api(
            f"projects/:id/issues/{iid}/related_merge_requests"
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _related_mrs_unknown(f"glab api failed: {type(exc).__name__}")
        return

    if mr_result.returncode != 0:
        # `split_lines`, not `str.splitlines()` (#1654). Both cut the string;
        # only one of them lets the writer choose which cut survives. A U+2028
        # inside glab's one line of stderr makes `str.splitlines()` take `[0]`
        # from before it and discard everything after — so a body reading
        # `nothing wrong here<U+2028>403 forbidden, list INCOMPLETE` reaches
        # the reader as `nothing wrong here`. `split_lines` cuts on LF/CR/CRLF
        # only, so the line is the line, and `_related_mrs_unknown` already
        # flattens it, which is what spells the separator as `[U+2028]`.
        reason = _untrusted.split_lines((mr_result.stderr or "").strip())
        _related_mrs_unknown(reason[0] if reason else "glab api exited non-zero")
        return

    try:
        mrs = json.loads(mr_result.stdout)
    except json.JSONDecodeError:
        _related_mrs_unknown("glab api returned output that is not JSON")
        return

    if not isinstance(mrs, list):
        _related_mrs_unknown("glab api returned an unexpected shape")
        return

    if not mrs:
        print(f"{chr(10)}Related MRs: none")
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


def _extract_image_urls(text: str) -> list[str]:
    """Extract GitLab upload image URLs from markdown text.

    Matches:
    - ![alt](/uploads/SECRET/filename.png)
    - ![alt](https://gitlab.example.com/user/repo/uploads/SECRET/filename.png)
    """
    patterns = [
        r'!\[[^\]]*\]\((/uploads/[^\)]+)\)',
        r'!\[[^\]]*\]\((https?://[^\)]*?/uploads/[^\)]+)\)',
    ]
    urls: list[str] = []
    for pattern in patterns:
        urls.extend(re.findall(pattern, text))
    return urls


def _is_inside(candidate: str, directory: str) -> bool:
    """True when `candidate` really resolves inside `directory`.

    Compared after realpath on both sides, and with a trailing separator on the
    directory so a sibling like `/tmp/images-other` cannot pass as `/tmp/images`.

    **What this establishes, and what it does not (#1493).** Realpathing both
    sides is right for the *leaf*: it is what makes a `..` or a symlink in the
    remote-chosen name resolve to where the write would actually land, and be
    compared against where the root actually is. It establishes nothing whatever
    about `directory` being a directory anyone should write into — a symlink
    planted at the root itself is resolved through on *both* sides, so this
    answers `True` about the attacker's directory exactly as readily as about
    ours. It is a containment test, not an ownership test, and it never was one.
    `_image_root.ensure` is what establishes the root; every call here is
    against a root that came back from it.

    The implementation moved to `_image_root.is_inside` when `gh-issue` needed
    the same test (#1506). This name stays because it is what this module's call
    sites and its tests use.
    """
    return _image_root.is_inside(candidate, directory)


def _download_images(image_urls: list[str], issue_number: str) -> list[str]:
    """Download GitLab upload images to local temp directory.

    Returns list of local file paths for successfully downloaded images.

    **`issue_number` is remote input.** It is `iid` from the `glab issue view`
    reply, so the *directory this writes into* was chosen by the server. Two
    things follow, and #1484 was both of them at once:

    - The refusal has to come **before** `os.makedirs`. A directory created is
      already a write, and no later guard can un-create it. `../escaped` gave
      `realpath` `/private/tmp/audit_pwned` from an `IMAGE_DIR` of
      `/tmp/supertool-images`; an absolute `iid` makes `os.path.join` discard
      the root outright.
    - The containment check is anchored to the root, never to `out_dir`, which is
      derived from the value being constrained. That was the defect:
      `_is_inside(local_path, out_dir)` answered `True` about a directory that
      had already escaped. `_is_inside` was correct; it was asked the wrong
      question. Same shape as #1246, one layer down.

    **And the root is established before either of those, because a boundary
    nobody owns is not a boundary (#1493).** `IMAGE_DIR` was a fixed name in a
    world-writable `/tmp`, and `_is_inside` realpaths both sides — so a symlink
    planted at the root got resolved through twice and containment approved a
    directory belonging to whoever planted it. `_image_root.ensure` returns a
    root this process created and can prove it owns, or a reason it could not;
    every check below is against what it returned, not against the constant.
    """
    if not image_urls:
        return []

    # Numeric, by the whole string — a GitLab iid is a positive integer and
    # nothing else. `str.isdigit()` is not this test: it accepts Arabic-Indic
    # and other Unicode digits, which are not what any path this builds means.
    if not re.fullmatch(r"[0-9]+", str(issue_number)):
        print(f"note: skipped {len(image_urls)} attachment(s) — the issue id "
              f"{_untrusted.flat(str(issue_number))!r} from the API reply is "
              "not numeric, so no download directory was chosen")
        return []

    # The root, before the id is joined onto anything. `_is_inside` compares two
    # resolved paths and cannot tell whose directory the root is; establishing
    # that is a separate question and it is asked here (#1493).
    root, why = _image_root.ensure(IMAGE_DIR)
    if root is None:
        print(f"note: skipped {len(image_urls)} attachment(s) — no attachment "
              f"root this process owns could be established: {why}")
        return []

    out_dir = os.path.join(root, str(issue_number))
    if not _is_inside(out_dir, root):
        # Unreachable through the check above, and kept anyway: it is the arm
        # that does not depend on the id having been validated, so it still
        # holds if the derivation moves.
        print(f"note: skipped {len(image_urls)} attachment(s) — the download "
              f"directory did not resolve inside {root}")
        return []
    # The per-issue directory goes through the same establishment as the root,
    # rather than an `os.makedirs(exist_ok=True)` that accepts whatever is on the
    # name. Only we can plant something inside a 0700 root we own — but a root an
    # *earlier*, looser run left behind could have had a link planted in it
    # before this call tightened it, and `os.path.islink` does not see a Windows
    # junction. `ensure` answers both, on both platforms, in one spelling.
    out_dir, why = _image_root.ensure(out_dir)
    if out_dir is None:
        print(f"note: skipped {len(image_urls)} attachment(s) — the per-issue "
              f"directory could not be established: {why}")
        return []

    downloaded: list[str] = []
    for url in image_urls:
        # Extract the /uploads/SECRET/FILENAME part
        match = re.search(r'(/uploads/[^\s\)]+)', url)
        if not match:
            continue

        upload_path = match.group(1)
        # Decode BEFORE taking the basename. The remote name is percent-encoded,
        # so `basename` on the encoded form sees one segment and leaves any
        # encoded separators intact — they only become separators afterwards.
        # Decoding first means basename operates on what the name really says.
        local_name = os.path.basename(urllib.parse.unquote(upload_path))
        local_path = os.path.join(out_dir, local_name)
        # And confirm it against the established root — not against `out_dir`,
        # which is derived from the API's `iid` and so cannot be its own
        # boundary (#1484). The `out_dir` arm stays as the tighter of the two.
        if not (_is_inside(local_path, root)
                and _is_inside(local_path, out_dir)):
            print("note: skipped an attachment whose name resolves outside "
                  f"{root}")
            continue

        # Use glab api to download (handles auth automatically)
        # The endpoint is projects/:id/uploads — but glab api with GET
        # on the raw upload path also works
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

    # 1. Fetch issue metadata
    try:
        result = _glab(["issue", "view", number, "--output", "json"])
    except FileNotFoundError:
        print("ERROR: glab not found — install from https://gitlab.com/gitlab-org/cli")
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

    # One-line fields are flattened rather than fenced — see presets/_untrusted.py.
    title = _untrusted.flat(d.get("title", "?"))
    state = d.get("state", "?")
    labels = _untrusted.flat(", ".join(d.get("labels", [])) or "none")
    milestone = _untrusted.flat((d.get("milestone") or {}).get("title", "none"))
    assignees = _untrusted.flat(", ".join(a.get("username", "?") for a in d.get("assignees", [])) or "none")
    author = _untrusted.flat((d.get("author") or {}).get("username", "?"))
    iid = d.get("iid", number)
    web_url = d.get("web_url", "")
    # GitLab markdown attributes are stripped *before* the cap, not after, so
    # the disclosed counts describe the text actually printed (#698).
    description = re.sub(
        r'\{width=\d+\s+height=\d+\}', '', d.get("description") or ""
    )
    description_total = len(description)
    description, description_withheld = _body.cut(description, desc_max)
    project_id = d.get("project_id", "")

    # Header. The fence convention is declared before the first thing inside a
    # fence — the reader this protects is the one who acts on the first line.
    print(_untrusted.banner())
    print(f"# #{iid} {title}")
    print(f"State: {state} | Author: {author}")
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    print(f"Assignees: {assignees}")
    if web_url:
        print(f"URL: {web_url}")
    if description_withheld:
        # Before the description, not only at the cut — the reader this
        # protects is the one who stops at the top (#681, #698).
        print(_body.header_notice(
            description, description_total, description_withheld))

    # 2. Related MRs, with each one's pipeline (#815)
    #
    # "Related", not "Linked" — and the difference from `gh-issue`'s
    # `Linked PRs:` is deliberate rather than drift (#628). This endpoint is
    # `related_merge_requests`: anything referencing the issue, closing or
    # not. The GitHub twin asks GraphQL `closedByPullRequestsReferences`,
    # which answers "will this close it" — and #780 exists because a PR that
    # merely mentions an issue used to count there. Renaming this heading to
    # match would make the word claim the stronger fact GitLab was never
    # asked for. `/issues/:iid/closed_by` is the endpoint for that question.
    _print_related_mrs(iid, full)

    # 3. Description (markdown attributes already stripped, above the cap)
    if description:
        print(f"\n## Description\n{_untrusted.fence(description)}")
        if description_withheld:
            print(f"\n{_body.cut_notice(description_withheld)}")

    # 4. Fetch comments (notes) — human only
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
                        # The truncation notice is supertool's, so it prints
                        # outside the fence — see the same call in gh-issue.
                        note_trunc = ""
                        if comment_max is not None and len(body) > comment_max:
                            body = body[:comment_max]
                            note_trunc = _body.comment_cut_notice(comment_max)
                        created = (note.get("created_at") or "")[:10]
                        print(f"\n**{note_author}** ({created}):")
                        print(_untrusted.fence(body))
                        if note_trunc:
                            print(note_trunc)
                        # Extract images from comments too
                        all_image_urls.extend(_extract_image_urls(note.get("body") or ""))
                else:
                    print(f"\n## Comments (0 human, {system_count} system skipped)")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # 5. Download images
    if all_image_urls:
        # Deduplicate
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
