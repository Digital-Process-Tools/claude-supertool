#!/usr/bin/env python3












































from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _console import use_utf8_stdout  
import _checks  
import _declared_legs  
import _git_run  
import _publish_safety  
import _refname  
import _repo_target  
import _untrusted  
import _digits  


def _load_pr_module():







    from importlib import util as _util
    spec = _util.spec_from_file_location(
        "_github_pr_for_merge", Path(__file__).resolve().parent / "pr.py")
    assert spec is not None and spec.loader is not None
    mod = _util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MERGE_METHODS = ("squash", "merge", "rebase")

UNKNOWN = "unknown"
ALL_CLOSED = "all closed"
NOT_CLOSED = "not closed"
NONE_DECLARED = "none declared"

MERGED = "merged"
UNVERIFIED = "unverified"








STACK_FOUND = "found"
STACK_NONE = "none"






_MERGE_STATE_OK = frozenset({"CLEAN", "HAS_HOOKS"})






_PR_FIELDS = (
    "number,title,state,isDraft,mergeable,mergeStateStatus,reviewDecision,"
    "baseRefName,headRefName,headRefOid,isCrossRepository,url,body,"
    "statusCheckRollup"
)






def gate(pr: dict, declared: int | None = None,
         missing: Sequence[str] = (),
         reason: str = "") -> tuple[bool, List[str]]:




















    lines: List[str] = []
    num = pr.get("number", "?")

    state = _checks.normalize(pr.get("state"))
    if state != "OPEN":
        return (False, [
            f"REFUSED: PR #{num} is {state}, not OPEN — there is nothing to "
            f"merge. Read it with `gh-pr:{num}`.",
        ])

    if pr.get("isDraft"):
        return (False, [
            f"REFUSED: PR #{num} is a draft. Mark it ready first "
            f"(`gh pr ready {num}`), then re-run.",
        ])

    mergeable = _checks.normalize(pr.get("mergeable"))
    if mergeable == "CONFLICTING":
        return (False, [
            f"REFUSED: PR #{num} has conflicts with "
            f"{_untrusted.flat(str(pr.get('baseRefName') or 'its base'))} — "
            f"GitHub reports mergeable=CONFLICTING. Rebase, push, and re-run.",
        ])
    if mergeable != "MERGEABLE":
        return (False, [
            f"REFUSED: whether PR #{num} can merge is UNKNOWN — GitHub reports "
            f"mergeable={mergeable}. GitHub computes mergeability "
            f"asynchronously and returns UNKNOWN while it is working, so this "
            f"is usually settled by re-running in a few seconds. It is not a "
            f"green light in the meantime.",
        ])

    merge_state = _checks.normalize(pr.get("mergeStateStatus"))
    if merge_state not in _MERGE_STATE_OK:
        lines.append(
            f"REFUSED: PR #{num} merge state is {merge_state}, not CLEAN. "
            f"Named states: BEHIND (base moved — rebase), BLOCKED (a required "
            f"review or check is outstanding), DIRTY (conflicts), UNSTABLE (a "
            f"check is red), DRAFT. Anything else is a state this op does not "
            f"recognise, and an unrecognised state is not permission."
        )

    review = _checks.normalize(pr.get("reviewDecision"))
    if review == "CHANGES_REQUESTED":
        lines.append(
            f"REFUSED: PR #{num} has reviewDecision=CHANGES_REQUESTED. "
            f"The review has to be resolved, not merged past."
        )

    lines.extend(_check_findings(pr, declared, missing, reason))





















    notes = _checks.superseded_disclosure(
        [(_untrusted.flat(n), s, k, i)
         for n, s, k, i in _checks.github_named_superseded(
             pr.get("statusCheckRollup"))])



















    misfiled = [n for n in notes if n.lstrip().startswith("REFUSED")]
    if misfiled:
        lines.append(
            f"REFUSED: {len(misfiled)} disclosure line(s) for PR #{num} spell "
            f"a refusal, so they are misfiled — a note cannot decide a verdict "
            f"and a refusal cannot be silent, and this is neither. Treated as "
            f"a refusal because the merge is irreversible. This is a bug in "
            f"this op, not in the pull request."
        )

    return (not lines, lines + notes)


