#!/usr/bin/env python3













from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from typing import NamedTuple, Optional








_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

import _untrusted  
from _spawnable import which_excluding_cwd  


def use_utf8_stdout() -> None:












    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

















from _git_run import (  
    LOCK_WAIT_DEFAULT,
    TIMEOUT_RC,
    _GIT_TIMEOUT_DEFAULT,
    _LOCK_BACKOFF,
    _LOCK_ERROR_RE,
    _LOCK_WAIT_CEILING,
    _TERM_GRACE_S,
    _diagnose_lock,
    _git,
    _git_attempt,
    _git_verbatim,
    _git_verbatim_attempt,
    _lock_fd_holder,
    _lock_wait_budget,
    _settled,
    _stop,
    _with_lock_retry,
    git_timeout,
)





NOT_A_REPO = "ERROR: not inside a git repository."


def probe_repo(git_fn=None, args: list[str] | None = None) -> tuple[bool | None, str]:








































    run = _git if git_fn is None else git_fn
    res = run(args or ["rev-parse", "--git-dir"])
    if res.returncode == TIMEOUT_RC:
        return None, (_untrusted.flat(res.stderr.strip())
                      or f"git exited {TIMEOUT_RC}")
    if res.returncode != 0:
        return False, ""










    return True, _untrusted.flat(res.stdout.strip())


def unanswered_repo_lines(why: str, probe: str = "git rev-parse --git-dir") -> list[str]:











    return [
        f"ERROR: could not tell whether this is a git repository — "
        f"`{probe}` did not answer ({_untrusted.flat(why)}).",
        "  Nothing was inspected. That is not the same as being outside a "
        "repository, so re-run rather than acting on this.",
    ]


def reject_fetch_option(remote: str, ref: str) -> str:



















    for label, val in (("remote", remote), ("ref", ref)):
        if val.startswith("-"):
            return (f"{label} {val!r} looks like a git option, not a {label} — "
                    f"a tracking ref named like `--upload-pack=…` executes a "
                    f"command on fetch (#818); refusing")
    return ""


def _list_conflicts() -> tuple[list[str], str]:































































    res = _git_verbatim(["diff", "--name-only", "--diff-filter=U", "-z"])
    if res.returncode != 0:
        return [], (res.stderr.strip() or f"git exited {res.returncode}")




    return [p for p in res.stdout.split(chr(0)) if p], ""





FOREIGN_WORKTREE_MARKER = "COPIED WORKTREE"


