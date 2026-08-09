#!/usr/bin/env python3
"""Merge a pull request, then prove it landed and that its issues closed (#950).

The four calls this replaces were run by hand six times on 2026-08-07::

    gh pr create ...
    gh pr merge N --squash
    gh pr view N --json state,mergedAt,mergeCommit -q '"state=\\(.state) ..."'
    gh issue view M --json state -q .state

The third exists because `gh pr merge` can print nothing at all on success, so a
zero exit is the only evidence — and a zero exit is not a merge. The fourth
exists because **`Closes #N` silently does not always fire**: read against the
API on 2026-08-07, eleven of the last twelve merged PRs of this repository had
their declared closing reference bound by GitHub and PR #908 did not. Its body
said `Closes #899`, `closingIssuesReferences` came back empty, nothing errored.
A shipped fix behind an issue still reading as outstanding is what the next
triage tick re-delegates.

**This is the first op in the family that writes**, so the refusal surface is
the design rather than a detail of it:

* Not green is not merged, and "green" is #454's arithmetic — the state counts
  must sum to the leg count, and `CANCELLED`, `SKIPPED`, `TIMED_OUT`, `NEUTRAL`
  and `ACTION_REQUIRED` are none of them a pass and none of them a pending.
* A tally that could not be reconciled is a refusal too. That is the call
  `gh-branch`'s own `verdict()` already makes with `unreconciled`: every leg
  read passed, but whether those are all of the legs is UNKNOWN, and on a merge
  gate the difference is the whole point of the op.
* **Nothing is deleted.** Chaining a branch delete onto a merge once deleted the
  branch and auto-closed the PR when the merge had actually failed on a
  conflict. The cleanup command is printed; it is never run.
* There is **no `--force` past the gate.** A green-bypass would make the op's
  one guarantee conditional on the caller, which is the thing that fails at 2am.
  A refusal names the raw command to run by hand instead, so the escape hatch
  exists without this op ever being what merged something unverified.

The confirmation gate itself is `_publish_safety.require_confirm` — the same
one the publish ops use (#149) — so a merge is never single-shot and the
existing env/`.supertool.json` opt-outs are honoured rather than re-invented.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _checks  # noqa: E402
import _declared_legs  # noqa: E402
import _publish_safety  # noqa: E402
import _refname  # noqa: E402
import _repo_target  # noqa: E402
import _untrusted  # noqa: E402


def _load_pr_module():
    """`gh-pr`'s own module, so the two ops derive the tally identically.

    The declared-leg count and the found-leg names must come from the same
    derivation `gh-pr` prints, or the gate and the dashboard can disagree about
    the same PR — and the one a reader would then believe is whichever they ran
    last. Loaded by path because `presets/github/` is not a package.
    """
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

# Merge-state values that are not a go. GitHub's `mergeStateStatus` is
# advisory rather than authoritative — `UNSTABLE` in particular means "mergeable
# but a check is red", which the check gate catches anyway — but each of these
# names a distinct reason a merge should not be attempted, and an unrecognised
# value joins them: an unknown state is not permission.
_MERGE_STATE_OK = frozenset({"CLEAN", "HAS_HOOKS"})

_PR_FIELDS = (
    "number,title,state,isDraft,mergeable,mergeStateStatus,reviewDecision,"
    "baseRefName,headRefName,headRefOid,url,body,statusCheckRollup"
)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

def gate(pr: dict, declared: int | None = None,
         missing: Sequence[str] = (),
         reason: str = "") -> tuple[bool, List[str]]:
    """`(allowed, lines)` — may this PR be merged, and on what evidence.

    Every branch that returns False names what it saw, in the state GitHub
    spelled it, and offers the op that answers the next question. A refusal
    whose reason a reader has to go and look up sends them to the web UI, which
    is the cost this family exists to remove.
    """
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

    return (not lines, lines)


def _check_findings(pr: dict, declared: int | None,
                    missing: Sequence[str],
                    reason: str = "") -> List[str]:
    """The check half of the gate — #454's arithmetic, applied to a merge.

    `reason` is why the declared count could not be established (#1181), and
    this is the render where it is worth most: the refusal below is the last
    line read before a merge, and "could not be squared" without a cause is
    the same sentence whether the API was down or the op's own reconciliation
    budget was one workflow too small.
    """
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
        # A convenience command, so it is declined rather than repaired
        # (`presets/_refname.py`, and the same call this file already makes for
        # the head-branch delete command below). Neither treatment makes an
        # unordinary name both correct and safe here: unflattened it forges
        # lines in a refusal, and flattened it names a branch that does not
        # exist — a `gh-branch:` the reader runs, on the tool's authority,
        # that answers about nothing. The name itself is still printed in
        # full, so nothing is withheld; only the command is.
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
    tally = _checks.summarize(states)
    out: List[str] = []

    if not _checks.all_green(states):
        named = [(_untrusted.flat(n), s, k, i)
                 for n, s, k, i in _checks.github_named_states(rollup)]
        out.append(f"REFUSED: checks on {sha} are not all green — {tally}")

        # `named_disclosure` drops the pending bucket on purpose — on a status
        # board a still-queued leg resolves itself and naming eight of them per
        # poll is noise. On a *gate* it is the opposite: which legs are still
        # moving is precisely what the reader is waiting for, and a refusal that
        # names nothing sends them back to the web UI. So the pending group is
        # named here, separately, with the same cap, rather than by loosening a
        # shared helper whose exclusion is right for its own callers.
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
            f"REFUSED: every one of the {len(states)} legs read on {sha} "
            f"passed, but the tally could not be squared with what the runs "
            f"declare ({marker}), so whether these are all of the legs is "
            f"UNKNOWN. `gh-branch`'s own verdict makes the same call: a green "
            f"is a claim about all of the legs, and 'every leg I managed to "
            f"read passed' is not that claim."
        )
        out.extend(shortfall_lines)
        out.append(f"  Full picture: `gh-pr:{num}`.")

    return out


# ---------------------------------------------------------------------------
# linked issues
# ---------------------------------------------------------------------------

def reconcile_links(declared: Sequence[str],
                    bound: Sequence[str] | None
                    ) -> tuple[List[str], List[str], str]:
    """`(refs, declared_but_unbound, note)` for the issues this PR should close.

    Two independent sources, and the disagreement between them **is** the
    finding. `declared` is what the body says, parsed by the same
    `_checks.closing_issue_refs` `gh-pr` renders. `bound` is GitHub's own
    `closingIssuesReferences`, i.e. what the merge will actually act on.

    A ref in the body that GitHub never bound is PR #908's exact shape and is
    the one thing a reader could not otherwise see: nothing errors, the body
    reads correctly, and the issue simply stays open. It is named here rather
    than inferred afterwards from the issue's state, because an issue closed by
    some other route would hide it.

    `bound is None` means the list could not be read. The refs still get
    checked — the body is a real source — but nothing may be concluded about
    binding, so that renders as a note rather than as an empty unbound list.
    """
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
    """`(ref, state, note)` per issue. A lookup that failed is UNKNOWN.

    Never `CLOSED` and never `OPEN` on a read that did not come back — this is
    the merge path, and a check that could not run reporting `ok` is the defect
    class the whole repository is organised against.
    """
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
    """The named, per-issue receipt, and the one word that summarises it.

    Every issue is named individually with its verified state, and every one
    that did not close carries **the exact command to close it by hand** — the
    op's whole reason for existing is that this step used to be a fifth call
    someone had to remember to make.
    """
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


# ---------------------------------------------------------------------------
# merge verification
# ---------------------------------------------------------------------------

def merge_verdict(after: dict | None, err: str) -> tuple[str, List[str]]:
    """`(state, lines)` from the PR **read back off the remote** after merging.

    `gh pr merge` can print nothing at all on success, so its exit code is the
    only signal it offers, and an exit code is a statement about a process
    rather than about a branch. Nothing here is inferred from it: `MERGED`
    requires `state`, `mergedAt` and a merge commit oid to have all come back.
    """
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


# ---------------------------------------------------------------------------
# the verdict line
# ---------------------------------------------------------------------------

def result_line(merge_state: str, issue_overall: str, branch_state: str) -> str:
    """One line, no newline, that survives `| tail -1`.

    Leads with the irreversible half. A merge that landed while an issue stayed
    open is **not** a success and must not read as one — but it is also not a
    failed merge, and rendering it as either loses half the truth.
    """
    if merge_state != MERGED:
        return (f"[result] MERGE {UNVERIFIED.upper()} — not confirmed merged; "
                f"nothing rolled back. Issues: {issue_overall}. "
                f"Default branch: {branch_state}")

    if issue_overall == NOT_CLOSED:
        return (f"[result] MERGED, but linked issues NOT CLOSED — the merge is "
                f"done and cannot be undone; close them by hand (commands "
                f"above). Default branch: {branch_state}")
    if issue_overall == UNKNOWN:
        return (f"[result] MERGED, linked issue state {UNKNOWN} — the merge is "
                f"confirmed; whether its issues closed was not read. Verify by "
                f"hand (commands above). Default branch: {branch_state}")
    if issue_overall == NONE_DECLARED:
        return (f"[result] MERGED, no linked issue declared. "
                f"Default branch: {branch_state}")
    return (f"[result] MERGED and every linked issue verified closed. "
            f"Default branch: {branch_state}")


# ---------------------------------------------------------------------------
# gh plumbing
# ---------------------------------------------------------------------------

def _gh(args: List[str], timeout: int = 30):
    return subprocess.run(["gh"] + args, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _gh_json(args: List[str], timeout: int = 30) -> tuple[object, str]:
    try:
        r = _gh(args, timeout=timeout)
    except FileNotFoundError:
        return (None, "gh not found — install from https://cli.github.com")
    except subprocess.TimeoutExpired:
        return (None, "gh timed out")
    except OSError as e:
        return (None, f"gh could not be run: {e}")
    if r.returncode != 0:
        return (None, (r.stderr or r.stdout).strip().splitlines()[-1:] and
                (r.stderr or r.stdout).strip().splitlines()[-1] or
                f"gh exited {r.returncode}")
    try:
        return (json.loads(r.stdout or "null"), "")
    except json.JSONDecodeError:
        return (None, "gh returned invalid JSON")


def _bound_refs(number: str, repo: str) -> Sequence[str] | None:
    """GitHub's own `closingIssuesReferences`, or None when it did not answer."""
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
    """`(state, lines)` for the default branch after the squash.

    Delegated to `gh-branch` rather than re-derived. A green PR is a statement
    about its merge-base; the default branch after the squash is a different
    commit with a different run, and that gap has left `master` red for hours
    while the board read clean. `gh-branch` is the op that already answers it
    correctly — per workflow, never by recency — so this shells out to it and
    quotes what it said, which also means every future fix to it lands here.
    """
    if not default_branch:
        return (UNKNOWN, [
            f"  {UNKNOWN}: the repository's default branch could not be "
            f"resolved, so its state after the merge was not read."])

    script = str(Path(__file__).resolve().parent / "branch.py")
    try:
        # 120s, raised from 90 with #846's enrichment (#1064 review). That
        # enrichment bounds itself to `_declared_workflows.BUDGET_SECS` and
        # declines past it, so this raise is headroom rather than the thing
        # keeping the call inside its budget — a timeout here still publishes
        # UNKNOWN, which is the safe direction but is not an answer.
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
    # #846: the scope block is carried too, not only the four header lines. The
    # `Verdict:` clause names the un-covered workflows itself, so this report is
    # correct without the block — but the block adds each one's triggers, which
    # is what tells a reader whether an absence is expected. Captured as a
    # region rather than by prefix: its continuation lines are indented and a
    # fifth prefix would have missed them exactly as the first four did.
    in_scope_block = False
    # `_untrusted.split_lines`, never `str.splitlines()` (#1105). Every branch
    # below anchors at column 0, and one of them — `Branch <default>: ` — is
    # what sets the state this whole report publishes about the branch after
    # the merge. `gh-branch` flattens the workflow names, paths and triggers it
    # prints, so nothing reaches here today that could open a second record;
    # this is the structural half of that guarantee rather than a restatement
    # of it, because the two live in different files and only one of them is
    # in the merge path.
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


def _repo_identity() -> tuple[str, str, str]:
    data, err = _gh_json(["repo", "view", "--json",
                          "nameWithOwner,defaultBranchRef"]
                         + _repo_target.gh_args(), timeout=20)
    if err or not isinstance(data, dict):
        return ("", "", err or "gh repo view returned no data")
    ref = data.get("defaultBranchRef") or {}
    return (str(data.get("nameWithOwner") or ""),
            str(ref.get("name") or "") if isinstance(ref, dict) else "", "")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _usage() -> str:
    return ("ERROR: usage: gh-pr-merge:NUMBER[:squash|:merge|:rebase][|force]. "
            "Without |force it previews the gate and merges nothing.")


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != ""]
    force = False
    cleaned: List[str] = []
    for a in argv:
        if a.endswith("|force"):
            force = True
            a = a[:-len("|force")]
        if a == "force":
            force = True
            continue
        if a:
            cleaned.append(a)

    if not cleaned or not cleaned[0].isdigit():
        print(_usage())
        return 1

    number = cleaned[0]
    method = "squash"
    for a in cleaned[1:]:
        if a in MERGE_METHODS:
            method = a
        else:
            print(f"ERROR: unrecognised token {a!r}. "
                  f"Merge methods: {', '.join(MERGE_METHODS)}. {_usage()}")
            return 1

    repo, default_branch, ident_err = _repo_identity()
    if ident_err and not repo:
        print(_repo_target.no_repo_error(f"gh-pr-merge:{number}"))
        return 1

    pr, err = _gh_json(["pr", "view", number, "--json", _PR_FIELDS]
                       + _repo_target.gh_args())
    if err or not isinstance(pr, dict):
        print(f"ERROR: PR #{number} could not be read "
              f"{_repo_target.not_found_scope()}: {err}. "
              f"{_repo_target.not_found_hint()}")
        return 1

    _pr_mod = _load_pr_module()
    # Four values since #1181, and this unpack is a seam across two files
    # loaded independently: every gate test replaces `_load_pr_module` with a
    # double, so the double and this line agreed with each other while the real
    # tuple grew, and nothing failed. `test_gh_pr_merge_tally_seam_1181.py`
    # is the check that brings the two into contact.
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

    allowed, gate_lines = gate(pr, declared, missing, reason)
    if not allowed:
        print("## Gate")
        for line in gate_lines:
            print(line)
        print()
        print(f"Nothing was merged. This op has no green-bypass: if you "
              f"disagree with the refusal, the manual route is "
              f"`gh pr merge {number} --{method}`.")
        print(f"[result] REFUSED — PR #{number} was not merged. "
              f"Reasons above; nothing changed.")
        return 1

    states = _checks.github_states(pr.get("statusCheckRollup"))
    authorising = _checks.summarize(states)
    print("## Gate — passed")
    print(f"  checks:      {authorising}")
    print(f"  reconciled:  {len(states)} legs read, {declared} declared")
    print(f"  mergeable:   {_checks.normalize(pr.get('mergeable'))} / "
          f"{_checks.normalize(pr.get('mergeStateStatus'))}")
    print()

    if not force:
        _publish_safety.require_confirm(
            f"gh-pr-merge #{number} ({head} -> {base}, {method})",
            f"{title}", force=False)

    # ---- the irreversible half ------------------------------------------
    try:
        merged = _gh(["pr", "merge", number, f"--{method}"]
                     + _repo_target.gh_args(), timeout=90)
        merge_err = "" if merged.returncode == 0 else (
            (merged.stderr or merged.stdout).strip() or
            f"gh exited {merged.returncode}")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        merge_err = f"gh pr merge did not complete: {e}"

    after, read_err = _gh_json(
        ["pr", "view", number, "--json",
         "state,mergedAt,mergeCommit,headRefName"] + _repo_target.gh_args())

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

    # ---- did the issues actually close? ---------------------------------
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

    # ---- the default branch, which the PR's green said nothing about ----
    branch_state, branch_lines = _default_branch_report(
        default_branch, repo, merge_sha)
    print(f"## Default branch after the squash — {default_branch or '?'}")
    for line in branch_lines:
        print(line)
    print()

    # ---- cleanup, named and NOT run -------------------------------------
    print("## Cleanup — not run by this op")
    if m_state != MERGED or not head:
        print("  Skipped — the merge is not confirmed, so nothing about this "
              "branch is safe to delete.")
    elif not _refname.ordinary(head):
        # A printed command is run by the reader, not by this op, and neither
        # available treatment makes this one both correct and safe: shell
        # quoting stops the shell acting on the name but leaves a U+2028 in it,
        # so the line still renders as three (#965); flattening fixes the render
        # and changes the ref, which is a delete command aimed at a branch that
        # is not this one. `_refname` calls this the convenience case — the
        # branch has a delete button on the PR page — so the op declines the
        # command rather than emitting a wrong or unsafe one, and says which.
        print(f"  Head branch {_untrusted.flat(_refname.shell_ref(head))} "
              f"still exists.")
        print("  No delete command is printed for it: the name contains "
              "characters a shell acts on or a terminal breaks a line at, and "
              "a command that is safe to paste would no longer name this "
              "branch. Delete it from the PR page, or by hand after reading "
              "the name above.")
    else:
        safe_head = _refname.shell_ref(head)
        ref_path = _refname.shell_ref(
            _repo_target.api_path("git/refs/heads/" + head))
        print(f"  Head branch `{safe_head}` still exists. "
              f"Delete it when you are done: gh api -X DELETE {ref_path}")
        print(f"  Local worktree, if any: git worktree remove <path> && "
              f"git branch -d {safe_head}")
        print("  Deliberately not chained: a merge and a delete in one command "
              "once deleted the branch and auto-closed the PR after the merge "
              "had failed on a conflict.")
    print()

    print(result_line(m_state, issue_overall, branch_state))
    return 0 if (m_state == MERGED and
                 issue_overall in (ALL_CLOSED, NONE_DECLARED)) else 1


if __name__ == "__main__":
    sys.exit(main())
