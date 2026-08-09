# Meta

Ops for self-documentation and version introspection. Used primarily in session-start hooks and agent prompts to onboard an LLM to a project's supertool setup in one call.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `introduction` | `introduction` | Output the project introduction text from `.supertool.json`. No `---` dispatch header — clean markdown. |
| `output-format` | `output-format` | Output format examples from `.supertool.json`. Shows what responses look like. |
| `ops` | `ops` | Full operations reference from `.supertool.json` — built-in ops, custom ops, and aliases with descriptions and examples. |
| `version` | `version` | Show supertool version. |
| `help` | `help:OP` | Print the full reference for a single op — syntax, full (uncompacted) description, and example — read from `.supertool.json`. Discovers an op's payload shape (e.g. `vim`'s macro grammar) without grepping source. Errors with a pointer to `ops` for an unknown or undocumented op. |
| `gc` | `gc[:dry\|:run][:KIND]` | Prune supertool's own caches. Bare `gc` and `gc:dry` preview; `gc:run` deletes. See below. |
| `cwd` | `cwd:PATH` | Set the working dir for the whole call. **Must be the first op** — chdir's once before dispatch, then is stripped, so every following op resolves against `PATH`. Replaces a `cd PATH && ./supertool …` prefix (which trips the use-supertool hook and risks stale-cwd path poisoning) for cross-repo sessions. `~`/`$VAR` expanded; non-directory or non-first → error before any op runs. |

## cwd auto-resolve

Without an explicit `cwd:`, a call whose path args only make sense from the project root recovers instead of failing. The pre-pass chdir's to the root and prints `[cwd auto-resolved to project root: …]` — so after a `cd` into a subdirectory (a browser test run, a package build), `./supertool 'wc:src/app/Module.py'` still answers rather than returning `path not found … wrong CWD?` and costing a second round-trip.

The trigger is deliberately narrow, and all three conditions must hold:

- an ancestor directory carries a `.supertool.json` (explicit project marker),
- **no** path-shaped arg resolves against the current cwd,
- at least one path-shaped arg resolves against that root.

So a subdirectory-relative call is never hijacked. An explicit `cwd:` op disables the probe entirely; `@` payloads, flags, absolute paths, `~` paths and wildcards are ignored when probing.

## Inherited `GIT_*` environment

Git's `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES` and `GIT_NAMESPACE` override discovery-from-cwd, and git **exports them to every hook it runs**. A supertool call made from inside a git hook — this repo's own `.githooks/pre-commit` invokes `./supertool` — therefore inherits a pointer to whatever repository invoked the hook.

Supertool removes all seven from its own environment **once per call, in the launcher, before any op dispatches**. Because the removal is applied to the process environment rather than to a copied dict, it reaches every child: presets (launched with a `dict(os.environ)` copy taken afterwards) and the git commands core spawns for itself.

```
scrubbed inherited git env: GIT_DIR — this call acted on the repo at /path/to/cwd, not the one those variables named (#692, #714)
```

One line per call, not per op: the scrub is one event.

### Where the boundary sits, and why there

