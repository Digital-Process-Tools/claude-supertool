---
title: "gh issue/pr list without --limit returns one page, and a short page looks like the whole board"
tool: Bash
match: ~(^|[;&|\n] *)(rtk +)?gh[[:space:]]+(issue|pr)[[:space:]]+list
mode: remind
require: --limit
---

`gh issue list` and `gh pr list` return **one default page**, not the set. A capped read is indistinguishable from a complete one — there is no marker in the output saying more exist.

**Measured 2026-08-09.** `gh issue list --state open --milestone v0.31.0` with no `--limit` returned a page I counted as **31** stale-milestone issues and reported as fact. The real number was **72**. An agent re-measured with a limit and corrected it.

Pass `--limit` explicitly and make it larger than you expect the answer to be:

```bash
gh issue list --state open --limit 200 --json number,title
```

**Better: use the op.** `gh-issues` renders the board, and when it caps it *says so* — `capped at --limit 50 — more may exist`. Raw `gh` just hands you a short list:

```
supertool 'gh-issues:per=100'
supertool 'gh-issues:nomilestone'
supertool 'gh-issues:label=cohort-3,per=100'
supertool 'gh-prs:state=open'
```

**Neither `gh-prs` nor `radar` filters by author any more** — bare is the whole repo (#1207, then #1230 for radar's tier, which had kept the default the op dropped). This line said the opposite until 2026-08-10; adding `anyauthor` widens nothing. `gl-mrs` is the one that still narrows — see `op-defaults-that-narrow.md`.
**Pinned to command position since #1415, and narrower is not precise.** The match
used to be a bare substring test over the whole command, so it refused a PR body that
merely *named* the raw command inside a quoted heredoc — twice, once while filing the
issue about it. Anchoring stops that, and stops a path component. It does **not** stop
a heredoc line that starts with the command: only a tokeniser can tell a body from a
command, and that tokeniser lives in `claude-jit-context`, a separate repository.
