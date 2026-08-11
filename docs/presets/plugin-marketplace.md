# `plugin-marketplace` — did this release reach anyone?

```
supertool 'plugin-marketplace'          # this repo's plugin, from .claude-plugin/plugin.json
supertool 'plugin-marketplace:NAME'     # another plugin, by catalogue name
```

Requires `gh`. Reads two public catalogues and this clone's git history. Writes nothing.

## The question

A Claude Code plugin reaches users through a **catalogue**, and a catalogue entry pins a **commit sha**:

```json
{ "name": "supertool",
  "source": { "source": "url", "url": "https://github.com/...", "sha": "dcb574ea..." } }
```

Tagging a release does not move that pin. The updater compares manifest versions at the pinned sha, so six releases can land on `master` — tags, changelog, GitHub release and all — and reach **nobody** installed through the catalogue. Nothing in the release itself says so.

Measured against the live catalogues on 2026-08-11, which is why this op exists:

| | |
| --- | --- |
| community pin | `dcb574ea` — the v0.27.0 release commit, 2026-08-08 |
| local `master` | `6a6347e` (v0.33.0) |
| distance | **101 commits, 6 releases** |
| bump PRs for supertool, ever | **1**, merged 2026-08-08 |
| official catalogue | supertool **not listed at all** — it is community-only |

## Why it is an op and not a snippet

Both hand-rolled routes return an **absence that reads like an answer** — this repo's defect class, on the one question whose whole point is "did it ship".

**The contents API answers HTTP 200 with an empty body.** The community manifest is ~1.5 MB and the JSON media type declines files over ~1 MB. It does not error:

```json
{ "path": ".claude-plugin/marketplace.json", "size": 1573735,
  "content": "", "encoding": "none" }
```

Fed to the documented one-liner that is a document with no plugins in it, and the output is `plugin not found` — attributed to the plugin instead of to the reader. The op asks for the file with `Accept: application/vnd.github.raw`, **and still checks for the envelope on the way back**, because a header that was sent is not a header that was honoured.

**Absent and stale render the same.** The official catalogue lists 13 plugins and supertool is not among them. A snippet prints the same nothing for "community-only" as for "listed but never bumped" — and those call for *opposite* actions. The first needs a **submission**; the second needs the automation that is bumping 2290 other plugins to run once more. So the render says which:

```
official   anthropics/claude-code
  state     not listed -- read 13 plugins, none named 'supertool'
            this needs a submission, not a bump
```

## Three states, never two

| State | Means |
| --- | --- |
| `listed` | the catalogue was read in full and holds this plugin; the pin, the distance and the bump PRs follow |
| `not listed` | the catalogue was read in full and does not hold it — an answer, and the population it searched is printed beside it |
| `skipped` | the catalogue could **not** be read, with the reason |

A `skipped` catalogue never renders as a row saying absent, in its own row or in the footer — the words "not listed" do not appear on screen at all when nothing was read. **Exit status is 1 whenever any catalogue went unanswered**, so an offline run is a red op rather than a clean board.

## What each row says

```
plugin-marketplace -- supertool 0.33.0, local HEAD 6a6347e

community  anthropics/claude-plugins-community
  state     listed among 2291 plugins
  pinned    dcb574ea  v0.27.0  2026-08-08
  distance  101 commits, 6 releases, 0.27.0 -> 0.33.0
  bump PRs  1 found, latest #1934 MERGED 2026-08-08 -- bump(supertool): 796166cc -> dcb574ea
            searched: bump(supertool) in:title (limit 30)
```