def _gitfile_target(dot: str) -> Optional[str]:





    try:
        with open(dot, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("gitdir:"):
                    target = line.split(":", 1)[1].strip()
                    if not target:
                        return None
                    if not os.path.isabs(target):
                        target = os.path.join(os.path.dirname(dot), target)
                    return os.path.normpath(target)
    except OSError:
        return None
    return None


def _same_path(a: str, b: str) -> bool:
    return (os.path.normcase(os.path.realpath(a))
            == os.path.normcase(os.path.realpath(b)))


class ForeignWorktree(NamedTuple):










    here_display: str
    registered_display: str


def foreign_worktree(start: Optional[str] = None) -> Optional[ForeignWorktree]:




















    here = os.path.abspath(start if start is not None else os.getcwd())


    while True:
        dot = os.path.join(here, ".git")
        if os.path.exists(dot):
            break
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent
    if not os.path.isfile(dot):
        return None  
    admin = _gitfile_target(dot)
    if admin is None:
        return None
    if os.path.basename(os.path.dirname(admin)) != "worktrees":
        return None  
    try:
        with open(os.path.join(admin, "gitdir"), "r",
                  encoding="utf-8", errors="replace") as handle:
            registered_dot = handle.read().strip()
    except OSError:
        return None
    if not registered_dot or _same_path(registered_dot, dot):
        return None














    return ForeignWorktree(
        _untrusted.flat(here, disclose_newline=True),
        _untrusted.flat(os.path.dirname(registered_dot), disclose_newline=True),
    )


def _with_foreign_note(label: str) -> str:



    found = foreign_worktree()
    if found is None:
        return label
    return f"{label}\n  {foreign_worktree_note(found)}"


def foreign_worktree_note(found: ForeignWorktree) -> str:





    return (f"⚠ {FOREIGN_WORKTREE_MARKER} — this directory is not the one git "
            f"registered for its git directory; the index, HEAD and refs "
            f"reached from here belong to {found[1]} (#1536)")


def repo_label() -> str:



























    top = _git(["rev-parse", "--show-toplevel"])
    if top.returncode == 0 and top.stdout.strip():
        return _with_foreign_note(
            _untrusted.flat(top.stdout.strip(), disclose_newline=True))
    bare = _git(["rev-parse", "--absolute-git-dir"])
    if bare.returncode == 0 and bare.stdout.strip():
        return f"{_untrusted.flat(bare.stdout.strip(), disclose_newline=True)} (bare)"
    return "unknown"


def install_dir() -> str:






    return os.path.dirname(os.path.dirname(_HERE))


def _quoted_interpreter() -> str:











    exe = sys.executable
    if " " not in exe:
        return exe
    return '"' + exe + '"' if os.name == "nt" else shlex.quote(exe)


def _wrapper_is_runnable(path: str) -> bool:





















    if os.name == "nt":
        try:
            with open(path, "rb") as fh:
                return fh.read(2) == b"#!"
        except OSError:
            return False
    return os.access(path, os.X_OK)


def st_hint(arg: str) -> str:



























    root = install_dir()
    wrapper = os.path.join(root, "supertool")
    if os.path.isfile(wrapper) and _wrapper_is_runnable(wrapper):
        return "./supertool " + chr(39) + arg + chr(39)
    if os.path.isfile(os.path.join(root, "supertool.py")):
        return _quoted_interpreter() + " supertool.py " + chr(39) + arg + chr(39)
    return ("(no runnable supertool found in " + root + " — the op is "
            + chr(39) + arg + chr(39) + ")")
















_SUCCESS_DEMOTION_WORD_RE = re.compile(
    r"\b(fatal|rejected|aborted|failed|declined)\b", re.IGNORECASE)


def _looks_like_success(line: str) -> bool:







    s = line.strip()
    if not s:
        return False
    low = s.lower()
    has_success = ("✅" in s or "✓" in s or any(m in low for m in (
        "0 errors", "no errors", "pushed successfully", "successfully pushed")))
    if not has_success:
        return False




    has_error = ("error:" in low or bool(_SUCCESS_DEMOTION_WORD_RE.search(low))
                 or "! [" in s or "❌" in s)
    return not has_error















_ERROR_WORD_RE = re.compile(
    r"\b(errors?|fatal|rejected|aborted|failed)\b", re.IGNORECASE)


def _first_error_line(text: str) -> str:


























    lines = _untrusted.split_lines(text)
    for line in lines:
        s = line.strip()
        if not s or _looks_like_success(s):
            continue
        if _ERROR_WORD_RE.search(s) or "! [" in s or "❌" in s:
            return _untrusted.flat(s)
    for line in reversed(lines):
        s = line.strip()
        if s and not _looks_like_success(s):
            return _untrusted.flat(s)
    return ""








GIT_OUTPUT_HEAD_LINES = 5
GIT_OUTPUT_TAIL_LINES = 30






RELAY_PREFIX = "> "




RELAY_HEADER = "--- git output ---"


def bounded_lines(lines: list[str], head: int = GIT_OUTPUT_HEAD_LINES,
                  tail: int = GIT_OUTPUT_TAIL_LINES) -> list[str]:








    if len(lines) <= head + tail + 1:
        return lines
    return (lines[:head]
            + [f"... {len(lines) - head - tail} line(s) not shown - first "
               f"{head} and last {tail} kept; re-run the push by hand to see "
               "all of it"]
            + lines[-tail:])


def relayed_lines(lines: list[str]) -> list[str]:







































    return [_untrusted.visible(ln, keep=chr(9)) for ln in lines]


def relayed_block(text: str, *, head: int = GIT_OUTPUT_HEAD_LINES,
                  tail: int = GIT_OUTPUT_TAIL_LINES) -> list[str]:

















    lines = relayed_lines(bounded_lines(
        _untrusted.split_lines(text.strip()), head, tail))
    if not lines:
        return [RELAY_HEADER, "(no output)"]
    return [RELAY_HEADER] + [RELAY_PREFIX + ln for ln in lines]





PR_INDEX_LIMIT = 100



PR_INDEX_TIMEOUT = 8




PR_INDEX_FIELDS = ("number,headRefName,baseRefName,isDraft,mergeable,"
                   "statusCheckRollup,url")


class PrIndex:


















    __slots__ = ("by_branch", "reason", "truncated", "limit")

    def __init__(self, by_branch: Optional[dict], reason: str = "",
                 truncated: bool = False, limit: int = PR_INDEX_LIMIT) -> None:
        self.by_branch = by_branch
        self.reason = reason
        self.truncated = truncated
        self.limit = limit

    @property
    def answered(self) -> bool:
        return self.by_branch is not None

    def get(self, branch: str) -> Optional[dict]:

        if not self.by_branch or not branch:
            return None
        return self.by_branch.get(branch)

    def __repr__(self) -> str:
        if self.by_branch is None:
            return f"PrIndex(None, {self.reason!r})"
        return f"PrIndex({len(self.by_branch)} branches, truncated={self.truncated})"


def _run_gh_pr_list(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + args, capture_output=True, text=True,
                          timeout=PR_INDEX_TIMEOUT, encoding="utf-8",
                          errors="replace")






MERGED_PR_FIELDS = "number,headRefName"





MERGED_PR_CHUNK = 30


def query_merged_prs_for_branches(branches, runner=None) -> PrIndex:

























    names = [b for b in dict.fromkeys(branches) if b]
    if not names:
        return PrIndex({}, limit=0)
    found: dict = {}
    last_limit = 0
    for start in range(0, len(names), MERGED_PR_CHUNK):
        chunk = names[start:start + MERGED_PR_CHUNK]



        limit = max(2 * len(chunk), 20)
        last_limit = limit
        idx = _pr_index(
            ["pr", "list", "--state", "merged", "--json", MERGED_PR_FIELDS,
             "--search", " ".join("head:" + b for b in chunk),
             "--limit", str(limit)], limit, runner)
        if not idx.answered:
            return idx
        if idx.truncated:
            return PrIndex(None, f"the merged-PR search hit its {limit}-item "
                           "cap, so its answer is incomplete", limit=limit)
        found.update(idx.by_branch or {})
    return PrIndex(found, limit=last_limit)


def query_open_prs_by_branch(limit: int = PR_INDEX_LIMIT, runner=None) -> PrIndex:











    return _pr_index(["pr", "list", "--state", "open", "--json",
                      PR_INDEX_FIELDS, "--limit", str(limit)], limit, runner)


def _pr_index(args: list, limit: int, runner=None) -> PrIndex:








    run = runner or _run_gh_pr_list
    args = list(args) + _repo_target_args()
    try:
        res = run(args)
    except subprocess.TimeoutExpired:
        return PrIndex(None, f"gh pr list did not answer within {PR_INDEX_TIMEOUT}s",
                       limit=limit)
    except FileNotFoundError:
        return PrIndex(None, "gh is not installed — the tracker cannot be read here",
                       limit=limit)
    except OSError as exc:
        return PrIndex(None, f"gh could not be run ({exc})", limit=limit)

    if res.returncode != 0:
        blob = (res.stderr or "") + chr(10) + (res.stdout or "")
        why = _first_error_line(blob) or f"gh pr list exited {res.returncode}"
        return PrIndex(None, why, limit=limit)
    try:
        prs = json.loads(res.stdout or "")
    except (json.JSONDecodeError, ValueError):
        return PrIndex(None, "gh pr list returned output that is not JSON", limit=limit)
    if not isinstance(prs, list):
        return PrIndex(None, "gh pr list returned JSON that is not a list", limit=limit)

    by_branch: dict = {}
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        head = pr.get("headRefName")
        if isinstance(head, str) and head and head not in by_branch:
            by_branch[head] = pr
    return PrIndex(by_branch, truncated=len(prs) >= limit, limit=limit)


def _repo_target_args() -> list:






    try:
        import _repo_target  
    except ImportError:
        return []
    return _repo_target.gh_args()





MR_LOOKUP_TIMEOUT = 5






NO_REQUEST_PHRASES = (
    "no open merge request",
    "no merge request",
    "no pull request",
)






NOT_THIS_HOST_PHRASES = (
    "none of the git remotes",
    "no git remotes found",
    "no remotes found",
)








NOT_AUTHENTICATED_PHRASES = (
    "gh_token environment variable",
    "glab_token environment variable",
    "auth login",
    "not logged in",
    "no token provided",
)





ANSWERED_NONE = NO_REQUEST_PHRASES + NOT_THIS_HOST_PHRASES


class MrLookup:






















    __slots__ = ("mr", "reason")

    def __init__(self, mr: Optional[dict] = None, reason: str = "") -> None:
        self.mr = mr
        self.reason = reason

    @property
    def answered(self) -> bool:
        return not self.reason

    def __repr__(self) -> str:
        if self.reason:
            return f"MrLookup(unanswered, {self.reason!r})"
        return f"MrLookup({self.mr!r})"


def _cli_verdict(res: subprocess.CompletedProcess) -> tuple:








    said = ((res.stderr or "") + (res.stdout or "")).lower()
    if any(p in said for p in NOT_THIS_HOST_PHRASES):
        return "n/a", ""
    if any(p in said for p in NO_REQUEST_PHRASES):
        return "answered", ""
    if any(p in said for p in NOT_AUTHENTICATED_PHRASES):


        return "failed", ("is not authenticated here — nothing asked the "
                          "tracker anything (`auth login`, or a token in the "
                          "environment)")
    blob = (res.stderr or "") + chr(10) + (res.stdout or "")
    return "failed", _first_error_line(blob) or f"exited {res.returncode}"


def _argv_limit(argv: list) -> Optional[int]:








    try:
        idx = argv.index("--limit")
    except ValueError:
        return None
    try:
        return int(argv[idx + 1])
    except (IndexError, ValueError):
        return None


def _probe_open_request(argv: list, parse) -> tuple:







    tool = argv[0]
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=MR_LOOKUP_TIMEOUT, encoding="utf-8",
                             errors="replace")
    except subprocess.TimeoutExpired:
        return None, "failed", f"`{tool}` timed out after {MR_LOOKUP_TIMEOUT}s"
    except FileNotFoundError:


        return None, "n/a", ""
    except OSError as exc:
        return None, "failed", f"`{tool}` could not be run ({exc})"
    if res.returncode != 0:
        state, why = _cli_verdict(res)
        return None, state, (f"`{tool}` {why}" if why else "")
    body = (res.stdout or "").strip()
    if not body.startswith("["):
        return None, "failed", f"`{tool}` answered with output that is not JSON"
    try:
        rows = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None, "failed", f"`{tool}` answered with output that is not JSON"
    if not isinstance(rows, list):
        return None, "failed", f"`{tool}` answered with JSON that is not a list"
    if not rows:
        return None, "answered", ""









    for row in rows:
        if not isinstance(row, dict):
            return None, "failed", f"`{tool}` answered with an entry that is not an object"
        if row.get("isCrossRepository"):
            continue
        return parse(row), "answered", ""









    limit = _argv_limit(argv)
    if limit is not None and len(rows) >= limit:
        return None, "failed", (
            f"`{tool}` returned {len(rows)} rows, all forks, at its own "
            f"fetch limit ({limit}) -- a same-repo match may exist beyond "
            "the window")
    return None, "answered", ""