def _check_findings(pr: dict, declared: int | None,
                    missing: Sequence[str],
                    reason: str = "") -> List[str]:








    num = pr.get("number", "?")
    sha = str(pr.get("headRefOid") or "")[:7] or "?"
    rollup = pr.get("statusCheckRollup")

    if not isinstance(rollup, list):
        return [
            f"REFUSED: the check rollup for PR #{num} could not be read, so "
            f"whether anything passed on {sha} is UNKNOWN. That is not zero "
            f"failures — it is no answer. Re-run, or read it with "
            f"`gh-pr:{num}`.",
        ]

    if not rollup:
        head = str(pr.get("headRefName") or "")








        if _refname.ordinary(head):
            pointer = f"`gh-branch:{head}` says whether a run is still expected."
        else:
            pointer = (
                f"The head branch is {_untrusted.flat(head)} — no `gh-branch:` "
                f"command is offered for it: the name is outside the ordinary "
                f"refname set, so a command safe to paste would name a "
                f"different branch. Read it from the PR page.")
        return [
            f"REFUSED: zero check runs on {sha} — {_checks.NO_CHECKS}. Nothing "
            f"has passed and nothing has failed; a commit no workflow ran on is "
            f"not a green one. " + pointer,
        ]








    states = _checks.github_states(rollup)
    live = _checks.github_live_states(rollup)
    tally = _checks.summarize_github(rollup)
    out: List[str] = []

    if not _checks.all_green(live):
        named = [(_untrusted.flat(n), s, k, i)
                 for n, s, k, i in _checks.github_named_live(rollup)]
        out.append(f"REFUSED: checks on {sha} are not all green — {tally}")








        settled = _checks.named_disclosure(
            [e for e in named if _checks.bucket(e[1]) not in ("passed", "pending")])
        out.extend(settled)

        pending = [n for n, s, _k, _i in named
                   if _checks.bucket(s) == "pending"]
        if pending:
            shown = ", ".join(pending[:_checks.NAMED_CAP])
            if len(pending) > _checks.NAMED_CAP:
                shown += f", +{len(pending) - _checks.NAMED_CAP} more"
            out.append(f"  pending: {shown}")

        if settled:
            out.append(
                f"  A leg that is not SUCCESS is not a pass: cancelled, "
                f"skipped, timed_out, neutral and action_required are each "
                f"their own state and none of them is permission (#454). Read "
                f"it with `gh-pr:{num}` or `gh-job:<id>:fail`."
            )
        else:
            out.append(
                f"  Nothing has failed — these legs have not finished, which is "
                f"neither a pass nor a fail. Waiting is the correct action; "
                f"`gh-pr:{num}:status` says when they settle."
            )
        return out

    marker, shortfall_lines = _checks.shortfall(len(states), declared, missing,
                                                reason=reason)
    if marker:
        out.append(
            f"REFUSED: every one of the {len(live)} live legs read on {sha} "
            f"passed, but the tally of all {len(states)} legs on the commit "
            f"could not be squared with what the runs "
            f"declare ({marker}), so whether these are all of the legs is "
            f"UNKNOWN. `gh-branch`'s own verdict makes the same call: a green "
            f"is a claim about all of the legs, and 'every leg I managed to "
            f"read passed' is not that claim."
        )
        out.extend(shortfall_lines)
        out.append(f"  Full picture: `gh-pr:{num}`.")

    return out






def reconcile_links(declared: Sequence[str],
                    bound: Sequence[str] | None
                    ) -> tuple[List[str], List[str], str]:

















    declared = [str(d) for d in declared]
    if bound is None:
        return (list(dict.fromkeys(declared)), [], (
            "GitHub's own closing-issue list could not be read, so whether the "
            "merge was bound to these refs is UNKNOWN — the states below come "
            "from the body's own references only."))

    bound = [str(b) for b in bound]
    refs = list(dict.fromkeys(list(bound) + list(declared)))
    unbound = [d for d in dict.fromkeys(declared) if d not in bound]
    return (refs, unbound, "")


def issue_verdicts(refs: Sequence[str],
                   lookup: Callable[[str], tuple[str, str]]
                   ) -> List[tuple[str, str, str]]:






    out: List[tuple[str, str, str]] = []
    for ref in refs:
        state, err = lookup(ref)
        if err or not state:
            out.append((ref, UNKNOWN, err or "state not returned"))
        else:
            out.append((ref, _checks.normalize(state), ""))
    return out


def render_issue_section(verdicts: Sequence[tuple[str, str, str]],
                         unbound: Sequence[str],
                         note: str,
                         repo: str) -> tuple[List[str], str]:







    lines: List[str] = []
    if note:
        lines.append(f"  {UNKNOWN}: {note}")

    if not verdicts:
        lines.append("  " + _checks.NO_CLOSING_REF +
                     " — this PR declares no closing reference and GitHub "
                     "bound none. Nothing was expected to close.")
        return (lines, UNKNOWN if note else NONE_DECLARED)

    repo_flag = f" --repo {repo}" if repo else ""
    any_open = False
    any_unknown = bool(note)

    for ref, state, why in verdicts:
        number = ref.split("#")[-1]
        target = ref.split("#")[0].rstrip("/") or repo
        flag = f" --repo {target}" if "/" in ref else repo_flag
        mark = " (declared in the body, NOT bound by GitHub)" \
            if ref in unbound else ""
        if state == "CLOSED":
            lines.append(f"  {ref}: CLOSED{mark}")
        elif state == UNKNOWN:
            any_unknown = True
            lines.append(
                f"  {ref}: {UNKNOWN} — {why}. Its state was not read, so it is "
                f"neither closed nor open here. Check it: "
                f"gh issue view {number}{flag} --json state")
        else:
            any_open = True
            lines.append(
                f"  {ref}: {state} — did NOT close{mark}. "
                f"Close it by hand: gh issue close {number}{flag}")

    if unbound:
        lines.append(
            "  A ref the body declares but GitHub did not bind will never be "
            "closed by the merge, whatever the body says. That is PR #908's "
            "shape and it raises no error anywhere.")

    if any_open:
        return (lines, NOT_CLOSED)
    if any_unknown:
        return (lines, UNKNOWN)
    return (lines, ALL_CLOSED)


def release_candidates(verdicts: Sequence[tuple[str, str, str]]) -> List[str]:



















    return [ref.lstrip("#") for ref, state, _ in verdicts
           if state == "CLOSED" and "/" not in ref]






def merge_verdict(after: dict | None, err: str) -> tuple[str, List[str]]:







    if after is None:
        return (UNVERIFIED, [
            f"  merge state: {UNVERIFIED} — the read-back failed ({err}). "
            f"`gh pr merge` exited zero, but a zero exit is not a merge and "
            f"nothing here read the branch. Whether it landed is UNKNOWN.",
        ])

    state = _checks.normalize(after.get("state"))
    merged_at = str(after.get("mergedAt") or "")
    commit = after.get("mergeCommit") or {}
    oid = str(commit.get("oid") or "") if isinstance(commit, dict) else ""

    if state == "MERGED" and merged_at and oid:
        return (MERGED, [
            f"  state:       MERGED (read back, not inferred)",
            f"  mergedAt:    {merged_at}",
            f"  mergeCommit: {oid[:7]} ({oid})",
        ])

    detail = []
    if state != "MERGED":
        detail.append(f"state={state}")
    if not merged_at:
        detail.append("mergedAt absent")
    if not oid:
        detail.append("mergeCommit absent")
    return (UNVERIFIED, [
        f"  merge state: {UNVERIFIED} — {', '.join(detail)}. The PR was read "
        f"back and does not show as merged. Nothing was rolled back and "
        f"nothing was undone; this says only that the merge is not confirmed.",
    ])






