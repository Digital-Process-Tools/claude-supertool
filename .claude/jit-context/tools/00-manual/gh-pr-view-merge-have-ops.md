---
title: "`gh pr view` and `gh pr merge` are hand-rolled renders of ops this repo ships"
tool: Bash
match: ~(^|[;&|] *)(rtk +)?gh[[:space:]]+pr[[:space:]]+(view|merge)
mode: block
---

Both have an op. Hand-writing the projection is how a field gets omitted, and hand-writing the
merge gate is how `CANCELLED` got counted as pending (#454).

## `gh pr view` to `gh-pr:NUMBER_OR_BRANCH`

| What you were about to ask                   | The op                     |
| -------------------------------------------- | -------------------------- |
| `--json state,mergeable,statusCheckRollup`   | `gh-pr:N:status` (~250B)   |
| `--json body`, description, `Closes` lines   | `gh-pr:N`, `gh-pr:N:full`  |
| `--json files`, blast radius by filename     | `gh-pr:N:diff`             |
| one file s hunks                             | `gh-pr:N:diff:PATH`        |
| everything at once                           | `gh-pr:N`                  |

## `gh pr merge` to `gh-pr-merge:N:squash`

Without `|force` it previews the gate and merges nothing. What it does that the raw command
cannot:

- **Refuses first**, on the #454 arithmetic: not OPEN, draft, CONFLICTING, `mergeable=UNKNOWN`,
  zero check runs, an unreadable rollup, and every leg that is not a pass named individually
  because cancelled, skipped, timed_out, neutral and action_required are each not permission.
  There is no green-bypass; a refusal names the manual command instead.
- **Reads `state`/`mergedAt`/`mergeCommit` back off the remote.** A zero exit is not a merge.
- **Checks every linked issue individually**, reconciling the body s own closing refs against
  GitHub s `closingIssuesReferences`, so a ref GitHub never bound is named. That is PR #908 s
  shape exactly: the body said `Closes #899`, GitHub bound nothing, no error anywhere.
- **Reports the default branch after the squash.** A green PR is a statement about its
  merge-base, not about master once the squash lands.
- Exits 0 only when the merge is verified AND every linked issue is verified closed.

## The measurement

One maintainer tick on 2026-08-10 ran **12** `gh pr view` invocations across four PRs, every one
answered by `gh-pr:N` or `gh-pr:N:diff`. Two of the twelve were reading `Closes` lines out of a
body with a pattern match, which is the check `gh-pr` performs with the same closing-reference
reader `gh-pr-merge` uses to catch a malformed `Closes #A B` before the merge rather than after.

## Why blocking rather than reminding

A bare PR number resolves against the CWD s forge. Run from the wrong root, `gh pr view 1057`
once returned a well-formed answer about a GitLab MR. `gh-pr` and `gh-pr-merge` both honour
`repo:OWNER/NAME`. The failure mode is a correct-looking render of a different repository, which
a skimmed reminder does not stop.

**The tell that this rule fired correctly:** you were about to pipe a `gh` read into a `--jq`
expression. That means an op s render is being rebuilt by hand.

## What still legitimately needs raw `gh`

Tagging, releasing, deleting a ref, re-running a workflow. Reads go through supertool, those
writes do not have an op yet.

## Two things this rule got wrong before it worked

**`\s` is not a character class in the awk that runs the hook.** The matcher is
`match(tolower(full_command), ...)` in `scripts/pre-tool-hook.sh:80`, and macOS ships the
one-true-awk (version 20200816), where the undefined string escape is dropped, so the pattern
becomes the regex `ghs+pr` and matches nothing. Measured on this machine 2026-08-10:

    backslash-s : no
    space-plus  : MATCH
    posix class : MATCH

This rule was written, indexed, and did not fire — the command it was written for ran twice and
returned. The same defect had been sitting in `merged-is-not-ancestry.md` since it was written:
a `block` listed in the index, reading as enforced, that had never once fired. Both are now
`[[:space:]]+`. Use a POSIX class; it is portable across one-true-awk and gawk.

**An unanchored pattern matches the payload, not the invocation.** The first working version
blocked the very `append` that was writing this file, because the prose quotes the command it
forbids. The regex is deliberately tested against the whole command (so a rule survives
`cd X && ...`), which means position is the only thing separating an invocation from a mention.
Hence the `(^|[;&|] *)` prefix: start of command, or just after a chain operator.

The hook logs its haystack to `.claude/jit-context/.discovery/logs/hooks.log`. A rule that is not
firing shows as `(none) [shown:0]` on the exact command it was written for — that log is how to
tell a rule that never matched from a rule that never ran.
