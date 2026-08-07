#!/usr/bin/env python3
"""Shared helpers for the git/* preset scripts.

Holds the bits that were drifting across commit.py / push.py:
  - _git            : thin subprocess wrapper
  - _first_error_line: pick the salient line out of git/hook output
  - query_open_mr_result : open MR/PR for a branch, or why that is unknown
  - use_utf8_stdout : stop the ✓/✗ glyphs crashing a cp1252 console

Each script formats the lookup's output its own way — the lookup itself
(glab → gh fallback) lives here once, and since #948 it reports which of
three things happened rather than returning the same `None` for "no MR"
and "the lookup did not happen".
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Optional

# Sibling import: `_env` lives one directory up. Arranged here rather than at
# each call site, so that importing this module is enough to get the knob —
# `presets/git/checkout.py` and five others had no `SUPERTOOL_GIT_TIMEOUT`
# override at all, purely because each would have had to set up its own path.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from _env import env_int  # noqa: E402  (the one numeric-knob reader)


def use_utf8_stdout() -> None:
    """Force UTF-8 on this process's stdout/stderr.

    Windows stdout defaults to cp1252, which cannot encode the ✓/✗/⚠ glyphs
    these scripts print — writing one kills the process with
    UnicodeEncodeError, so a commit that actually succeeded reports as a
    crash. supertool.py reconfigures its own streams, but each preset runs as
    a separate process and does not inherit that. diff.py carried this inline
    from issue #308; it lives here so the rest do not each need a copy.

    A stream without ``reconfigure`` (wrapped or replaced, as under pytest's
    capture) is left alone rather than treated as an error.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


#: Budget for one git call when the call site does not name its own.
#:
#: The ten copies this module replaces did not agree: 5 in `status.py`, 10 in
#: six presets, 30 here and in `merge.py`. No test pinned any of them and no
#: two were chosen together, so consolidating had to pick one. 10 is the value
#: six of the ten already used, and it is the only one that was ever reachable
#: from the environment. The three calls that genuinely need longer — the push,
#: the merge, the commit that runs a hook suite — now say so at the call site,
#: which is where a budget of 300s is legible and a module default is not.
_GIT_TIMEOUT_DEFAULT = 10

#: Shell convention for "killed by a timeout" (coreutils `timeout`). Distinct
#: from any exit code git itself produces, so a caller checking
#: `returncode != 0` keeps working while one that wants to tell a stall from a
#: failure can. `status.py`, `conflicts.py` and `resolve.py` had each defined
#: this constant separately, with the same value and the same comment.
TIMEOUT_RC = 124


def git_timeout(default: int | None = None) -> int:
    """Default budget for a git call, overridable per environment (#650).

    Same shape and same reasoning as `SUPERTOOL_LINT_TIMEOUT` (#553): a loaded
    runner occasionally needs room without a code change, and what supertool
    ships with does not move for it.
    """
    base = _GIT_TIMEOUT_DEFAULT if default is None else default
    return env_int("SUPERTOOL_GIT_TIMEOUT", base, minimum=1)


