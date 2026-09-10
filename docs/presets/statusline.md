# statusline — a bar for Claude Code's `statusLine` hook, with no network calls

`statusline` reads the JSON blob Claude Code's `statusLine` hook hands its command
on stdin (`workspace.current_dir`, model info, ...) and prints one line, suitable
directly as `statusLine.command` in `settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/supertool.py statusline"
  }
}
```

Every team that wants a useful status bar has been writing its own throwaway
shell script against this contract. [#1850] is that script, generalised.

## The hard constraint: no network call, ever

This runs after every assistant message, debounced at 300ms. A status line that
shells out to `gh`/`glab` synchronously adds its full round-trip latency to every
single turn — unusable. **The render path never touches the network.**

## Two independent axes

**Render** is width only. A local segment computes live and prints a compact
line — `git-status`'s own multi-line dashboard becomes `master up2 dirty3` here.
No cache, no fragment, no publish step.

**Data source** is what actually varies:

| Segment | How it gets its data | Fragment needed |
| --- | --- | --- |
| `session` | the stdin blob alone | no — local |
| `git-status` | a live `git status`, under a sub-second budget | no — local |
| `gh-pr` | reads what `gh-pr` last published | yes |

A network-backed op publishes its own fragment as a **side effect of running
normally** — `gh-pr` writes the check tally it already reconciled via
`presets/_checks.py`, so there is never a second verdict path that could
disagree with `radar`/`gh-pr` itself ("share the model, not the op"). The
statusline op only ever *reads* that fragment; it makes no call of its own.

Consequences that are by design, not bugs:

- **Coverage is sparse.** A fragment exists only if `gh-pr` has been run in this
  worktree this session. `checks: unknown (not run this session)` is the normal
  state, not an error — see it as often as you have not just run `gh-pr`.
- **Staleness is normal, not exceptional.** Nothing refreshes a fragment except
  running its op again. Past `ops.statusline.stale_secs` (default 300), the
  render says so explicitly (`checks: ... (stale 42m)`) instead of presenting
  an old value as current.
- **Fragments are atomic and worktree-keyed** (`presets/_statusline_fragments.py`)
  — temp file + rename, never read-modify-write, keyed by a hash of the
  enclosing git worktree root (found by walking up for a `.git` entry, never
  a `git` subprocess) so a fragment `gh-pr` publishes from a monorepo
  subdirectory is still found when `statusline` reads it from
  `workspace.current_dir`, and sibling worktrees never share a slot.
- **Fragments are swept by `gc`**, not left to accumulate forever: `statusline`
  is a registered `gc` kind (`_GC_DEFAULT_RETENTION_DAYS["statusline"]`, 7
  days by default, configurable the same way every other kind is) — see
  docs/configuration.md's `gc` section (or `help:gc`) for the mechanism.

A local segment gets a **budget**, not a cache: `git-status`'s statusline
segment times out in well under a second (`ops.statusline.git_budget_secs`,
default `1.5`) and renders `git:unknown` rather than stalling the bar — a much
tighter budget than the `git-status` op's own 15s validator patience (#1882),
because a status line has none.

## Configuration

```json
{
  "ops": {
    "statusline": {
      "groups": [["session"], ["git-status"], ["gh-pr"]],
      "item_sep": " · ",
      "group_sep": " | ",
      "stale_secs": 300,
      "git_budget_secs": 1.5
    }
  }
}
```

Unconfigured (no `ops.statusline.groups`) **refuses** rather than inventing a
default. A segment name this op does not know how to render also refuses, by
name (`` `radar` declares no statusline render ``), rather than being silently
dropped or rendered blank.

### Why nested arrays, never a flat one with literal separators

Two levels of separator are needed because a real bar has two: an item
separator inside a group, a group separator between groups. A flat array
mixing op names with literal separator entries was considered and rejected —
under this design a missing segment is the *normal* case (a fragment that was
never published), so a literal separator sitting next to an absent neighbour
produces dangling or doubled separators on every render where a segment has
nothing. Nesting makes the separator a **join**, emitted only between groups
that actually rendered something, and there is no entry type in the array that
is neither a segment name nor a structural level — which is also what keeps
this a pure configuration format with **no shell** in it: `.supertool.json` is
tracked, so a literal-command array would execute whatever it contained on
every clone, unprompted, several times a minute.

## Segments shipped in this slice

- **`session`** — from the stdin blob alone: `model.display_name`, falling back
  to `model.id`, `session unknown` if neither is present. The stdin contract is
  Claude Code's, not this repo's, and is read defensively throughout — a
  reshaped or missing field renders `unknown`, never a crash, because a status
  line that throws is worse than one that is briefly wrong (the harness
  surfaces the stderr on every turn).
- **`git-status`** — branch, ahead/behind counts, dirty file count, from a live
  `git status --porcelain=v2 --branch` under the budget above. This is a
  separate, minimal implementation from `presets/git/status.py`'s own
  multi-line dashboard rather than a reuse of it — that op's `main()` is a
  print-oriented CLI script, not a function returning structured data, and
  refactoring it to serve both was out of scope for this slice.
- **`gh-pr`** — the fragment `gh-pr` publishes, described above.

## Not in scope (deferred, not forgotten)

- **Per-op statusline declarations in the preset manifest.** The design
  discussion settled on "a statusline render is a declaration on the op, next
  to `syntax`/`replaces`, so `ops` can list which ops are statusline-capable
  with no second registry to drift." This slice instead hardcodes a small
  segment registry inside `presets/statusline/statusline.py`. Extending the
  manifest schema and the `ops` renderer to carry this per-op is real,
  separate work.
- **Multi-line output.** Claude Code renders each printed line as its own row;
  this op prints exactly one line today. A third nesting level (rows, each
  with its own group/item separators) is the natural extension and was left
  for a follow-up rather than guessed at.
- **`radar`, `gl-mr` and other network-backed segments.** Only `gh-pr` publishes
  a fragment in this slice. Any op that wants a statusline segment needs its
  own `_statusline_fragments.publish()` call, wired the same way `gh-pr`'s is.
- **Width/truncation policy.** Terminals are not infinite and this line renders
  every turn; nothing here truncates.
- **A refresher/backfill process.** Nothing pre-warms a fragment. The first
  render after a fresh clone (or before `gh-pr` has ever run) is `unknown`,
  by design.

Anything writing to the repo, spawning a process, or making the render path
conditional on network availability is explicitly out of scope, per [#1850]
itself.

[#1850]: https://github.com/Digital-Process-Tools/claude-supertool/issues/1850
