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
* **Nothing is deleted unless you ask for it.** Chaining a branch delete onto a
  merge once deleted the branch and auto-closed the PR when the merge had
  actually failed on a conflict. By default the cleanup command is printed and
  never run. The opt-in `cleanup` token (#1256) runs it — but only downstream of
  the read-back above, which is the gate that incident lacked, and with three
  states per item so a cleanup that could not run never renders as one that had
  nothing to do.
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
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
import _checks  # noqa: E402
import _declared_legs  # noqa: E402
import _publish_safety  # noqa: E402
import _refname  # noqa: E402
import _repo_target  # noqa: E402
import _untrusted  # noqa: E402
import _digits  # noqa: E402  (the one ASCII-digit test — #1727)


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

#: `isCrossRepository` is here for the cleanup arm, and it is the field whose
#: absence cost #1281: the head branch of a PR is named by whoever opened it,
#: opening one from a fork needs no permission on this repo, and every delete
#: downstream lands on the **base** repo. Without this field nothing in the op
#: could tell a branch of ours from a fork branch wearing its name.
_PR_FIELDS = (
    "number,title,state,isDraft,mergeable,mergeStateStatus,reviewDecision,"
    "baseRefName,headRefName,headRefOid,isCrossRepository,url,body,"
    "statusCheckRollup"
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

    **`lines` holds two kinds and only one of them decides.** A *refusal* says
    `REFUSED:` and withholds the merge; a *note* is disclosure and withholds
    nothing. `allowed` is computed from the refusals alone, and the invariant
    is one sentence: `allowed is False` if and only if at least one line says
    `REFUSED`. `tests/test_gh_pr_superseded_checks_1792.py` asserts it as a
    property over several rollups rather than as one more example, because
    the defect it is pinning was not in any branch — it was here, in the return
    contract, where every branch meets.

    The caller may print every line on either path. Nothing in `lines` is
    conditional on the verdict, and a note printed only when the gate refuses
    would be a disclosure that disappears exactly when the merge happens.
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

    # --- the verdict comes from the refusals, never from the line count ------
    #
    # This used to be `return (not lines, lines)`, and the superseded
    # disclosure was appended to that same list on the passing path — so a line
    # whose entire purpose is to say "these legs are NOT counted red" decided
    # that the merge was refused. The maintainer read the note, then
    # `[result] REFUSED`, with zero `REFUSED:` lines to act on, and was then
    # offered the raw `gh pr merge` escape hatch — routed onto the path with no
    # leg reconciliation and no post-merge read-back, for no valid reason
    # (#1792, second round).
    #
    # It hid because `named_disclosure` skips the passed and pending buckets:
    # a superseded *pass* — the ordinary case, a re-push — emits no lines and
    # flipped nothing. Only a superseded *failure* emits, and that is precisely
    # the reported case. Four merges went through the afternoon it shipped.
    #
    # `notes` is disclosure and can never decide anything. Two lists rather
    # than one, and the split is enforced below rather than trusted: a parallel
    # list drifts, and the direction that matters is a refusal misfiled as a
    # note, because that clears an irreversible merge past a stated objection.
    notes = _checks.superseded_disclosure(
        [(_untrusted.flat(n), s, k, i)
         for n, s, k, i in _checks.github_named_superseded(
             pr.get("statusCheckRollup"))])

    # Fail closed, and say so. A note spelling a refusal is treated as one
    # rather than trusted for being in the notes list — trusting the list it
    # arrived in is the assumption that produced this bug.
    #
    # **Anchored at line start, and that is not a detail.** This was
    # `"REFUSED" in n` for one commit, an unanchored substring over lines that
    # render check-run names verbatim — and a check-run name is whoever writes
    # the repo's workflow files, down to a matrix value interpolated from
    # branch metadata. A job called `connection REFUSED`, which is an ordinary
    # name for a job that tests one, then blocked every merge of every pull
    # request carrying it. That is this fix's own defect class pointed at
    # itself, one layer further in: remote text reaching a decision.
    #
    # The anchor closes it because `named_disclosure` renders a name mid-line,
    # after `  superseded failed: `, and `_untrusted.flat` has already taken
    # the newline that would be the only other way to reach column 0. So a
    # name cannot spell this, and the tool's own refusals — which all begin
    # `REFUSED:` — still can.
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

    # Two counts, and they answer different questions (#1792). `states` is
    # every leg on the sha and feeds the reconciliation below, which asks
    # whether the tally *covers* what the runs declare. `live` drops the legs a
    # later run of the same name replaced and is what decides green — GitHub
    # evaluates a required check on its latest run, so refusing on a superseded
    # failure refused a pull request the forge called `clean`, permanently: a
    # concluded check run cannot be withdrawn by any trigger the maintainer has.
    states = _checks.github_states(rollup)
    live = _checks.github_live_states(rollup)
    tally = _checks.summarize_github(rollup)
    out: List[str] = []

    if not _checks.all_green(live):
        named = [(_untrusted.flat(n), s, k, i)
                 for n, s, k, i in _checks.github_named_live(rollup)]
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
        # Two decisions, not one (#1648). The split decides *which* segment is
        # the error, and `str.splitlines()` cuts on U+2028 — so a server that
        # controls the body chose that segment, and the real error was dropped.
        # `_untrusted.split_lines` cuts on LF/CR/CRLF only, so the last line is
        # the last line; `flat()` then keeps the chosen segment on the one line
        # it is rendered on, which for this helper is column 0 inside the merge
        # gate's own receipt (`ERROR: PR #N could not be read ...: {err}`).
        tail = _untrusted.split_lines((r.stderr or r.stdout).strip())
        return (None, _untrusted.flat(tail[-1]) if tail
                else f"gh exited {r.returncode}")
    try:
        return (json.loads(r.stdout or "null"), "")
    except json.JSONDecodeError:
        return (None, "gh returned invalid JSON")


# ---------------------------------------------------------------------------
# cleanup (#1256) — three states per item, and `idle` has to be earned
# ---------------------------------------------------------------------------

CLEAN_DONE = "done"
CLEAN_REFUSED = "refused"
CLEAN_SKIPPED = "skipped"

_CLEAN_ITEMS = ("local worktree", "local branch", "remote branch")

#: `git-worktrees`' own three-state tally, read off the render.
#:
#: It used to be read off the exit code, which is one integer standing in for a
#: board with three states — and #1282 is where that collapse became a delete
#: gate. `git-worktrees:PATH` matches every worktree above and below the path,
#: so a nested layout printed `3 occupied, 0 idle` and still exited 0, which
#: this consumer read as `idle` and as permission to remove the directory.
#:
#: This is not a second occupancy heuristic — the thing #1239 is about, and the
#: reason the exit code was read here in the first place. No state is computed
#: here; the op's own verdict is read at full width instead of through a lossy
#: channel. Anchored at column 0, where no flattened branch name can reach.
_TALLY_RE = re.compile(
    r"^\[result\] (\d+) occupied, (\d+) idle, (\d+) cannot tell")

#: How many of a tree's undeletable files to name in a refusal before counting.
_DIRT_SHOWN = 5

_WORKTREES_PY = Path(__file__).resolve().parent.parent / "git" / "worktrees.py"


def _git(args: List[str], timeout: int = 30):
    # --no-optional-locks precedes the subcommand -- a git global flag
    # (#1945, same mechanism as #1944). This chokepoint runs both reads
    # (rev-parse, worktree list, status, ls-files) and writes (branch -d,
    # worktree remove) -- the flag suppresses OPTIONAL locks only, verified
    # harmless against real git 2.46.2 for both `branch -d` and `worktree
    # remove`, so a blanket edit here is a no-op on the writes and closes the
    # index.lock hole on every read without having to enumerate call sites.
    return subprocess.run(["git", "--no-optional-locks"] + args, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _git_rc(args: List[str], timeout: int = 30) -> tuple[int, str]:
    """`(returncode, message)`, with a spawn failure as a *reason*, not a raise.

    Windows raises `FileNotFoundError [WinError 2]` where POSIX may not fail at
    all, and an uncaught one here would escape the cleanup arm entirely — the
    #997 shape, where a new subprocess call skipped its own "the tool failed"
    branch and the original defect came back.
    """
    try:
        r = _git(args, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return (127, f"git could not be run: {e}")
    if r.returncode == 0:
        return (0, "")
    msg = ((r.stderr or r.stdout) or "").strip()
    return (r.returncode, msg or f"git exited {r.returncode}")


def _worktrees_for_branch(branch: str) -> List[str]:
    """Every worktree of this repository with `branch` checked out.

    A list rather than one path: two worktrees can hold the same branch only in
    unusual states, and picking the first of them would be a guess about which
    one to delete.
    """
    try:
        r = _git(["worktree", "list", "--porcelain"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0:
        return []
    paths: List[str] = []
    current = ""
    # `--porcelain` does not quote a path, so a branch or directory name can
    # carry a separator U+2028/U+2029 that str.splitlines() splits on and git
    # does not (#1119). Narrowed rather than registered: this reader decides
    # which directory gets deleted.
    for line in _untrusted.split_lines(r.stdout or ""):
        if line.startswith("worktree "):
            current = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref == f"refs/heads/{branch}" and current:
                paths.append(current)
    return paths


def _worktree_state(path: str) -> str:
    """`idle` / `occupied` / `cannot tell` for one worktree, via `git-worktrees`.

    A probe that did not run answers `cannot tell`, never `idle`: "no evidence
    of an agent" is exactly what the check that caused #860 already said.

    `idle` needs a board of **exactly one** row, and that row idle. The op's
    path filter is ancestor-or-descendant, so a nested worktree pulls in the
    trees above and below it; a board of three says nothing about the one that
    was asked for, and before #1282 that board exited 0 and was read as `idle`.
    """
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


#: Settings that decide what a read of a worktree is even willing to mention,
#: pinned on the command line where they outrank every config file and
#: `GIT_CONFIG_*` both. `status.showUntrackedFiles=no` is an ordinary user or
#: repo preference and it suppresses `!!` as well as `??`, so the #1280 gate
#: inherited it, received an empty list, and deleted an ignored `.env` while
#: reporting that it had looked (#1290).
#:
#: `core.quotePath` is not what keeps a path with a newline in it on one line —
#: git quotes C0 control characters unconditionally, whatever this is set to,
#: which is why splitting the output on newlines is safe at all. It governs
#: bytes >= 0x80, rendering an accented filename as octal escapes, so the
#: refusal text is ASCII whatever codec the console decoded with.
_DIRT_PINS = ["-c", "status.showUntrackedFiles=normal",
              "-c", "core.quotePath=true"]


def _dirt_read(path: str, argv: List[str]) -> tuple[str, str]:
    """`(stdout, error)` for one read of what a worktree holds.

    A spawn failure is a *reason*, not a raise: Windows raises
    `FileNotFoundError [WinError 2]` where POSIX may not fail at all, and an
    uncaught one here would escape the cleanup arm entirely (#997).
    """
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
    """`(paths, error)` — everything in the tree a removal would destroy.

    `--ignored` is the whole point. `git worktree remove` without `--force`
    declines a tree holding **modified or untracked** files, and that is the
    entire scope of the flag: an **ignored** file is deleted either way
    (#1280). Nothing recovers one — not the index, not a stash, not the remote,
    which is what ignoring it meant. A local env file, a virtualenv and a
    scratch database are the three that turn up.

    An empty answer here is the authorisation for that deletion, so it is not
    taken from one read. Two are used, and they fail in different ways:

    * `git status --porcelain --ignored` is the only one that sees **modified
      tracked** files, but its untracked half is a *display* setting — the one
      #1290 turned off underneath the guard;
    * `git ls-files --others` is plumbing. It has no display configuration to
      suppress, so it answers the untracked-and-ignored half by a mechanism
      the first read's failure mode cannot reach.

    Pinning `_DIRT_PINS` closes that instance; requiring both reads is what
    closes the shape. If **either** read cannot be performed the caller gets
    the reason and no list, because a tree that was never established is not a
    tree to delete, and `[]` here would be indistinguishable from an empty one.
    """
    raw: List[str] = []

    # `=normal` and not `=all`: the flag is here so the command line, not a
    # config file, is the last word on whether untracked files are mentioned —
    # but `all` also defeats git's own directory collapse, and an untracked
    # `venv/` would then arrive as ten thousand `??` lines beside the single
    # collapsed entry the second read returns for it. `normal` names the
    # directory, which is all a refusal needs.
    out, err = _dirt_read(path, ["status", "--porcelain", "--ignored",
                                 "--untracked-files=normal"])
    if err:
        return ([], err)
    for line in _untrusted.split_lines(out):
        # `XY path`: two status columns and a space. Anything shorter is not a
        # porcelain record, and an empty list here is about to authorise a
        # deletion — so a line that cannot be read is dropped rather than
        # turned into a path.
        if len(line) > 3:
            raw.append(line[3:].strip())

    out, err = _dirt_read(path, ["ls-files", "--others", "--directory",
                                 "--no-empty-directory"])
    if err:
        return ([], err)
    # `--directory` collapses a wholly-untracked directory to one entry, which
    # keeps a virtualenv from filling the refusal with ten thousand paths; a
    # partially-tracked one is still listed file by file, so nothing is hidden
    # by the collapse.
    for line in _untrusted.split_lines(out):
        raw.append(line.strip())

    # Both reads answer about overlapping sets, and the refusal counts what it
    # names — so the same path arriving twice is one file, not two.
    return ([p for p in dict.fromkeys(raw) if p], "")


def _cleanup_worktree(head: str) -> tuple[str, str, str]:
    """Remove the branch's worktree, once its contents have been established.

    Two gates, and they answer different questions: `git-worktrees` says
    whether an agent is alive in the tree, and `_worktree_dirt` says whether
    removing it destroys anything. Only the first existed before #1280, and the
    refusal text justified the second on the grounds that no `--force` is
    passed — the one sentence that is not true. A wrong safety claim is worse
    than no claim, because it ends the next reader's search.

    Which is why the second gate has three answers and not two. A read that did
    not happen returns a reason, and the arm below prints that reason instead
    of the removal receipt — #1290 was this same arm printing prose asserting a
    check it had inherited a config setting out of performing.
    """
    item = "local worktree"
    paths = _worktrees_for_branch(head)
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
    """`(sha, error)` for what `refs/heads/<head>` points at **in this repo**.

    `git/ref/heads/X` — singular `ref` — is the exact-match read; the plural
    `git/refs/heads/X` is a prefix search and answers with a list. A list here
    would mean the name did not identify one ref, which is not something to
    delete.
    """
    data, err = _gh_json(["api", _repo_target.api_path("git/ref/heads/" + head)])
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
    """Delete the head ref — after something establishes that it *is* the head.

    The DELETE lands on the **base** repository, and `headRefName` is a name
    chosen by whoever opened the PR. Before #1281 that was the whole of it: a
    contributor whose fork branch was called `master` deleted ours, under a
    receipt reading `recoverable: GitHub keeps refs/pull/N/head` — false in
    precisely that case, because the ref deleted is not that PR's head.

    Reading the ref back and comparing it to `headRefOid` is what makes that
    sentence true, and it subsumes the name-shaped version of the question:
    `develop`, `release/1.0` and every other ref of ours fails it, because
    they do not point at this PR's head. It also declines a branch someone
    pushed to after the merge, which is the same defect one commit smaller.
    """
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
    ref_path = _repo_target.api_path("git/refs/heads/" + head)
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
                head_oid: str = "") -> List[tuple[str, str, str]]:
    """`(item, state, detail)` per item — done, refused, or skipped, never two.

    Four things have to be established before anything here is deleted, and
    before #1281 only the first was: that the merge happened, that the head
    branch lives in **this** repository, that it is not the default branch, and
    that the ref about to go is the one the PR names.

    The three added keywords default to their *unestablished* values on
    purpose. `cross_repo=None` means the field did not come back, and that is a
    refusal rather than an assumption — a caller who forgets to answer gets a
    refusal it can read instead of a delete it cannot undo.

    Gated on the merge that was **read back off the remote**, which is the gate
    the 2026 incident lacked: there, a delete chained with `&&` ran after a
    merge had failed on a conflict. Nothing here runs on an unverified merge.

    The remote branch goes through `gh api -X DELETE`, never `git push
    --delete`: the pre-push hook runs the entire suite per deletion, and 96
    branches that way is about three hours of pytest whose output looks like
    progress.
    """
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
    target = _repo_target.target()
    if target:
        why = (f"a repo target is set ({_untrusted.flat(target)}), so this "
               f"checkout is not that PR's repository — deleting a local "
               f"branch of the same name here would hit the wrong repo")
        rows.append(("local worktree", CLEAN_SKIPPED, why))
        rows.append(("local branch", CLEAN_SKIPPED, why))
    else:
        # Order is load-bearing: `git branch -d` cannot delete a branch that is
        # checked out in a worktree, so the tree goes first or the branch row
        # refuses for a reason that is this op's own doing.
        rows.append(_cleanup_worktree(head))
        rows.append(_cleanup_local_branch(head))
    rows.append(_cleanup_remote_branch(head, head_oid))
    return rows


def render_cleanup(rows: Sequence[tuple[str, str, str]]) -> List[str]:
    """The section body. A tally in three counts, because a cleanup that could
    not run must not render as a cleanup that had nothing to do."""
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


# ---------------------------------------------------------------------------
# how old is the base this tally was computed on (#1257)
# ---------------------------------------------------------------------------

#: One explicit string, not adjacent literals inside the argument list. The
#: two-literal form is the missing-comma shape — inside a list, `"a" "b"` and
#: `"a", "b"` differ by one character and mean an argument or two of them —
#: and a reviewer flagged it here before it cost anything.
_COMPARE_JQ = ("{behind_by, base_sha: .base_commit.sha, "
               "base_date: .base_commit.commit.committer.date}")


def base_distance(base: str, head_oid: str) -> tuple[int | None, str, str, str]:
    """`(behind_by, base_head_sha, base_head_date, error)` for the base branch.

    `compare/BASE...HEAD` is asked about `headRefOid`, never `headRefName`: the
    check tally belongs to a *commit*, and a ref name resolves to whatever the
    branch points at when the question is asked, which is a different commit
    the moment anybody pushes. `behind_by` is the field that answers this — the
    number of commits on the base that the tested tree does not contain.

    `--jq` rather than the whole reply: an ordinary compare carries the commit
    list and the file list, and nothing here reads either.
    """
    if not base or not head_oid:
        return (None, "", "", "the PR's base branch or head commit was not in "
                              "the API reply")
    data, err = _gh_json(
        ["api", _repo_target.api_path(f"compare/{base}...{head_oid}"),
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
    """The `## Base` block — three states, and the level one is printed too.

    Disclose, never block. Refusing a merge because the base moved would make a
    busy afternoon serial and take back what `changelog.d` fragments bought:
    four merges in one afternoon on 2026-08-07 with zero rebases. What went
    wrong on 2026-08-10 was not that a merge happened on an old base, it was
    that nothing on the receipt said the base was old — so the fix is the
    sentence, not the gate.

    The UNKNOWN arm is the point of writing this at all. An absent warning and
    an unasked question render identically, and that is the same shape as the
    tally defect one layer up.
    """
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
    return ("ERROR: usage: "
            "gh-pr-merge:NUMBER[:squash|:merge|:rebase][|force][|cleanup]. "
            "Without |force it previews the gate and merges nothing. "
            "|cleanup deletes the head branch and an idle worktree AFTER the "
            "merge is read back as MERGED, three states per item.")


def parse_argv(argv: Sequence[str]) -> tuple[str, str, bool, bool, str]:
    """`(number, method, force, cleanup, error)`.

    Tokens are split on `|` wherever they appear rather than matched as a
    suffix: supertool hands `gh-pr-merge:940:squash|force|cleanup` over as one
    argument, and the old `endswith("|force")` test saw `squash|force|cleanup`
    as a single unrecognised token.
    """
    tokens: List[str] = []
    for a in argv:
        for piece in str(a).split("|"):
            if piece:
                tokens.append(piece)

    # ASCII digits, not `str.isdigit()` (#1727) — this number is the one that
    # decides which PR gets merged, and it comes off the caller's op string.
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
        # `ident_err` is handed over rather than dropped (#1789). This site is
        # the reproduction: during a GraphQL outage the identity read failed,
        # the reason was discarded here, and `no_repo_error` rendered the
        # collapsed answer as "cwd is not a GitHub repo" in a working clone.
        # The error we already hold is contemporaneous with the failure, which
        # is better evidence than a second lookup a second later — and it
        # spares this terminal path a `gh repo view` it does not need.
        print(_repo_target.no_repo_error(f"gh-pr-merge:{number}",
                                         detail=ident_err))
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

    # Before the gate, not after it: without `force` this op previews and
    # merges nothing, and a disclosure printed downstream of the gate is absent
    # from every run a human reads before deciding (#1257).
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
              f"disagree with the refusal, the manual route is "
              f"`gh pr merge {number} --{method}`.")
        print(f"[result] REFUSED — PR #{number} was not merged. "
              f"Reasons above; nothing changed.")
        return 1

    states = _checks.github_states(pr.get("statusCheckRollup"))
    # The tally the gate actually authorised on, not a second one computed a
    # different way (#1792). `summarize(github_states(...))` here counted the
    # superseded legs as live failures, so the banner printed immediately
    # before an irreversible merge read `⚠ NOT ALL GREEN` under a heading
    # saying `Gate — passed` — the arithmetic that cleared the merge and the
    # arithmetic displayed beside it disagreeing, on the one line a reader
    # stops at. `reconciled:` below stays on the full count: that is the
    # coverage question and it has to see every leg.
    authorising = _checks.summarize_github(pr.get("statusCheckRollup"))
    print("## Gate — passed")
    print(f"  checks:      {authorising}")
    print(f"  reconciled:  {len(states)} legs read, {declared} declared")
    print(f"  mergeable:   {_checks.normalize(pr.get('mergeable'))} / "
          f"{_checks.normalize(pr.get('mergeStateStatus'))}")
    # The gate's notes, on the path where the merge actually happens (#1792).
    # `gate()` returns refusals and notes in one list and only the refusals
    # decide, so on this path every line here is disclosure. Printing them only
    # under `## Gate` — the refusal branch above — would make the superseded
    # legs visible in every case except the one where they stop mattering,
    # which is the case a reader is entitled to see them in.
    for line in gate_lines:
        print(line)
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

    # ---- cleanup ---------------------------------------------------------
    # A missing or non-boolean `isCrossRepository` becomes None, not False:
    # "the field did not come back" and "the head is ours" are different
    # facts, and only one of them authorises a delete — or the printing of a
    # delete command, which is the same decision made by the reader.
    x_repo_raw = pr.get("isCrossRepository")
    x_repo = x_repo_raw if isinstance(x_repo_raw, bool) else None

    if do_cleanup:
        print("## Cleanup — run by this op (`cleanup`)")
        for line in render_cleanup(
                run_cleanup(head, merged=(m_state == MERGED and bool(head)),
                            cross_repo=x_repo,
                            default_branch=default_branch,
                            head_oid=str(pr.get("headRefOid") or ""))):
            print(line)
        print()
        print(result_line(m_state, issue_overall, branch_state))
        return 0 if (m_state == MERGED and
                     issue_overall in (ALL_CLOSED, NONE_DECLARED)) else 1

    # "by this invocation", not "by this op": the op does clean up, and the
    # pointer saying so sat 69 lines below a header a reader stops at (#1670).
    # The three refusal arms below each say that `|cleanup` refuses on the same
    # ground, so this offer is never left standing where it would not be taken.
    print("## Cleanup — not run by this invocation (add `|cleanup`)")
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
              "branch, and `|cleanup` refuses it on the same ground. Delete it "
              "from the PR page, or by hand after reading the name above.")
    elif x_repo is not False or not default_branch or head == default_branch:
        # The same establishment #1281 put in front of `cleanup`, in front of
        # the printed commands — because these are the *default* path and a
        # reader runs them. Quoting was the only thing between a fork branch
        # called `master` and `gh api -X DELETE …/refs/heads/master` aimed at
        # this repository, and quoting makes a wrong command safe to paste
        # rather than making it right. Both facts are local: no extra call.
        #
        # The conditions are tested in `run_cleanup`'s order, and the reason
        # below is chosen in that same order, so the two arms cannot report
        # different grounds for the same refusal. `not default_branch` was the
        # one this arm was missing (#1292): an empty default branch satisfies
        # neither `x_repo is not False` nor `head == default_branch` — `""` is
        # not the head — so it fell straight through to the printed DELETE
        # while `run_cleanup` refused all three items on it. Empty is a
        # reachable answer, not an impossible one: `_repo_identity` returns it
        # whenever the API reply lacks `defaultBranchRef.name`, and the
        # section header above already renders `default_branch or '?'`.
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
    else:
        safe_head = _refname.shell_ref(head)
        # `api_path_for_display`, not `api_path`: this string is pasted by a
        # reader, and gh's `{owner}`/`{repo}` are resolved from whatever cwd it
        # is pasted into. The concrete slug is already on screen twice above
        # (#1670). Every *executed* path in this file still goes through
        # `api_path`, which a repo target must keep replacing rather than
        # accompanying (#1281).
        ref_path = _refname.shell_ref(
            _repo_target.api_path_for_display("git/refs/heads/" + head, repo))
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

    print(result_line(m_state, issue_overall, branch_state))
    return 0 if (m_state == MERGED and
                 issue_overall in (ALL_CLOSED, NONE_DECLARED)) else 1


if __name__ == "__main__":
    sys.exit(main())