def _git(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command; a call that does not answer says so (#650, #704).

    `TimeoutExpired` is not allowed to escape. It is not swallowed either: the
    result carries `TIMEOUT_RC`, so every call site's existing
    `returncode != 0` branch behaves exactly as it would for a git that failed,
    while a caller that needs to tell a stall from a failure still can. The one
    thing that must never happen is rendering it as a success — an empty
    `diff --diff-filter=U` reads as "no conflicts", which is the sentence
    `git-conflicts` printed over live `<<<<<<<` markers until #703.

    **The argument wins; the environment sets the default.** A call site that
    names its own budget is making a statement about that call — `git-push`
    gives its push 300s because it owns the timeout and must verify the remote
    before reporting — and a `SUPERTOOL_GIT_TIMEOUT` set to shorten the
    courtesy calls in `git-status` must not silently cap it. `status.py` had
    the reverse precedence; it was reachable only through its own default, so
    nothing depended on it.
    """
    budget = git_timeout() if timeout is None else timeout
    cmd = ["git"] + args
    try:
        return subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=budget, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=f"timed out after {budget}s",
        )


def reject_fetch_option(remote: str, ref: str) -> str:
    """Non-empty error when a fetch (remote, ref) pair would smuggle an option.

    Both values land as bare argv elements in `git fetch <remote> <ref>`, and
    git parses any element beginning with '-' as an option before it reaches
    the refspec grammar. `--upload-pack=<cmd>` is the weaponised case: git
    *executes* <cmd> on fetch (proven on 2.46.2, #818) — unlike `ls-remote`,
    where the same value does not run.

    The dangerous values do not come from operator argv (checkout.py / merge.py
    already refuse a leading-dash argv REF, #150). They come from `@{upstream}`
    split into (remote, ref) — i.e. a remote-tracking ref name, which anyone who
    controls the remote can choose. A real remote or branch never starts with
    '-'; git refuses to create one. So a leading-dash value here is never a
    legitimate ref — it is an option in ref position, and it is refused by name
    rather than fetched silently (a dropped ref would fetch the wrong thing).

    Returns "" when the pair is safe. Callers decide loudness: a mutation
    (push) aborts; a refresh (merge) falls back to the local ref.
    """
    for label, val in (("remote", remote), ("ref", ref)):
        if val.startswith("-"):
            return (f"{label} {val!r} looks like a git option, not a {label} — "
                    f"a tracking ref named like `--upload-pack=…` executes a "
                    f"command on fetch (#818); refusing")
    return ""


def _list_conflicts() -> tuple[list[str], str]:
    """`(paths, why_unavailable)` — three states, not two (#650).

    `([...], "")` git answered and named conflicts; `([], "")` git answered and
    the tree is clean; `([], why)` git did not answer.

    The third state used to be the second, in all three copies of this
    function, and `git-conflicts` is the worst place in the tool to make that
    mistake: `main` renders an empty list as `No conflicted files.`, and you
    only run it because you are already stopped mid-merge — so a lookup that
    failed printed the one sentence that says "go ahead and commit". An index
    lock held by a concurrent git is enough to trigger it; no load required
    (docs/validators.md, "Declining instead of guessing").

    `resolve.py` reached the same hazard by a different route — its
    `Remaining: 0` line is followed by `Next: git-commit ...` — and `merge.py`
    was the lowest-stakes of the three. One function now, so the next fix does
    not have to find all three.
    """
    res = _git(["diff", "--name-only", "--diff-filter=U"])
    if res.returncode != 0:
        return [], (res.stderr.strip() or f"git exited {res.returncode}")
    return [l for l in res.stdout.splitlines() if l.strip()], ""


def repo_label() -> str:
    """Absolute path of the repo the calling op is acting on.

    `git-diff` has printed a `Repo:` line for a long time; `git-commit` and
    `git-push` did not — so the two ops that WRITE were the two that never said
    where they wrote. When a commit lands somewhere unexpected, that line is
    the difference between noticing within the minute and noticing next week
    (#692).

    The work tree when there is one, the git dir otherwise: a bare repo has no
    top level, and printing an empty string there would be worse than printing
    nothing. "unknown" rather than a guess when git answers neither — a wrong
    repo name is the one output worse than no repo name.
    """
    top = _git(["rev-parse", "--show-toplevel"])
    if top.returncode == 0 and top.stdout.strip():
        return top.stdout.strip()
    bare = _git(["rev-parse", "--absolute-git-dir"])
    if bare.returncode == 0 and bare.stdout.strip():
        return f"{bare.stdout.strip()} (bare)"
    return "unknown"


def _looks_like_success(line: str) -> bool:
    """True for lines that report success — must never be picked as an error.

    Pre-push / pre-receive hooks that auto-format print green '✅ … 0 errors.'
    lines and 'pushed successfully' notices; the substrings 'errors' /
    'fatal' would otherwise match the error scan and surface a success line
    as the "first error".
    """
    s = line.strip()
    if not s:
        return False
    low = s.lower()
    has_success = ("✅" in s or "✓" in s or any(m in low for m in (
        "0 errors", "no errors", "pushed successfully", "successfully pushed")))
    if not has_success:
        return False
    # A success marker doesn't win if the same line also carries a hard error
    # signal (e.g. 'lint ✓ — push blocked: error: …'). 'error:' (with colon)
    # avoids matching the '0 errors' / 'no errors' success phrases.
    has_error = any(k in low for k in (
        "error:", "fatal", "rejected", "aborted", "failed", "declined")) \
        or "! [" in s or "❌" in s
    return not has_error


def _first_error_line(text: str) -> str:
    """First line mentioning an error/rejection, else last non-empty line.

    Skips success lines (green ✅, '0 errors', 'pushed successfully') so a
    hook's success banner is never misreported as the failure cause.
    """
    lines = text.splitlines()
    for line in lines:
        s = line.strip()
        if not s or _looks_like_success(s):
            continue
        low = s.lower()
        if ("error" in low or "fatal" in low or "rejected" in low
                or "aborted" in low or "failed" in low
                or "! [" in s or "❌" in s):
            return s
    for line in reversed(lines):
        s = line.strip()
        if s and not _looks_like_success(s):
            return s
    return ""


#: One page of open PRs is enough for every worktree a fleet realistically
#: holds, and the cap is remembered so a page that *hit* it can decline for the
#: branches it did not name (see `PrIndex.truncated`).
PR_INDEX_LIMIT = 100

#: The batched lookup is one network call on an otherwise local op, so it gets
#: a short budget. A slow answer is `unknown`, never "no PR".
PR_INDEX_TIMEOUT = 8

#: Fields the index needs. `statusCheckRollup` rides the same call, which is
#: what makes the tally free — the alternative was a per-PR fetch, i.e. the N
#: calls this function exists to avoid.
PR_INDEX_FIELDS = ("number,headRefName,baseRefName,isDraft,mergeable,"
                   "statusCheckRollup,url")


class PrIndex:
    """Open PRs keyed by head branch — or a stated reason for not knowing.

    `by_branch is None` is the whole point of the class. `query_open_mr` below
    returns `None` for *both* "there is no PR" and "the lookup never ran", and
    a caller cannot tell those apart — which is fine for an advisory line under
    a push and wrong for a board somebody reads to decide where to work. Here
    the two are different objects:

    * `PrIndex({...})`      — GitHub answered; a branch absent from the map has
      no open PR, and that is a fact about the world.
    * `PrIndex(None, why)`  — GitHub did not answer; nothing at all is known
      about any branch, and `why` says what stopped it.

    `truncated` is the third case and the sneaky one: the page came back but
    hit its own limit, so it is authoritative for the branches it *names* and
    establishes nothing about the ones it does not.
    """

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
        """The open PR for `branch`, or None. Only meaningful when `answered`."""
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


def query_open_prs_by_branch(limit: int = PR_INDEX_LIMIT, runner=None) -> PrIndex:
    """Every open PR of this repo, keyed by head branch, in **one** call.

    N worktrees must not mean N lookups: `gh pr list` already returns the head
    ref of every open PR, so the join is a dict build, not a fan-out. The
    caller passes a `runner` in tests; the default shells out to `gh`.

    Every failure route ends in `PrIndex(None, reason)` — a missing binary, a
    non-GitHub remote, an expired token, a rate limit, a timeout, output that
    is not the JSON array it should be. None of them return an empty map,
    because an empty map is a claim.
    """
    run = runner or _run_gh_pr_list
    args = ["pr", "list", "--state", "open", "--json", PR_INDEX_FIELDS,
            "--limit", str(limit)] + _repo_target_args()
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
    """`--repo OWNER/NAME` when a repo target is set, else nothing (#673).

    Imported lazily: `_git_common` is loaded by presets that have no business
    depending on the gh family, and an import error there must not break a
    commit.
    """
    try:
        import _repo_target  # noqa: PLC0415  (deliberately lazy — see docstring)
    except ImportError:
        return []
    return _repo_target.gh_args()


#: Budget for one branch→MR/PR lookup. Advisory output hanging off an
#: otherwise local op, so it stays short — but since #948 a lookup that
#: outlasts it is `unknown`, never "no MR".
MR_LOOKUP_TIMEOUT = 5

#: What `glab` / `gh` say when they mean "there is no such request here". That
#: is an answer about the world, and the branch has no MR — rendered as
#: silence, because a branch with no MR yet is the most ordinary state a branch
#: can be in. Both CLIs exit non-zero for this *and* for an expired token, so
#: the exit code cannot tell them apart and the sentence has to.
NO_REQUEST_PHRASES = (
    "no open merge request",
    "no merge request",
    "no pull request",
)

#: The other host's CLI, in a repo that is not on its host: a GitLab repo runs
#: `gh` too, and a GitHub repo runs `glab`. Neither an answer about this branch
#: nor a failure worth recording — structural and permanent, so counting it as
#: "could not answer" would put a decline on every push of every GitLab repo
#: with `gh` installed, and a line that appears on every call stops being read.
NOT_THIS_HOST_PHRASES = (
    "none of the git remotes",
    "no git remotes found",
    "no remotes found",
)

#: The union, under the name `presets/git/status.py` gave it. That module had
#: the only copy of these phrases and now imports them (#948) — the same rule
#: about the same two CLIs, in one place, because a second copy beside the real
#: one is what this repo's issue tracker is largely made of.
ANSWERED_NONE = NO_REQUEST_PHRASES + NOT_THIS_HOST_PHRASES


class MrLookup:
    """The open MR/PR for a branch — or a stated reason for not knowing (#948).

    `query_open_mr` used to return `None` for *both* "there is no open MR/PR
    for this branch" and "the lookup did not happen", and swallowed every
    exception on the way. Its callers are `git-push` and `git-commit`, so the
    ambiguity was resolved — as an absence, every time — at the moment somebody
    reads the output to decide whether to open a request. `git-push` then told
    a caller who asked for `:watch` that the branch has no MR/PR *yet* and to
    open one, out of a lookup that never completed.

    Two objects, not one value:

    * `MrLookup(mr)` / `MrLookup(None)` — a CLI answered. `mr` is the request,
      or `None` meaning there genuinely is none.
    * `MrLookup(None, why)` — nothing answered, and `why` says what stopped it.
      Nothing at all is known about this branch.

    Deliberately *not* a blocking condition. A tracker that cannot be reached
    is not a reason to refuse to publish work — the receipt degrades to a
    stated unknown and the push proceeds (see `push.py::_mr_unknown_line`).
    """

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
    """(state, why) for a CLI that exited non-zero — the three-way read.

    `answered` — it said there is no such request. `n/a` — it said this repo is
    not on its host, which establishes nothing and never will. `failed` —
    anything else, including every authentication and network error, carried
    with the CLI's own first line so the reader is not sent to fix the wrong
    thing.
    """
    said = ((res.stderr or "") + (res.stdout or "")).lower()
    if any(p in said for p in NOT_THIS_HOST_PHRASES):
        return "n/a", ""
    if any(p in said for p in NO_REQUEST_PHRASES):
        return "answered", ""
    blob = (res.stderr or "") + chr(10) + (res.stdout or "")
    return "failed", _first_error_line(blob) or f"exited {res.returncode}"


def _probe_open_request(argv: list, parse) -> tuple:
    """One CLI list call. Returns (mr, state, why) — never raises.

    Every route that used to fall into a bare `except … : pass` now names
    itself. The exceptions are still caught — this is advisory output and must
    not take a push receipt with it — but catching is not the same as
    discarding what was caught.
    """
    tool = argv[0]
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=MR_LOOKUP_TIMEOUT, encoding="utf-8",
                             errors="replace")
    except subprocess.TimeoutExpired:
        return None, "failed", f"`{tool}` timed out after {MR_LOOKUP_TIMEOUT}s"
    except FileNotFoundError:
        # Vanished between `which` and here. Nothing on this machine was going
        # to answer through it, so it is the not-installed case, not a failure.
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
    if not isinstance(rows[0], dict):
        return None, "failed", f"`{tool}` answered with an entry that is not an object"
    return parse(rows[0]), "answered", ""


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
    # gh: mergeable is CONFLICTING / MERGEABLE / UNKNOWN
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


def query_open_mr_result(branch: str) -> MrLookup:
    """Open MR/PR for `branch` as an `MrLookup` — three states, not two (#948).

    Returns {source, iid, target, pipeline, pipeline_id, pipeline_url,
    merge_status} in `.mr` when a request was found. `pipeline` is the GitLab
    pipeline status when known, else None (gh list carries no cheap check
    state). `merge_status` is the server's view of mergeability
    ('can_be_merged' / 'cannot_be_merged' / None). The extra fields ride the
    same call — no added round-trip — and are best-effort: absent on a glab
    version that doesn't emit them. Tries glab (GitLab) first, falls back to
    gh (GitHub).

    **An answer already given is never downgraded by the fallback.** glab
    saying "no MR" on a GitLab repo is a fact; `gh` failing a moment later
    because the repo is not on GitHub says nothing about it. Getting that
    backwards would decline on every push of every GitLab repo with `gh`
    installed — the loud bug traded for the quiet one, in the direction that
    makes the disclosure worthless.
    """
    if not branch or branch == "HEAD":
        # A detached HEAD has no branch for a request to be open against. That
        # is a fact about the repository, not a lookup that failed.
        return MrLookup(None)
    probes = []
    if shutil.which("glab"):
        # No `--state opened`: glab has no such flag (1.86 exits 1 with
        # `Unknown flag: --state.`) and open is `mr list`'s default anyway —
        # `--closed` is the opt-out. This arm therefore failed at argument
        # parsing on every call, and the swallowed exit code meant nobody
        # found out for as long as the fallback to `gh` also said nothing
        # (#948). Found by the disclosure above, on its first run.
        probes.append((
            ["glab", "mr", "list", "--source-branch", branch,
             "--output", "json"], _glab_fields))
    if shutil.which("gh"):
        probes.append((
            ["gh", "pr", "list", "--head", branch, "--state", "open",
             "--json", "number,baseRefName,mergeable", "--limit", "1"],
            _gh_fields))
    if not probes:
        return MrLookup(None, "neither `glab` nor `gh` is installed, so no "
                              "tracker can be read from here")
    answered = False
    reasons: list = []
    for argv, parse in probes:
        mr, state, why = _probe_open_request(argv, parse)
        if mr is not None:
            return MrLookup(mr)
        if state == "answered":
            answered = True
        elif state == "failed" and why:
            reasons.append(why)
    if answered or not reasons:
        # `not reasons` is the all-`n/a` case: every CLI present said this repo
        # is not on its host, so there is no tracker on which a request could
        # exist. That is an answer, and the same one status.py gives it.
        return MrLookup(None)
    return MrLookup(None, "; ".join(reasons))


def query_open_mr(branch: str) -> Optional[dict]:
    """The request itself, discarding why it might be unknown.

    Kept for `git-commit`'s post-commit hint, which appends `(!42)` to a line
    when there is a request and says nothing when there is not — a caller with
    no third thing to render. Anything that shows the reader a verdict should
    call `query_open_mr_result` and disclose the unknown.
    """
    return query_open_mr_result(branch).mr