def _glab_fields(row: dict) -> dict:
    pipeline = row.get("pipeline") or row.get("head_pipeline") or {}
    if not isinstance(pipeline, dict):
        pipeline = {}
    return {
        "source": "gitlab",
        "iid": row.get("iid") or row.get("number") or "?",
        "target": row.get("target_branch", "?"),
        "pipeline": pipeline.get("status"),
        "pipeline_id": pipeline.get("id"),
        "pipeline_url": pipeline.get("web_url"),
        "merge_status": row.get("detailed_merge_status")
        or row.get("merge_status"),
    }


def _gh_fields(row: dict) -> dict:

    gh_merge = row.get("mergeable")
    return {
        "source": "github",
        "iid": row.get("number", "?"),
        "target": row.get("baseRefName", "?"),
        "pipeline": None,
        "pipeline_id": None,
        "pipeline_url": None,
        "merge_status": "cannot_be_merged" if gh_merge == "CONFLICTING" else None,
    }


def _is_local_remote(url: str) -> bool:







    u = url.strip()
    if not u:
        return False
    if u.startswith("file://"):
        return True
    if u.startswith(("/", "./", "../", "~")) or u[:1] == chr(92):
        return True
    if len(u) > 1 and u[1] == ":" and u[0].isalpha() and u[0].isascii():






        return True
    scheme, sep, rest = u.partition("://")
    if sep and rest and scheme and scheme.replace(
            "+", "").replace(".", "").replace("-", "").isalnum():
        return False  
    if ":" in u.split("/", 1)[0]:
        return False  
    return True  


