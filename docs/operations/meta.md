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

Compact variant used by the session-start hook to stay under Claude Code's ~7KB hook output cap:

```bash
./supertool 'introduction' 'output-format' 'ops-compact'
```

`ops-compact` drops examples on self-explanatory ops and prepends a warning if output still exceeds the cap, telling the model to fetch the full listing via `./supertool 'ops'`.

## See also

- [index.md](index.md) — full op table for all categories
- [docs/validators.md](../validators.md) — validator reference