- **`pinned`** — the sha, the plugin manifest version *at* that sha, and its date. A sha alone says nothing; the version at it is the thing users have.
- **`distance`** — commits and tags between the pin and local `HEAD`, resolved from this clone. When it cannot be resolved the row is `skipped` and says which of the two reasons applies: the commit is not in this clone (fetch it), or it is here and carries no `.claude-plugin/plugin.json`, which fetching will not fix. `git show SHA:PATH` exits 128 for both.
- **`bump PRs`** — the catalogue's automation opens one PR per bump, titled `bump(NAME): old -> new`. **The search is printed under its own result**, because that title convention belongs to one repository's workflow and not to the ecosystem: if it is renamed, this renders as a query that found nothing rather than as a plugin that was never bumped. An `OPEN` bump PR gets its own line — that is a bump waiting on review, not a bump that never happened. Two things the forge's answer needs correcting for, both measured 2026-08-11: **the search is tokenized**, so `bump(claude) in:title` comes back with `bump(claude-mem)`, `bump(claude-hud)`, twelve more siblings and a `ci:` PR merely holding both words — only titles beginning `bump(NAME):` are kept, and the row says `kept N of M returned` whenever that narrowed anything; and **the order is relevance, not time**, since GitHub search defaults to best match with no `sort:` qualifier, so the list is re-sorted here rather than requested that way.
- **unpinned** — an entry with no `sha` tracks the repository default branch, so every release reaches users at once. That is the *opposite* of a stale pin and is not rendered as a missing one.

Titles come from another repository's tracker, so they are flattened to one line under a single disclosure line, the same trade `gh-prs` and `gl-mrs` make.

## The gate section

The community catalogue's bump PRs are opened by `app/github-actions`, and the body of every one of them reads:

> The new SHA was validated via `claude plugin validate` in [this workflow run] before this PR was opened.

Validation is therefore what decides whether a bump PR **exists at all**, which makes it evidence inside this op's own question rather than a separate one — if the pin has not moved in six releases, "does my HEAD still validate" is the next thing you want to know, not a different report.

```
gate       claude plugin validate -- ok: root: CLAUDE.md at the plugin root is not loaded...
           read the working tree; the catalogue's automation validates the sha it is about to pin
```

Two things it deliberately says out loud:

- **which tree it read.** `claude plugin validate` examines the working tree, uncommitted edits included. The automation validates the pushed sha it is about to pin. A green about the wrong tree is this repo's defect class wearing a CLI's exit code.
- **an absent `claude` CLI is `skipped` with its reason** — never a pass, never a failure. The gate section is omitted entirely for `plugin-marketplace:NAME`, since validating this working tree says nothing about somebody else's plugin.

A skipped gate does **not** red the op. It is evidence beside the question rather than the question, `claude` is not in this preset's `requires`, and a machine without it should not turn an op whose catalogues all answered into a failure. Exit 1 is reserved for a catalogue that went unread.

## Judgment calls, and why

**The catalogue list is hardcoded**, not configurable. It is a fact about the Claude Code plugin ecosystem rather than about anyone's project, and there are exactly two today. A config key would let a stale or partial local config silently *remove* a catalogue, at which point "listed nowhere" becomes a statement about the config rather than about the world — the exact substitution this op exists to stop. Adding a third is a code change with a test beside it.

**Plugin identity comes from `.claude-plugin/plugin.json`** in the repository root. A repository with no such manifest is not one this op can answer for, so it refuses with exit 2 and names both routes rather than printing an empty board. With `:NAME` the catalogue rows still render and the distance is `skipped` with its reason, because that pin points into a repository this clone does not hold.

**It does not predict when a bump will arrive.** The catalogue runs its workflow on a schedule, and a schedule says when the pin moves, never what it moves to. The honest render is the observed lag with the sample that produced it — which is why `bump PRs` carries a count and a search string rather than an estimate.

## Exit status

| Code | Meaning |
| --- | --- |
| 0 | every catalogue was read and answered |
| 1 | at least one catalogue is `skipped` — something went unanswered |
| 2 | no plugin to ask about: no `.claude-plugin/plugin.json` here and no `:NAME` given |

**A missing `gh` exits 1 here, not 0.** [contributing.md](../contributing.md) tells preset scripts to print a friendly message and exit 0 when their CLI is absent, so that an unrelated missing tool does not red a whole batched call. This op is the case that rule does not fit: `gh` is not incidental to it, it is the only reader, and with `gh` gone *every* catalogue is `skipped`. Exit 0 would hand back a board on which nothing was measured and nothing went wrong — which is the substitution the op exists to prevent, one layer out. The rows say `skipped -- gh is not installed here` and the status agrees with them.