def _remotes_could_host_a_request() -> tuple[Optional[bool], str]:



















    try:
        res = _git(["remote", "-v"])
    except OSError as exc:





        return None, f"`git remote` could not be run ({exc})"
    if res.returncode != 0:
        return None, (res.stderr.strip() or f"git exited {res.returncode}")
    urls = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            urls.append(parts[1])
    if not urls:
        return False, ""
    return any(not _is_local_remote(u) for u in urls), ""


def query_open_mr_result(branch: str) -> MrLookup:


















    if not branch or branch == "HEAD":


        return MrLookup(None)
    could_host, repo_why = _remotes_could_host_a_request()
    if could_host is False:




        return MrLookup(None)
    probes = []
    glab_bin = which_excluding_cwd("glab")
    if glab_bin:






        probes.append((
            [glab_bin, "mr", "list", "--source-branch", branch,
             "--output", "json"], _glab_fields))
    gh_bin = which_excluding_cwd("gh")
    if gh_bin:



        probes.append((
            [gh_bin, "pr", "list", "--head", branch, "--state", "open",
             "--json", "number,baseRefName,mergeable,isCrossRepository",
             "--limit", "5"],
            _gh_fields))
    if not probes:
        why = ("neither `glab` nor `gh` is installed, so no tracker can be "
               "read from here")
        return MrLookup(None, f"{repo_why}; {why}" if repo_why else why)
    answered = False




    reasons: list = [repo_why] if repo_why else []
    for argv, parse in probes:
        mr, state, why = _probe_open_request(argv, parse)
        if mr is not None:
            return MrLookup(mr)
        if state == "answered":
            answered = True
        elif state == "failed" and why:
            reasons.append(why)
    if answered or not reasons:



        return MrLookup(None)
    return MrLookup(None, "; ".join(reasons))