[#692](https://github.com/Digital-Process-Tools/claude-supertool/issues/692) put the scrub in the **preset** launcher and argued — correctly — for a single chokepoint, on the grounds that a preset should be protected by being launched rather than by remembering to opt in. [#714](https://github.com/Digital-Process-Tools/claude-supertool/issues/714) found the level was one too low. Built-in ops never pass through that function, and core spawns git in six of its own places:

| Function | Feeds |
|----------|-------|
| `_run_git_ignore_query` | the gitignore pruning behind `glob`, `grep`, `tree`, `map` |
| `_path_meta_suffix` | the ` m` / ` ?` / ` !` marker on every `read` and on `workspace`'s meta line |
| `_branch_probe` | the branch named in receipts |
| `op_workspace` §Git | branch, ahead/behind, file status, log, blame |
| `op_validate_staged` | `git diff --cached` |
| `op_format_staged` | `git diff --cached` |

Twelve `subprocess.run` calls, none of which passed an `env=`. Guarding each one would be the partial-adoption failure [#704](https://github.com/Digital-Process-Tools/claude-supertool/issues/704) describes — twelve sites to keep in step, and spawn thirteen written without the guard. So the boundary **moved up rather than multiplying**: one call site, now in `_main`. A test pins that there is exactly one and that it is the launcher.

### Why it needed its own issue

Under [#692](https://github.com/Digital-Process-Tools/claude-supertool/issues/692)'s reported case the op acted on the wrong repo and said nothing. On the built-in side git **exits 0** — it answers correctly, about a different repository. So [#705](https://github.com/Digital-Process-Tools/claude-supertool/issues/705)'s `git?` decline has no failure to catch, and the marker is not absent but *wrong*. Observed under `GIT_DIR` pointing at another repo, all three exiting 0:

- a tracked, locally modified file read ` ?` (untracked) instead of ` m`;
- `workspace` reported the other repository's branch;
- `validate_staged` — the op a pre-commit hook exists to run — printed `no staged files` with a file staged.

Only **index**-derived answers flip. A bare `GIT_DIR` leaves the work tree at the cwd, and `.gitignore` is read from the work tree, so the ` !` ignored marker is identical either way. That also means two synthetic repos carrying the same paths hide the bug completely: the foreign index answers the same thing. A reproduction needs the repos to be **asymmetric**.

### Opting out

There is no opt-out. If you meant to operate on the repo those variables name, `cd` there — or use `cwd:PATH` as the call's first op — rather than relying on the environment. The scrub is unconditional because a redirect that survives into `git push` is not something a receipt can undo after the fact.

`GIT_CEILING_DIRECTORIES` and `GIT_DISCOVERY_ACROSS_FILESYSTEM` are deliberately left alone: they only *restrict* discovery, so they cannot land an op on the wrong repo — the worst they do is make it find none — and they are set on purpose by people working over slow mounts.

## `gc` — cache retention

Supertool writes four caches under `~/.cache/supertool` (`XDG_CACHE_HOME` honoured): `vim-cursor` and `vim-undo` (per-file cursor state and the cross-call undo snapshot), `validators` (validator results keyed by content hash), and the legacy `vi-cursor`. Nothing used to reap them — on a daily-driver machine the tree reached **1.0 GB across 242,000 files in about two weeks**, and both `vim-*` directories exceeded the 65535-dirent listing cap, which is enough to make an ordinary `ls` visibly slow.

`gc` prunes them.

```bash
./supertool 'gc'            # preview everything (same as gc:dry)
./supertool 'gc:dry'        # explicit preview
./supertool 'gc:run'        # delete
./supertool 'gc:run:vim-undo'   # delete one kind only
```

**Preview is the default.** Bare `gc` never deletes — it reports, per kind, how many entries are outside the retention window and how many bytes they hold, plus a total:

```
gc — dry run, nothing deleted
  vim-cursor   125431 stale / 496.2 MB   (kept 522, skipped 0, retention 7d)
  vim-undo     115980 stale / 468.9 MB   (kept 221, skipped 0, retention 7d)
  vi-cursor      4368 stale / 16.8 MB    (kept 0, skipped 0, retention 7d)
  validators        0 stale / 0 B        (kept 18555, skipped 0, retention 30d)
  total        245779 stale / 981.9 MB
  run `gc:run` to delete
```

**Retention differs per kind, because the caches differ.** `vim-cursor` and `vim-undo` default to **7 days** — that is where the gigabyte lives and 99% of their entries were older than that. `validators` defaults to **30 days**: it was measured with *zero* entries older than 7 days, is invalidated by content hash rather than by age, and is correctly sized — a 7-day default would evict a hot cache to reclaim nothing. Override any of them in `.supertool.json`; see [configuration.md](../configuration.md#gc--cache-retention).

**The boundary is exact and exclusive.** An entry is removed when `now - mtime` is **strictly greater** than the window; an entry exactly at the boundary is kept.

**An entry whose age cannot be determined is never removed.** Anything that is not a plain regular file, whose `stat` fails, or whose mtime is in the future (clock skew, a restored backup) is counted as `skipped` and left alone — not knowing how old something is is not evidence that it is stale. The count is printed so the entries are visible rather than silently accumulating.

**Blast radius is an allowlist.** `gc` only ever opens the four kind directories by name, non-recursively. Subdirectories, the `.cache_key` HMAC secret, and any directory supertool does not own are never touched.

**It also runs on its own**, at most once per `interval_seconds` (default 3600), gated on the mtime of `~/.cache/supertool/.gc-stamp` — so ordinary use never accumulates a gigabyte in the first place, with no daemon and no cron. The stamp is written *before* the sweep, so a sweep that fails does not re-arm on every subsequent call, and any failure is swallowed: a cache prune that raises during your edit is a worse bug than the disk usage it was cleaning up. Set `SUPERTOOL_GC_DISABLE=1`, or `"gc": {"enabled": false}`, to turn the automatic sweep off; the explicit op keeps working either way.

## Common patterns

Full LLM onboarding in one call — everything an agent needs to use supertool:

```bash
./supertool 'introduction' 'output-format' 'ops'
```

Use this in session-start hooks or agent system prompts. The model learns what ops exist, how output is formatted, and what the project uses supertool for — without reading any config files.

Check installed version:

```bash
./supertool 'version'
```

Discover one op's full payload shape — the front door for ops with non-obvious input (e.g. `vim`):

```bash
./supertool 'help:vim'
```

`help:OP` prints that op's uncompacted description from `.supertool.json`. Use it when an op's signature isn't enough; use `ops` when you don't yet know which op you want.

What the session-start hook actually runs, and it fits the ~7KB hook-output cap:

```bash
./supertool 'introduction' 'output-format' 'ops:roster'
```

Measured in this checkout (`python3 supertool.py 'ops' | wc -c`): `ops` is 47,254 bytes and `ops-compact` 9,067, against a cap of ~7,168 — so **no listing form fitted, and the startup listing was truncated on every session**, hiding everything alphabetically after `grep`: the whole `gh-*` and `git-*` families, `radar`, `watch`, `read`, `paste`, `tree`. It disclosed the truncation honestly and that did not help, because what was hidden was *existence*, and a reader cannot miss what they never learned about. Three agents in one session reported `write:` is not an op without being told `paste:` is.

`ops:roster` is ~1.7KB — every op name, each carrying a safety class, and nothing else, plus the same "N shipped presets are not loaded here" line `ops` carries. The whole hook payload is ~2.7KB against the ~7.2KB cap. Not quoted to the byte: the disclosure names the absolute path of the config it read, so the size moves with the checkout.

```
  append* around around_line batch* between channel check cwd dashboard diag
  ... gh-pr gh-pr-create! gh-pr-merge! gh-prs gh-run ... git-push! ...
```

| Marker | Class | What it licenses |
|--------|-------|------------------|
| *(none)* | `read-only` | Call it blind — its own error teaches the signature |
| `*` | `writes` | Changes files in this tree |
| `!` | `acts` | Changes something outside this tree, or starts something that outlives the call — look it up, never probe |

Flat and alphabetical rather than grouped by family, because every miss that motivated it was a neighbour miss: `gh-pr-create` sits beside `gh-pr`, `git-worktrees` beside `git-status`. An op whose class is not declared renders `!`, so a gap in the data is never the quiet answer.

Descriptions are one call away and richer there — `help:OP` carries the full contract, the semantics and a worked example, where a listing row carried one line. `ops-compact` still exists and still warns when it exceeds the cap; it is no longer what the hook prints.

**`ops` refuses an argument rather than dropping it.** `ops:gh-labels` used to print all 47KB and say nothing about the token it discarded — in the op whose subject is which tokens exist. It now names the alternative:

```
$ ./supertool 'ops:gh-labels'
ERROR: `ops` takes no filter, and 'gh-labels' is an op name.
  Its full entry: `help:gh-labels` — more than the listing row carries.
  Every name plus its safety class: `ops:roster`. Every entry: `ops`.
```

There is deliberately no `ops:PATTERN` filter. `help:OP` already answers what a filter would and answers it with more, and a filter would re-create this issue in miniature: `ops:gh-labl` matching nothing renders identically to an op that does not exist.

## See also

- [index.md](index.md) — full op table for all categories
- [docs/validators.md](../validators.md) — validator reference
