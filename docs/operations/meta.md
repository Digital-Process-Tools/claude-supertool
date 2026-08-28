# Meta

Ops for self-documentation and version introspection. Used primarily in session-start hooks and agent prompts to onboard an LLM to a project's supertool setup in one call.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `introduction` | `introduction` | Output the project introduction text from `.supertool.json`. No `---` dispatch header — clean markdown. |
| `output-format` | `output-format` | Output format examples from `.supertool.json`. Shows what responses look like. |
| `ops` | `ops` | Every op's **signature** — built-in ops, custom ops and aliases, one row each, no descriptions and no examples (~4.1KB here). Its footer says what the **whole `ops:full` render** costs, not what the descriptions cost: that is the footer's number minus this listing, 71,523 bytes here. It also names the two ops that fetch them (#1774). |
| `ops:full` | `ops:full` | The same rows carrying their descriptions and examples — what `ops` was before #1774, ~75.7KB here. This is the full reference; nothing was deleted when the default changed. |
| `ops-compact` | `ops-compact` | The descriptive listing with per-op detail trimmed except where an entry declares `hint` (~15KB). Still over the 10,000-byte SessionStart hook cap; it says so in its first line rather than letting the tail be cut silently. |
| `ops:roster` | `ops:roster` | Every op name plus a safety class and nothing else (~2.0KB) — the only listing that fits the SessionStart cap, so it is what the hook prints. Unmarked = read-only, `*` = writes in this tree, `!` = acts outside it or outlives the call. |
| `version` | `version` | Show supertool version. |
| `help` | `help:OP` | Print the full reference for a single op — syntax, full (uncompacted) description, example, and **the `@-` payload route with the field names it derives** (#1400). Both invocation forms, because an entry showing one of two reads as complete: `help:paste` printed `paste:::PATH:::CONTENT` and stopped, and the agents that needed `path` / `content` guessed them. The fields are rendered from the same registry that drives the route, so they cannot describe a shape the loader would reject. Errors with a pointer to `ops` for an unknown or undocumented op. |
| `registry` | `registry[:OP]` | Which ops this project loads, and where each definition came from — preset, project config, or a project entry merged over a preset one. `registry:OP` shows one op's merged definition with the source of every key. See below. |
| `guard` | `guard:SHELL COMMAND` | What, if anything, the op registry says replaces a raw shell command — the same answer the shipped `PreToolUse` hook enforces on every Bash call. Four states: `BLOCKED` naming the op and quoting its description, `OK`, `NOT COVERED` when an entry claims the verb and declines this invocation of it (a refspec past a `git push` the op takes none of — the command runs, with the reason, [#1684](https://github.com/Digital-Process-Tools/claude-supertool/issues/1684)), and `UNDECIDED` when the command did not tokenise or the registry could not be enumerated. See below. |
| `doctor` | `doctor` or `doctor:probe` | The environment supertool runs in (interpreter, architecture/Rosetta, CPU topology, symlink health) plus, per configured validator, whether the toolchain it dispatches to actually resolves here. See below. |
| `gc` | `gc[:dry\|:run][:KIND]` | Prune supertool's own caches. Bare `gc` and `gc:dry` preview; `gc:run` deletes. See below. |
| `cwd` | `cwd:PATH` | Set the working dir for the whole call. **Must be the first op** — chdir's once before dispatch, then is stripped, so every following op resolves against `PATH`. Replaces a `cd PATH && ./supertool …` prefix (which trips the use-supertool hook and risks stale-cwd path poisoning) for cross-repo sessions. `~`/`$VAR` expanded; non-directory or non-first → error before any op runs. |
| `repo` | `repo:OWNER/NAME` | Name the repo a call is *about*, when it is not the one the cwd stands *in*. **First op, or immediately after `cwd:`**, once per call; resolved before the op loop and exported as `SUPERTOOL_REPO`. Honoured by the `gh-*` family and, since [#676](https://github.com/Digital-Process-Tools/claude-supertool/issues/676), by `gl-issue`/`gl-mr`/`gl-pipeline`/`gl-job` — an op in the same call that cannot honour it **refuses the whole call** rather than half-applying the target. Not a `cwd:` substitute: presets still resolve from the cwd's project root. Full reference in [github.md](../presets/github.md#targeting-another-repo). |

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

## `guard` — what the registry says replaces a raw command

The block on raw commands is a property of the op, not of a rules file beside it: each op declares `replaces` in its registry entry, and one `PreToolUse` hook shipped with the plugin enforces every one of them. `guard` asks that question directly, without running anything.

```
$ supertool 'guard:gh pr view 1321 --json state'
## Raw-command guard

BLOCKED

`gh pr view 1321 --json state` is replaced by supertool's `gh-pr` op.
  Use: supertool 'gh-pr:NUMBER:status'
  Review a pull request: branch, checks, reviews/approval, linked issue…
  Full contract: supertool 'help:gh-pr'
```

**An op with a `@payload` route has it named on its own line, under `Use:` and above the description** ([#1815](https://github.com/Digital-Process-Tools/claude-supertool/issues/1815)). `gh-pr` has none, which is why the example above shows none — an op whose `syntax` carries `:::` does, and the line is derived from the same registry the route uses. It stands above the description because the description is cut by position: `git-commit`'s multi-line route lived past the cut, so `git commit -F -` was refused toward an op whose visible contract did not contain the route it was being refused toward.

**It parses, it does not pattern-match.** The command is tokenised into argv the way a shell would, so the match is on the command word, its subcommands and its flags — a directory named `claude-supertool` is not an invocation, a flag after a quoted argument is still a token, and a heredoc body is content rather than argv. Flags select *which* op is named: `--json state` points at `gh-pr:N:status`, `--json files` at `gh-pr:N:diff`.

`UNDECIDED` is a real third state and is never rendered as `OK`. A command that did not tokenise, one that hides a substitution inside double quotes or hands a string to `eval` / `sh -c`, or a registry that could not be fully enumerated, all mean the guard did not answer — and the hook allows the command while saying so in the transcript, because a gate that quietly did not run is indistinguishable from a command that complied.

Writing a mapping: [contributing.md](../contributing.md). Turning the gate off: `"raw_command_guard": false`, [configuration.md](../configuration.md).

## `registry` — which ops are loaded, and where each came from

`ops` answers *what can I do*. `registry` answers *where did this definition come from*, which is a different question and had no answer in the product at all.

```
## Op registry — 49 ops (49 from presets, 0 project-only, 3 shadowed)
Built-ins are not config entries and are not listed — `ops:roster`.

### Shadowed by project config (3)
The preset definition is still in effect; the project entry merges these keys over it.
- dashboard  preset dashboard  + lane_prefix
- git-diff   preset git        + red_flags_extra
- radar      preset watch      + radar_tiers
```

Three answers sit in that last column, not two: the keys the project supplied, `(replaced wholesale)` when the project entry was not a table and replaced the shipped definition outright, and `(merged, no keys)` when it merged and changed nothing. The middle one is the only case where the preset's `cmd` and `syntax` are gone.

```

### From presets (46)
- gh-issue            preset github
...
### Project config only (0)
(none — this repository declares no op of its own since #1472)

Not enabled here: bluesky, claude-log, devto, gitlab, hashnode (5 shipped presets, 38 ops).
```

`registry:OP` narrows to one entry and attributes every key:

```
## git-diff
Preset 'git' defines it; shadowed by 1 project key, merged over it.

- cmd              preset git
- description      preset git
- red_flags_extra  project
- safety           preset git
- syntax           preset git
- timeout          preset git
```

### Why an op and not a documented helper

A project entry that names an op a preset already defines is a **partial override**: supertool merges it key-by-key, so `{"git-diff": {"red_flags_extra": [...]}}` adds one key and leaves the preset's `cmd`, `syntax` and `timeout` in force. Every hand-rolled walk over `presets/*.json` plus `.supertool.json` wrote the obvious `ops[name] = entry` instead, which replaces the shipped definition with the stub.

Nothing looks wrong when it happens. The op count is unchanged — the entry does not vanish, it becomes a definition with no `cmd` and no `syntax` — so it silently stops matching every filter applied downstream. In [#1350](https://github.com/Digital-Process-Tools/claude-supertool/issues/1350) the containment audit's own registry helper did exactly this, and `git-diff` — an op that names a path and sits in the grandfather register the audit exists to drain — dropped out of the audited population. The audit printed a pass over a set that was short by the one op it was about.

So the merge rule now lives in one function that the loader calls, the loader stamps where each op came from during the same walk, and `registry` renders what the loader produced. A caller re-deriving the population is re-deriving the bug.

### A population that could not be enumerated says so

Preset load failures — a name under `"presets"` with no file, a manifest that will not parse — reach stderr from the launcher. `registry` also carries them **in its own body**:

```
INCOMPLETE: this listing may be missing ops.
  - preset 'gone' not found
```

In a batched call stderr is somewhere else entirely, and a short list and a complete one are byte-identical apart from the rows that are absent. Same rule as the rest of the tool: three states, not two.

A config that declares `"presets"` but never passed through the loader is a third case: no preset op was ever merged in, so the listing holds only whatever the raw `"ops"` section carried — short *and* unattributable. Those ops render under `### Source not known`, with both facts in the `INCOMPLETE` block. They are never quietly reported as project-only.

A `"presets"` value that is not a list is the same shape one step earlier. Nothing is merged, so the config asked for ops that are now absent; the loader records that as a preset warning and `registry` prints it rather than going on to report a complete set.

## `doctor` — the environment and toolchain supertool runs in

Two halves ([#1857](https://github.com/Digital-Process-Tools/claude-supertool/issues/1857), [#1950](https://github.com/Digital-Process-Tools/claude-supertool/issues/1950)), filed separately so neither buries the other, one op because they answer the same question — "what, in this environment, could quietly be going wrong under supertool" — from two directions.

**Always runs, in-process, cheap:**

- Interpreter path, version and **architecture**. A binary-architecture mismatch (Rosetta 2, or the reverse) is loud rather than a report line: it is a performance fact, not a fault — supertool works correctly, only slower — but the incident that filed #1857 spent a morning looking at the network and the repository before the interpreter, because nothing said so. Measured on that machine, interleaved under identical load: 40 interpreter starts at **2.514s under Rosetta vs 0.830s native** (~3x), 30 subprocess spawns at **0.37s vs 0.11s CPU** (~3.4x) — wall time is not quoted for the spawn case because the machine was loaded and wall time there measures the run queue, not the code.
- CPU topology: logical core count, plus a performance/efficiency split where the platform exposes it (macOS `hw.perflevel{0,1}.logicalcpu`). Supertool does not size worker pools itself, but a caller that does and reads `os.cpu_count()` alone asks for one worker per core on a 5+6 chip and gets six of them fighting the other five — decisive in the incident #1857 describes.
- Whether the `supertool` binary on `PATH` is a dangling symlink — the `exit 127` mid-session failure mode `CLAUDE.md` documents — and whether it resolves to the module actually answering the call (expected inside a worktree; a mismatch elsewhere means a stale symlink target).
- The resolved `.supertool.json` and the configured watch fleet name.

Every one of these is a **three-state** answer, never two: an architecture check that could not run (no `sysctl.proc_translated` node — an Intel Mac, for instance) reports "could not tell", never "native". Guessing the clean answer when the check could not run would be the exact failure #1857 exists to end.

**Toolchain scope always runs; binary resolution is opt-in (`doctor:probe`):**

For every validator this project configures, `doctor` reports whether any tracked file even matches its `match` glob — free, since it is one pass over `git ls-files` already paid for once. A validator with no matching file in this tree is `not applicable`, not a finding nobody needs.

Bare `doctor` stops there: "in scope — could not tell without probing". It does **not** fall back to a `shutil.which()` sweep, because that reports green where the tool is broken — `#1950`'s own example: `which("npx")` is true, the stylelint adapter resolves `["npx", "--no-install", "stylelint"]`, and stylelint itself is not installed. `doctor:probe` invokes each in-scope validator's own resolution path — the same `_validator_run_one` the real `validate`/`format` ops use — against a real tracked file, and reads the verdict the adapter already emits (`validators/common/refusal.py`'s `absent`/`skipped`/`tool_fault` vocabulary), sorting it into:

- **resolves** — the tool ran and answered, `ok`/`count` included.
- **absent** — the adapter said so in its own words (its `INSTALL_HINT`, verbatim).
- **could not tell** — a crash, a timeout, an unreadable reply, or a decline that does not clearly name an absent tool. Never rendered as either of the other two.

Because probing invokes the *real* adapter against a *real* file in this tree, it also answers #1950's config half for free: `eslint.py`'s `_NO_CONFIG` decline and `stylelint`'s ignore-marker skip are ordinary `skipped()` results the invocation already produces when this tree lacks the config the tool needs — no second, per-tool config-detection layer.

```bash
./supertool 'doctor'          # environment + validator scope, no subprocess spawned per validator
./supertool 'doctor:probe'    # + one subprocess per in-scope validator
```

`doctor:probe` costs a subprocess per in-scope validator — up to the full validator count in this tree — which is why it is not the default: a doctor that takes thirty seconds is one nobody runs. It also always bypasses `_validator_run_one`'s own result cache (`~/.cache/supertool/validators/`, up to 24h TTL by default): a cache hit from before a binary was installed or removed would silently contradict "does this resolve now", which is the whole question `doctor:probe` exists to answer.

**One inherited cost worth naming rather than hiding.** A validator spec that opts into `"mcp_autospawn": true` (an existing, documented per-validator setting — see [mcp-integration.md](../mcp-integration.md)) makes `doctor:probe` cold-start that daemon, same as `validate`/`format` already do for the same setting: 30-60s to index, persisting up to a 600s idle window. No validator in this project's own `.supertool.json` sets it, so `doctor:probe` here never pays it — but a project that does opt in should read `doctor:probe` as inheriting that cost, not as a lightweight read regardless of config.

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

Measured in this checkout (`python3 supertool.py 'ops:full' > /tmp/o; wc -c /tmp/o`): `ops:full` is 74,114 bytes and `ops-compact` 16,110, against a cap of ~7,168 — so **no descriptive listing fits, and the startup listing was truncated on every session**, hiding everything alphabetically after `grep`: the whole `gh-*` and `git-*` families, `radar`, `watch`, `read`, `paste`, `tree`. It disclosed the truncation honestly and that did not help, because what was hidden was *existence*, and a reader cannot miss what they never learned about. Three agents in one session reported `write:` is not an op without being told `paste:` is.

Two things have changed under that paragraph since it was written, and both are why it is dated. Bare `ops` is signatures only since #1774 and does fit, at ~4.1KB — the numbers above now describe `ops:full`, which is where the descriptions went. And the numbers themselves read 47,254 and 9,067 here until 2026-08-16: a measurement written into prose is a measurement nothing re-runs, so quote the command beside it and expect to re-take it.

`ops:roster` is ~2.0KB — every op name, each carrying a safety class, and nothing else, plus the same "N shipped presets are not loaded here" line `ops` carries. The whole hook payload is ~2.9KB against the ~7.2KB cap. Not quoted to the byte: the disclosure names the absolute path of the config it read, so the size moves with the checkout.

**How far it moves, measured, because a stale-looking figure here is usually neither.** The same tree at `…/claude-supertool` (46 characters) and at `…/st-wt/1783` (40) renders `ops:roster` at 1,969 and 1,963 bytes — the six-character difference, exactly, and the same six bytes in `ops`, `ops:full` and `ops-compact`. So a byte count read against one of these figures can disagree by tens of bytes and mean nothing at all, while a KB approximation is stable. One exact number is stable too, and it is the one worth quoting: `ops:full` minus `ops` cancels the disclosure each carries once, giving 68,988 bytes of description at either path. `tests/test_meta_doc_figures_1783.py` pins these against the live renders so the paragraph stops being something only an audit re-reads (#1783).

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
  Every name plus its safety class: `ops:roster`. Every signature: `ops`. Every description: `ops:full`.
```

There is deliberately no `ops:PATTERN` filter. `help:OP` already answers what a filter would and answers it with more, and a filter would re-create this issue in miniature: `ops:gh-labl` matching nothing renders identically to an op that does not exist. `ops:full` and `ops:roster` are modes rather than filters — a fixed set of three words, each rendering every op, and a fourth word is refused naming the three (#1774).

## See also

- [index.md](index.md) — full op table for all categories
- [docs/validators.md](../validators.md) — validator reference