def query_open_mr(branch: str) -> Optional[dict]:







    return query_open_mr_result(branch).mr

def _glab_last_fields(row: dict) -> dict:






    return {
        "source": "gitlab",
        "iid": row.get("iid") or row.get("number") or "?",
        "target": row.get("target_branch", "?"),
        "state": row.get("state"),
        "merged_at": row.get("merged_at"),
        "closed_at": row.get("closed_at"),
    }


def _gh_last_fields(row: dict) -> dict:







    state = row.get("state")
    return {
        "source": "github",
        "iid": row.get("number", "?"),
        "target": row.get("baseRefName", "?"),
        "state": state.lower() if isinstance(state, str) else state,
        "merged_at": row.get("mergedAt"),
        "closed_at": row.get("closedAt"),
    }


def query_last_mr_result(branch: str) -> MrLookup:

























    if not branch or branch == "HEAD":
        return MrLookup(None)
    could_host, repo_why = _remotes_could_host_a_request()
    if could_host is False:
        return MrLookup(None)
    probes = []
    glab_bin = which_excluding_cwd("glab")
    if glab_bin:
        probes.append((
            [glab_bin, "mr", "list", "--source-branch", branch, "--all",
             "--order", "updated_at", "--sort", "desc", "--per-page", "1",
             "--output", "json"], _glab_last_fields))
    gh_bin = which_excluding_cwd("gh")
    if gh_bin:



        probes.append((
            [gh_bin, "pr", "list", "--head", branch, "--state", "all",
             "--json",
             "number,baseRefName,state,mergedAt,closedAt,isCrossRepository",
             "--limit", "5"], _gh_last_fields))
    if not probes:
        why = ("neither `glab` nor `gh` is installed, so no tracker can be "
               "read from here")
        return MrLookup(None, f"{repo_why}; {why}" if repo_why else why)
    answered = False
    reasons: list = [repo_why] if repo_why else []
    for argv, parse in probes:
        mr, state, why = _probe_open_request(argv, parse)
        if mr is not None:
            return MrLookup(mr)
        if state == "answered":
            answered = True
        elif state == "failed" and why:
            reasons.append(why)
    if answered or not reasons:
        return MrLookup(None)
    return MrLookup(None, "; ".join(reasons))