def result_line(merge_state: str, issue_overall: str, branch_state: str,
                stack_state: str = "") -> str:










    tail = f" Stacked follow-up: {stack_state}." if stack_state else ""
    if merge_state != MERGED:
        return (f"[result] MERGE {UNVERIFIED.upper()} — not confirmed merged; "
                f"nothing rolled back. Issues: {issue_overall}. "
                f"Default branch: {branch_state}" + tail)

    if issue_overall == NOT_CLOSED:
        return (f"[result] MERGED, but linked issues NOT CLOSED — the merge is "
                f"done and cannot be undone; close them by hand (commands "
                f"above). Default branch: {branch_state}" + tail)
    if issue_overall == UNKNOWN:
        return (f"[result] MERGED, linked issue state {UNKNOWN} — the merge is "
                f"confirmed; whether its issues closed was not read. Verify by "
                f"hand (commands above). Default branch: {branch_state}" + tail)
    if issue_overall == NONE_DECLARED:
        return (f"[result] MERGED, no linked issue declared. "
                f"Default branch: {branch_state}" + tail)
    return (f"[result] MERGED and every linked issue verified closed. "
            f"Default branch: {branch_state}" + tail)






def _gh(args: List[str], timeout: int = 30):
    return subprocess.run(["gh"] + args, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _gh_json(args: List[str], timeout: int = 30) -> tuple[object, str]:
    try:
        r = _gh(args, timeout=timeout)
    except FileNotFoundError:
        return (None, "gh not found — install the GitHub CLI")
    except subprocess.TimeoutExpired:
        return (None, "gh timed out")
    except OSError as e:
        return (None, f"gh could not be run: {e}")
    if r.returncode != 0:







        tail = _untrusted.split_lines((r.stderr or r.stdout).strip())
        return (None, _untrusted.flat(tail[-1]) if tail
                else f"gh exited {r.returncode}")
    try:
        return (json.loads(r.stdout or "null"), "")
    except json.JSONDecodeError:
        return (None, "gh returned invalid JSON")






CLEAN_DONE = "done"
CLEAN_REFUSED = "refused"
CLEAN_SKIPPED = "skipped"

_CLEAN_ITEMS = ("local worktree", "local branch", "remote branch")













_TALLY_RE = re.compile(
    r"^\[result\] (\d+) occupied, (\d+) idle, (\d+) cannot tell")


_DIRT_SHOWN = 5

_WORKTREES_PY = Path(__file__).resolve().parent.parent / "git" / "worktrees.py"






_GIT_TIMEOUT_DEFAULT = 30


def _git(args: List[str], timeout: int | None = None):

























    budget = _git_run.git_timeout(_GIT_TIMEOUT_DEFAULT) if timeout is None else timeout
    return _git_run._git(args, timeout=budget)


def _git_rc(args: List[str], timeout: int = 30) -> tuple[int, str]:







    try:
        r = _git(args, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return (127, f"git could not be run: {e}")
    if r.returncode == 0:
        return (0, "")
    msg = ((r.stderr or r.stdout) or "").strip()
    return (r.returncode, msg or f"git exited {r.returncode}")


def _worktrees_for_branch(branch: str) -> tuple[List[str], str]:














    try:
        r = _git(["worktree", "list", "--porcelain"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return ([], f"`git worktree list` could not be run: {e}")
    if r.returncode != 0:





        why = _untrusted.flat(((r.stderr or r.stdout) or "").strip())
        return ([], f"`git worktree list` exited {r.returncode}"
                    + (f": {why}" if why else ""))
    paths: List[str] = []
    current = ""




    for line in _untrusted.split_lines(r.stdout or ""):
        if line.startswith("worktree "):
            current = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref == f"refs/heads/{branch}" and current:
                paths.append(current)
    return (paths, "")


def _worktree_state(path: str) -> str:










    try:
        r = subprocess.run([sys.executable, str(_WORKTREES_PY), path, "nopr"],
                           capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return f"cannot tell (the occupancy probe did not run: {e})"
    for line in _untrusted.split_lines(r.stdout or ""):
        hit = _TALLY_RE.match(line)
        if not hit:
            continue
        occupied, idle, unknown = (int(g) for g in hit.groups())
        total = occupied + idle + unknown
        if (occupied, idle, unknown) == (0, 1, 0):
            return "idle"
        if occupied:
            return (f"occupied ({occupied} of {total} worktrees under this "
                    f"path are occupied per git-worktrees)")
        return (f"cannot tell (git-worktrees answered about {total} worktrees "
                f"under this path — {occupied} occupied, {idle} idle, "
                f"{unknown} cannot tell — and only a board of exactly one "
                f"idle tree says anything about this one)")
    return (f"cannot tell (git-worktrees printed no [result] tally, so nothing "
            f"was established about {_untrusted.flat(path)}; it exited "
            f"{r.returncode})")














_DIRT_PINS = ["-c", "status.showUntrackedFiles=normal",
              "-c", "core.quotePath=true"]


def _dirt_read(path: str, argv: List[str]) -> tuple[str, str]:






    name = argv[0]
    try:
        r = _git(["-C", path] + _DIRT_PINS + argv)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return ("", f"`git {name}` could not be run: {e}")
    if r.returncode != 0:
        why = ((r.stderr or r.stdout) or "").strip()
        return ("", f"`git {name}` exited {r.returncode}"
                    + (f": {why}" if why else ""))
    return (r.stdout or "", "")


def _worktree_dirt(path: str) -> tuple[List[str], str]:
























    raw: List[str] = []







    out, err = _dirt_read(path, ["status", "--porcelain", "--ignored",
                                 "--untracked-files=normal"])
    if err:
        return ([], err)
    for line in _untrusted.split_lines(out):




        if len(line) > 3:
            raw.append(line[3:].strip())

    out, err = _dirt_read(path, ["ls-files", "--others", "--directory",
                                 "--no-empty-directory"])
    if err:
        return ([], err)




    for line in _untrusted.split_lines(out):
        raw.append(line.strip())



    return ([p for p in dict.fromkeys(raw) if p], "")


def _cleanup_worktree(head: str) -> tuple[str, str, str]:














    item = "local worktree"
    paths, paths_err = _worktrees_for_branch(head)
    if paths_err:
        return (item, CLEAN_REFUSED,
                f"the worktree list could not be read "
                f"({_untrusted.flat(paths_err)}) — a cleanup that could not "
                f"look has not skipped anything")
    if not paths:
        return (item, CLEAN_SKIPPED,
                f"no worktree of this checkout has `{_untrusted.flat(head)}` "
                f"checked out")
    if len(paths) > 1:
        shown = ", ".join(_untrusted.flat(p) for p in paths)
        return (item, CLEAN_REFUSED,
                f"{len(paths)} worktrees hold this branch ({shown}) — removing "
                f"one of them would be a guess about which")
    path = paths[0]
    state = _worktree_state(path)
    if not state.startswith("idle"):
        return (item, CLEAN_REFUSED,
                f"{_untrusted.flat(path)} is `{state}` per git-worktrees, not "
                f"`idle` — and `cannot tell` is treated as occupied, because an "
                f"agent can be alive in a tree that looks finished")
    dirt, dirt_err = _worktree_dirt(path)
    if dirt_err:
        return (item, CLEAN_REFUSED,
                f"what {_untrusted.flat(path)} holds could not be read "
                f"({_untrusted.flat(dirt_err)}) — a tree whose contents were "
                f"never established is not a tree to delete")
    if dirt:
        shown = ", ".join(_untrusted.flat(p) for p in dirt[:_DIRT_SHOWN])
        more = (f" and {len(dirt) - _DIRT_SHOWN} more"
                if len(dirt) > _DIRT_SHOWN else "")
        return (item, CLEAN_REFUSED,
                f"{_untrusted.flat(path)} holds {len(dirt)} file(s) git is not "
                f"tracking ({shown}{more}). `git worktree remove` deletes "
                f"**ignored** files whatever flags it is given, and an ignored "
                f"file is in no index, no stash and no remote — remove the "
                f"tree by hand once you have looked at those")
    rc, msg = _git_rc(["worktree", "remove", path])
    if rc != 0:
        return (item, CLEAN_REFUSED,
                f"git declined to remove {_untrusted.flat(path)}: "
                f"{_untrusted.flat(msg)}")
    return (item, CLEAN_DONE,
            f"removed {_untrusted.flat(path)} — two reads with different "
            f"failure modes both came back empty first (`git status "
            f"--porcelain --ignored` with the untracked display pinned on, "
            f"and the plumbing `git ls-files --others`), so nothing "
            f"untracked, ignored or modified was there to destroy")


def _cleanup_local_branch(head: str) -> tuple[str, str, str]:
    item = "local branch"
    safe = _untrusted.flat(head)
    rc, _msg = _git_rc(["rev-parse", "--verify", "--quiet",
                        f"refs/heads/{head}"])
    if rc != 0:
        return (item, CLEAN_SKIPPED,
                f"no local branch `{safe}` in this checkout")
    rc, msg = _git_rc(["branch", "-d", head])
    if rc == 0:
        return (item, CLEAN_DONE, f"deleted local `{safe}`")
    return (item, CLEAN_REFUSED,
            f"`git branch -d {safe}` declined: {_untrusted.flat(msg)}. That is "
            f"expected after a squash: `-d` cannot see the branch's commits in "
            f"the squashed commit, so it correctly says it cannot confirm the "
            f"merge — observed on fix/1207, whose PR #1212 had merged. `-D` is "
            f"never run here; confirm against the PR and delete by hand")


def _remote_ref_sha(head: str) -> tuple[str, str]:







    data, err = _gh_json(["api", _repo_target.api_path("git/ref/heads/" + head, explicit=True)])
    if err:
        return ("", err)
    if isinstance(data, list):
        return ("", f"the name matched {len(data)} refs, not one")
    if not isinstance(data, dict):
        return ("", "the API returned no ref object")
    obj = data.get("object")
    sha = str(obj.get("sha") or "") if isinstance(obj, dict) else ""
    if not sha:
        return ("", "the API returned a ref with no object sha")
    return (sha, "")


def _cleanup_remote_branch(head: str, head_oid: str) -> tuple[str, str, str]:














    item = "remote branch"
    safe = _untrusted.flat(head)
    if not head_oid:
        return (item, CLEAN_REFUSED,
                f"the PR carried no headRefOid, so nothing establishes that "
                f"`{safe}` in this repository is this PR's head rather than a "
                f"ref of ours wearing the same name")
    sha, read_err = _remote_ref_sha(head)
    if read_err:
        return (item, CLEAN_REFUSED,
                f"`{safe}` could not be read back before deleting it "
                f"({_untrusted.flat(read_err)}) — an unread ref is not deleted")
    if sha != head_oid:
        return (item, CLEAN_REFUSED,
                f"`{safe}` in this repository points at {sha[:7]}, not at this "
                f"PR's head {head_oid[:7]} — the same name, a different ref. "
                f"Deleting it would destroy a branch this PR never owned")
    ref_path = _repo_target.api_path("git/refs/heads/" + head, explicit=True)
    try:
        r = _gh(["api", "-X", "DELETE", ref_path])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return (item, CLEAN_REFUSED, f"gh could not be run: {e}")
    if r.returncode != 0:
        msg = ((r.stderr or r.stdout) or "").strip() or f"gh exited {r.returncode}"
        return (item, CLEAN_REFUSED,
                f"the API refused the delete: {_untrusted.flat(msg)}")
    return (item, CLEAN_DONE,
            f"deleted `{safe}` on the remote via the API — it was read back at "
            f"{head_oid[:7]}, this PR's own head, so it is recoverable from "
            f"refs/pull/N/head")


def run_cleanup(head: str, *, merged: bool, cross_repo: bool | None = None,
                default_branch: str = "",
                head_oid: str = "",
                stack_state: str = UNKNOWN) -> List[tuple[str, str, str]]:
































    if not merged:
        return [(i, CLEAN_SKIPPED,
                 "the merge is not confirmed, so nothing about this branch is "
                 "safe to delete") for i in _CLEAN_ITEMS]

    if not _refname.ordinary(head):
        reason = (f"the branch name is not an ordinary ref "
                  f"({_untrusted.flat(_refname.shell_ref(head))}) — a name "
                  f"carrying a leading dash or a character a shell acts on is "
                  f"refused rather than passed to a delete. Use the PR page")
        return [(i, CLEAN_REFUSED, reason) for i in _CLEAN_ITEMS]

    if cross_repo is not False:
        where = "is true" if cross_repo else "did not come back"
        reason = (f"the head branch is not established to be in this "
                  f"repository (`isCrossRepository` {where}) — "
                  f"`{_untrusted.flat(head)}` then names a **fork's** branch "
                  f"while every arm below acts on this repository, so each of "
                  f"them would hit a ref of ours that happens to share the "
                  f"name. Delete the fork's branch from the PR page")
        return [(i, CLEAN_REFUSED, reason) for i in _CLEAN_ITEMS]

    if not default_branch:
        reason = ("this repository's default branch could not be read, so "
                  "nothing here can tell whether the cleanup is about to "
                  "delete it")
        return [(i, CLEAN_REFUSED, reason) for i in _CLEAN_ITEMS]

    if head == default_branch:
        reason = (f"`{_untrusted.flat(head)}` is this repository's default "
                  f"branch. A head branch with that name is never a branch to "
                  f"clean up, whoever opened the PR and wherever it lives")
        return [(i, CLEAN_REFUSED, reason) for i in _CLEAN_ITEMS]

    rows: List[tuple[str, str, str]] = []
    target = _repo_target.target(explicit=True)
    if target:
        why = (f"a repo target is set ({_untrusted.flat(target)}), so this "
               f"checkout is not that PR's repository — deleting a local "
               f"branch of the same name here would hit the wrong repo")
        rows.append(("local worktree", CLEAN_SKIPPED, why))
        rows.append(("local branch", CLEAN_SKIPPED, why))
    else:



        rows.append(_cleanup_worktree(head))
        rows.append(_cleanup_local_branch(head))

    if stack_state != STACK_NONE:









        why = ("an open pull request has been found based on this branch"
               if stack_state == STACK_FOUND else
               "whether an open pull request is now based on this branch "
               "was not established")
        rows.append(("remote branch", CLEAN_REFUSED,
                     f"{why} — see the `## Stacked follow-up` section above. "
                     f"Deleting the remote branch closes that pull request "
                     f"and there is no clean way back; retarget or close it "
                     f"first, then delete the branch by hand"))
    else:
        rows.append(_cleanup_remote_branch(head, head_oid))
    return rows


def render_cleanup(rows: Sequence[tuple[str, str, str]]) -> List[str]:


    tally = {CLEAN_DONE: 0, CLEAN_REFUSED: 0, CLEAN_SKIPPED: 0}
    out: List[str] = []
    for item, state, detail in rows:
        tally[state] = tally.get(state, 0) + 1
        out.append(f"  {item:<15} {state:<8} {detail}")
    left = tally[CLEAN_REFUSED] + tally[CLEAN_SKIPPED]
    out.append(f"  [cleanup] {tally[CLEAN_DONE]} done, "
               f"{tally[CLEAN_REFUSED]} refused, {tally[CLEAN_SKIPPED]} skipped"
               + (f" — {left} item(s) are still there, named above; a refused "
                  f"cleanup is not a failed merge and does not move the exit "
                  f"code" if left else ""))
    return out


def _bound_refs(number: str, repo: str) -> Sequence[str] | None:

    owner, name = _declared_legs.owner_repo(repo)
    if not owner or not name:
        return None
    query = (
        "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r)"
        "{pullRequest(number:$n){closingIssuesReferences(first:20)"
        "{nodes{number repository{nameWithOwner}}}}}}"
    )
    data, err = _gh_json([
        "api", "graphql", "-f", f"query={query}", "-f", f"o={owner}",
        "-f", f"r={name}", "-F", f"n={number}",
    ])
    if err or not isinstance(data, dict):
        return None
    try:
        nodes = data["data"]["repository"]["pullRequest"][
            "closingIssuesReferences"]["nodes"]
    except (KeyError, TypeError):
        return None
    if not isinstance(nodes, list):
        return None
    out: List[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        slug = str(((n.get("repository") or {}).get("nameWithOwner")) or "")
        num = n.get("number")
        if num is None:
            continue
        out.append(f"#{num}" if slug == repo or not slug
                   else f"{slug}#{num}")
    return out


def _issue_lookup(repo: str) -> Callable[[str], tuple[str, str]]:
    def lookup(ref: str) -> tuple[str, str]:
        number = ref.split("#")[-1]
        target = ref.split("#")[0].rstrip("/") or repo
        args = ["issue", "view", number, "--json", "state"]
        if target:
            args += ["--repo", target]
        data, err = _gh_json(args, timeout=20)
        if err:
            return ("", err)
        if not isinstance(data, dict) or not data.get("state"):
            return ("", "gh returned no state field")
        return (str(data["state"]), "")
    return lookup


def _default_branch_report(default_branch: str, repo: str,
                           merge_sha: str) -> tuple[str, List[str]]:









    if not default_branch:
        return (UNKNOWN, [
            f"  {UNKNOWN}: the repository's default branch could not be "
            f"resolved, so its state after the merge was not read."])

    script = str(Path(__file__).resolve().parent / "branch.py")
    try:





        r = subprocess.run([sys.executable, script, default_branch],
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError) as e:
        return (UNKNOWN, [
            f"  {UNKNOWN}: gh-branch did not return ({e}), so the state of "
            f"`{default_branch}` after this merge was not read. Run it "
            f"yourself: `gh-branch:{default_branch}`"])

    state = UNKNOWN
    lines: List[str] = []






    in_scope_block = False








    for line in _untrusted.split_lines(r.stdout or ""):
        if line.startswith(f"Branch {default_branch}: "):
            state = line.split(": ", 1)[1].strip()
        if line.startswith("Declared "):
            in_scope_block = True
        elif not line.strip():
            in_scope_block = False
        if in_scope_block or line.startswith(
                ("Branch ", "Head: ", "Verdict: ", "Legs: ")):
            lines.append(f"  {line}")
    if not lines:
        return (UNKNOWN, [
            f"  {UNKNOWN}: gh-branch returned nothing readable for "
            f"`{default_branch}`. Run it yourself: `gh-branch:{default_branch}`"])

    if merge_sha and repo:
        lines.append(f"  Run for this merge commit: "
                     f"https://github.com/{repo}/commit/{merge_sha}/checks")
    lines.append(f"  Full picture: `gh-branch:{default_branch}`")
    return (state, lines)










_COMPARE_JQ = ("{behind_by, base_sha: .base_commit.sha, "
               "base_date: .base_commit.commit.committer.date}")


def base_distance(base: str, head_oid: str) -> tuple[int | None, str, str, str]:











    if not base or not head_oid:
        return (None, "", "", "the PR's base branch or head commit was not in "
                              "the API reply")
    data, err = _gh_json(
        ["api", _repo_target.api_path(f"compare/{base}...{head_oid}", explicit=True),
         "--jq", _COMPARE_JQ],
        timeout=30)
    if err or not isinstance(data, dict):
        return (None, "", "", err or "the compare API returned no object")
    behind = data.get("behind_by")
    if not isinstance(behind, int) or isinstance(behind, bool):
        return (None, "", "", f"the compare API returned no usable behind_by "
                              f"({behind!r})")
    return (behind, str(data.get("base_sha") or ""),
            str(data.get("base_date") or ""), "")


def base_distance_lines(base: str, head_oid: str, behind: int | None,
                        base_sha: str, base_date: str,
                        err: str) -> List[str]:













    b = _untrusted.flat(base or "?")
    head7 = head_oid[:7] if head_oid else "?"
    if behind is None:
        return [
            f"  UNKNOWN: how far `{b}` has moved since {head7} could not be "
            f"read ({err}).",
            f"  The tally below is a statement about this PR's merge-base. "
            f"Whether that is still `{b}`'s head was NOT established — this "
            f"line is the question, not an answer to it.",
        ]
    if behind == 0:
        return [
            f"  `{b}` has not moved since this PR's head {head7} — the tally "
            f"below covers a tree containing every commit on `{b}`.",
        ]
    sha7 = base_sha[:7] if base_sha else "?"
    plural = "commit" if behind == 1 else "commits"
    return [
        f"  BEHIND: `{b}` is {behind} {plural} ahead of this PR's head "
        f"{head7}. The checks below ran on a tree that does not contain them.",
        f"  `{b}` head: {sha7}"
        + (f" ({base_date})" if base_date else ""),
        f"  A green tally is evidence about this PR's merge-base and about "
        f"nothing else. Two PRs each 22/22 green on disjoint files turned "
        f"`master` red on 2026-08-10 that way — no conflict, no failing leg, "
        f"and a whole-tree test that only met the other PR's new op after both "
        f"had landed (#1257). Nothing here blocks the merge; it is the fact "
        f"you would otherwise have to know to ask for.",
    ]






def stacked_followups(head: str) -> tuple[str, List[str]]:















    if not head or not _refname.ordinary(head):
        return (UNKNOWN, [
            f"  {UNKNOWN}: the head branch name could not be used to search "
            f"for a stacked follow-up."])
    data, err = _gh_json(
        ["pr", "list", "--state", "open", "--base", head,
         "--json", "number,title,url"] + _repo_target.gh_args(explicit=True), timeout=20)
    if err or not isinstance(data, list):
        return (UNKNOWN, [
            f"  {UNKNOWN}: whether any open pull request now targets "
            f"`{_untrusted.flat(head)}` could not be read "
            f"({_untrusted.flat(err or 'gh returned no list')})."])
    if not data:
        return (STACK_NONE, [
            f"  none — no open pull request targets `{_untrusted.flat(head)}`; "
            f"nothing about this merge is left unwatched on that account."])
    lines = [f"  {len(data)} open pull request(s) now target "
             f"`{_untrusted.flat(head)}` and need a poller unless one already "
             f"covers them:"]
    for row in data:
        if not isinstance(row, dict):
            continue
        num = row.get("number")
        title = _untrusted.flat(str(row.get("title") or ""))
        url = row.get("url") or "?"
        lines.append(f"    #{num} {title} — {url}")
    return (STACK_FOUND, lines)


def _repo_identity() -> tuple[str, str, str]:
    data, err = _gh_json(["repo", "view", "--json",
                          "nameWithOwner,defaultBranchRef"]
                         + _repo_target.gh_args(explicit=True), timeout=20)
    if err or not isinstance(data, dict):
        return ("", "", err or "gh repo view returned no data")
    ref = data.get("defaultBranchRef") or {}
    return (str(data.get("nameWithOwner") or ""),
            str(ref.get("name") or "") if isinstance(ref, dict) else "", "")






def _usage() -> str:
    return ("ERROR: usage: "
            "gh-pr-merge:NUMBER[:squash|:merge|:rebase][|force][|cleanup]. "
            "Without |force it previews the gate and merges nothing. "
            "|cleanup deletes the head branch and an idle worktree AFTER the "
            "merge is read back as MERGED, three states per item.")


def parse_argv(argv: Sequence[str]) -> tuple[str, str, bool, bool, str]:







    tokens: List[str] = []
    for a in argv:
        for piece in str(a).split("|"):
            if piece:
                tokens.append(piece)



    if not tokens or not _digits.is_ascii_int(tokens[0]):
        return ("", "squash", False, False, _usage())

    number, method, force, cleanup = tokens[0], "squash", False, False
    for tok in tokens[1:]:
        if tok == "force":
            force = True
        elif tok == "cleanup":
            cleanup = True
        elif tok in MERGE_METHODS:
            method = tok
        else:
            return (number, method, force, cleanup,
                    f"ERROR: unrecognised token {tok!r}. "
                    f"Merge methods: {', '.join(MERGE_METHODS)}. "
                    f"Other tokens: force, cleanup. {_usage()}")
    return (number, method, force, cleanup, "")


def main() -> int:
    use_utf8_stdout()
    number, method, force, do_cleanup, parse_err = parse_argv(
        [a for a in sys.argv[1:] if a != ""])
    if parse_err:
        print(parse_err)
        return 1

    repo, default_branch, ident_err = _repo_identity()
    if ident_err and not repo:







        print(_repo_target.no_repo_error(f"gh-pr-merge:{number}",
                                         detail=ident_err, explicit=True))
        return 1

    pr, err = _gh_json(["pr", "view", number, "--json", _PR_FIELDS]
                       + _repo_target.gh_args(explicit=True))
    if err or not isinstance(pr, dict):
        print(f"ERROR: PR #{number} could not be read "
              f"{_repo_target.not_found_scope(explicit=True)}: {err}. "
              f"{_repo_target.not_found_hint(explicit=True)}")
        return 1

    _pr_mod = _load_pr_module()





    declared, declared_names, _unc, reason = _pr_mod._declared_for_commit(pr)
    found = _pr_mod._actions_leg_names(pr.get("statusCheckRollup"))
    missing = _declared_legs.missing_names(declared_names, found)

    title = _untrusted.flat(str(pr.get("title") or ""))
    head = str(pr.get("headRefName") or "")
    base = str(pr.get("baseRefName") or "")

    print(f"# gh-pr-merge #{number} — {repo or '?'}")
    print(f"PR:     {title}")
    print(f"Merge:  {_untrusted.flat(head)} -> {_untrusted.flat(base)}  "
          f"(method: {method})")
    print(f"URL:    {pr.get('url', '?')}")
    print()




    head_oid = str(pr.get("headRefOid") or "")
    behind, base_sha, base_date, base_err = base_distance(base, head_oid)
    print("## Base")
    for line in base_distance_lines(base, head_oid, behind, base_sha,
                                    base_date, base_err):
        print(line)
    print()

    allowed, gate_lines = gate(pr, declared, missing, reason)
    if not allowed:
        print("## Gate")
        for line in gate_lines:
            print(line)
        print()
        print(f"Nothing was merged. This op has no green-bypass: if you "
              f"disagree with the refusal, `gh pr merge {number} --{method}` "
              f"is refused too, by this repo's own raw-command guard, which "
              f"points back here. The route that is actually open is "
              f"`gh pr merge {number} --web`, which opens the merge page in "
              f"the browser for a human to decide -- or ask the maintainer "
              f"to merge from outside a hooked session.")
        print(f"[result] REFUSED — PR #{number} was not merged. "
              f"Reasons above; nothing changed.")
        return 1

    states = _checks.github_states(pr.get("statusCheckRollup"))








    authorising = _checks.summarize_github(pr.get("statusCheckRollup"))
    print("## Gate — passed")
    print(f"  checks:      {authorising}")
    print(f"  reconciled:  {len(states)} legs read, {declared} declared")
    print(f"  mergeable:   {_checks.normalize(pr.get('mergeable'))} / "
          f"{_checks.normalize(pr.get('mergeStateStatus'))}")






    for line in gate_lines:
        print(line)
    print()

    if not force:
        _publish_safety.require_confirm(
            f"gh-pr-merge #{number} ({head} -> {base}, {method})",
            f"{title}", force=False)


    try:
        merged = _gh(["pr", "merge", number, f"--{method}"]
                     + _repo_target.gh_args(explicit=True), timeout=90)
        merge_err = "" if merged.returncode == 0 else (
            (merged.stderr or merged.stdout).strip() or
            f"gh exited {merged.returncode}")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        merge_err = f"gh pr merge did not complete: {e}"

    after, read_err = _gh_json(
        ["pr", "view", number, "--json",
         "state,mergedAt,mergeCommit,headRefName"] + _repo_target.gh_args(explicit=True))

    print("## Merge")
    if merge_err:
        print(f"  gh pr merge reported: {merge_err}")
    m_state, m_lines = merge_verdict(
        after if isinstance(after, dict) else None,
        read_err or merge_err or "no detail")
    for line in m_lines:
        print(line)
    print()

    merge_sha = ""
    if isinstance(after, dict):
        commit = after.get("mergeCommit") or {}
        if isinstance(commit, dict):
            merge_sha = str(commit.get("oid") or "")


    declared_refs = _checks.closing_issue_refs(pr.get("body"))
    bound = _bound_refs(number, repo)
    refs, unbound, note = reconcile_links(declared_refs, bound)
    verdicts = issue_verdicts(refs, _issue_lookup(repo))
    issue_lines, issue_overall = render_issue_section(
        verdicts, unbound, note, repo)

    print("## Linked issues")
    for line in issue_lines:
        print(line)
    print()


    if m_state != MERGED or not head:
        stack_state = UNKNOWN
        print("## Stacked follow-up")
        print("  Skipped — the merge is not confirmed, so whether anything "
              "now targets this branch was not checked.")
        print()
    else:
        stack_state, stack_lines = stacked_followups(head)
        print("## Stacked follow-up")
        for line in stack_lines:
            print(line)
        print()


    branch_state, branch_lines = _default_branch_report(
        default_branch, repo, merge_sha)
    print(f"## Default branch after the squash — {default_branch or '?'}")
    for line in branch_lines:
        print(line)
    print()






    x_repo_raw = pr.get("isCrossRepository")
    x_repo = x_repo_raw if isinstance(x_repo_raw, bool) else None

    if do_cleanup:
        print("## Cleanup — run by this op (`cleanup`)")
        for line in render_cleanup(
                run_cleanup(head, merged=(m_state == MERGED and bool(head)),
                            cross_repo=x_repo,
                            default_branch=default_branch,
                            head_oid=str(pr.get("headRefOid") or ""),
                            stack_state=stack_state)):
            print(line)












        if m_state == MERGED:
            released = release_candidates(verdicts)
            print(f"  [release] {', '.join(released) if released else 'none'} "
                  f"— same-repo issue(s) this merge verified CLOSED; a "
                  f"caller with its own per-issue lane bookkeeping may "
                  f"release these now (#2337). This op stores nothing and "
                  f"calls nothing on your behalf.")
        else:
            print("  [release] none — the merge is not confirmed, so "
                  "nothing here is attributed to it")
        print()
        print(result_line(m_state, issue_overall, branch_state, stack_state))
        return 0 if (m_state == MERGED and
                     issue_overall in (ALL_CLOSED, NONE_DECLARED)) else 1





    print("## Cleanup — not run by this invocation (add `|cleanup`)")
    if m_state != MERGED or not head:
        print("  Skipped — the merge is not confirmed, so nothing about this "
              "branch is safe to delete.")
    elif not _refname.ordinary(head):








        print(f"  Head branch {_untrusted.flat(_refname.shell_ref(head))} "
              f"still exists.")
        print("  No delete command is printed for it: the name contains "
              "characters a shell acts on or a terminal breaks a line at, and "
              "a command that is safe to paste would no longer name this "
              "branch, and `|cleanup` refuses it on the same ground. Delete it "
              "from the PR page, or by hand after reading the name above.")
    elif x_repo is not False or not default_branch or head == default_branch:

















        print(f"  Head branch {_untrusted.flat(_refname.shell_ref(head))} "
              f"still exists.")
        if x_repo is not False:
            why = ("the head is not established to be in this repository "
                   "(`isCrossRepository` "
                   + ("is true" if x_repo else "did not come back")
                   + "), so a command naming it would be aimed at a ref of "
                     "ours that happens to share the name")
        elif not default_branch:
            why = ("this repository's default branch could not be read, so "
                   "nothing here can tell whether the command would be aimed "
                   "at it — the same fact `run_cleanup` establishes before it "
                   "deletes anything")
        else:
            why = (f"it is this repository's default branch, so a delete "
                   f"command naming it would be aimed at `{default_branch}` "
                   f"here")
        print(f"  No delete command is printed for it: {why}, and `|cleanup` "
              f"refuses it on the same ground. Delete the branch from the PR "
              f"page, which knows which repository it is in.")
    elif stack_state != STACK_NONE:








        safe_head = _refname.shell_ref(head)
        why = ("an open pull request has been found based on this branch"
               if stack_state == STACK_FOUND else
               "whether an open pull request is now based on this branch "
               "was not established")
        print(f"  Head branch `{safe_head}` still exists.")
        print(f"  No remote delete command is printed for it: {why} — see "
              f"the `## Stacked follow-up` section above. Deleting the "
              f"remote branch closes that pull request and there is no "
              f"clean way back; retarget or close it first, then delete the "
              f"branch by hand. `|cleanup` refuses the remote branch item "
              f"on the same ground.")
        print(f"  Local worktree, if any, is still safe to remove: "
              f"git worktree remove <path> && git branch -d {safe_head}")
    else:
        safe_head = _refname.shell_ref(head)






        ref_path = _refname.shell_ref(
            _repo_target.api_path_for_display("git/refs/heads/" + head, repo, explicit=True))
        print(f"  Head branch `{safe_head}` still exists. "
              f"Delete it when you are done: gh api -X DELETE {ref_path}")
        print(f"  Local worktree, if any: git worktree remove <path> && "
              f"git branch -d {safe_head}")
        print("  Deliberately not chained by default: a merge and a delete in "
              "one command once deleted the branch and auto-closed the PR "
              "after the merge had failed on a conflict.")
        print(f"  To have this op do it instead, gated on the MERGED it just "
              f"read back: gh-pr-merge:{number}:{method}|force|cleanup")
    print()

    print(result_line(m_state, issue_overall, branch_state, stack_state))
    return 0 if (m_state == MERGED and
                 issue_overall in (ALL_CLOSED, NONE_DECLARED)) else 1


if __name__ == "__main__":
    sys.exit(main())
