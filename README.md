<p align="center">
  <img src="supertool-banner.webp" alt="SuperTool — cut your Claude Code bill by 50%" width="900">
</p>

# supertool

> **Cut your Claude Code bill by 50%.**
> `git-status`, but it tells you what to do next.

[![Tests](https://github.com/Digital-Process-Tools/claude-supertool/actions/workflows/tests.yml/badge.svg)](https://github.com/Digital-Process-Tools/claude-supertool/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Community-brightgreen)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.11.0-orange)](.claude-plugin/plugin.json)

Saves tokens. Saves money. Saves turns. Works the same in interactive sessions and autonomous runs — humans pair-programming with Claude Code use it every day, not just Kevin-style headless agents. One Python file, zero deps, Python 3.9+.

[Why](#why) • [Four pillars](#four-pillars) • [Receipt](#receipt--the-bill-math) • [Batching](#batch-multiple-ops-in-one-call) • [Parallel](docs/configuration.md#parallel-execution) • [Input forms](#input-forms) • [Validators](#validators--squiggle-on-save-for-the-llm) • [Expand it](#supertooljson--project-configuration) • [Install](#install)

```bash
# 7 ops, 1 round-trip, parallel where safe
supertool 'read:src/Module.py' 'read:src/Auth.py' 'grep:TODO:src/:20' 'map:src/'
```

---

## Why

**Hammer in 2026.** Claude Code's default toolbelt is 1995 unix: `cat` one file, `grep` one pattern, `git status` returns 200 bytes of porcelain. Every tool call re-sends the entire conversation cache — system prompt, CLAUDE.md, rules, every prior turn — at 10% of input price. Read 7 files? Pay that prefix 7 times. Run `git status` then realize you needed `ahead/behind` too? Pay it twice for one decision. The bill compounds turn over turn.

**Drill in 2026.** supertool gives the agent variants that pack the *next question* into the *current call*:

- **`git-status`** — branch + tracking + ahead/behind + dirty files + open MR/PR + suggested next step. One call, decision ready.
- **`gl-mr:NUMBER`** / **`gh-pr:NUMBER`** — full MR/PR dashboard: branch, pipeline, reviewer, approval, diff stat, comments. Replaces 4-5 `glab`/`gh` calls.
- **`claude-log-summary:UUID`** — model, duration, tool calls, tokens, cache hit %, errors-by-tool. Audit your own runs.

That's a sample. supertool ships ~40 ops out of the box (built-ins + `gitlab` / `github` / `git` / `claude-log` presets) — add your own and you're past 60 fast.

The variant *is* the lever. A turn saved isn't free time — it's a cached prefix you didn't re-pay.

## Four pillars

| Pillar           | What it does                                                                     |
| ---------------- | -------------------------------------------------------------------------------- |
| **Right tool**   | Variants pack state + guards + next-step into one call. Less to remember.        |
| **Batched**      | 7 ops, 1 round-trip. The cached prefix gets re-paid once, not seven times.       |
| **Parallel**     | Read-only ops in a batch run concurrently — ~3-5× faster on cold I/O.            |
| **Expandable**   | Add a custom op in 4 lines of JSON. Presets ship gitlab, github, git, claude-log. |

## Receipt — the bulldozer math

| Mode                     | Cache reads | Output | Turns |    Savings |
| ------------------------ | ----------: | -----: | ----: | ---------: |
| Hammer (no batching)     |        436K |  1,400 |    10 |          — |
| supertool                |        133K |    750 |     3 |    **50%** |
| Pre-computed + supertool |       85.5K |    600 |     2 |    **56%** |

**50% fewer tokens, 3-4× faster wall time.** Fewer turns = fewer prefix re-reads. Multiply by task count and team size — the bill cut is real.

## What this means in practice

Three things happen once you ship variants instead of raw shell:

**1. You build your own ops.** [Digital Process Tools](https://digital-process-tools.com) built a stack on top — none ship with supertool, all written in 5-15 lines of JSON: `git-commit` (stage + commit + receipt), `mr` (push + MR + reviewer), `mysql_read`/`mysql_write`, `verify_staged` (phpstan + phpmd + phplint on the staged diff). Every project has its own "what's the next question I always ask" — bake the answer in, save the round-trip forever.

**2. The op holds the guards.** `mysql_write` refuses `UPDATE`/`DELETE` without `WHERE`. `mysql_read` auto-`LIMIT 50`s. `mr` can enforce branch policy and reviewer. Every guard is a class of mistake the agent *can't* make. Tokens saved, yes — but the session that didn't get derailed cleaning up "oops, emptied the user table" is the expensive one.

**3. The agent thinks less.** A variant that returns everything in one shot is a variant the agent doesn't have to *think through*. Thinking tokens bill at output rate. Every "let me also check..." that becomes "the op already told me" is output cost saved on top of round-trip cost.

---

## Install

From the DPT marketplace:

```
/plugin marketplace add Digital-Process-Tools/claude-marketplace
/plugin install supertool@dpt-plugins
```

This auto-registers the session-start hook via the plugin's `hooks/hooks.json` — no manual `settings.json` editing.

Or directly — clone the repo and symlink `supertool.py` onto your `$PATH` as `supertool`:

```bash
git clone https://github.com/Digital-Process-Tools/claude-supertool.git
ln -s "$(pwd)/claude-supertool/supertool.py" /usr/local/bin/supertool
chmod +x /usr/local/bin/supertool
```

Verify:

```bash
supertool 'read:README.md'
```

Standalone install doesn't wire up the session-start hook (no plugin system). You get the binary; the marketplace install adds the session-start prompt that primes the model on your project's ops.

---

## How to use

Just install. The session-start hook runs `./supertool 'introduction' 'output-format' 'ops-compact'` to output the project-specific operations reference from `.supertool.json`. The model learns what's available and how to batch. Falls back to native `Grep`/`Read` when those are better.

> **Heads-up — hook output cap.** Claude Code truncates hook stdout around 7KB; over that, only a ~2KB preview reaches the model and the rest is silently saved to disk. With many ops, the tail of the listing gets hidden until rediscovered mid-task.
>
> The session-start hook uses `ops-compact` to stay under the cap: examples are dropped on self-explanatory ops, and only kept on ops marked `"hint": true` in `.supertool.json`. If the body still exceeds the cap, `ops-compact` prepends a warning telling the model to fetch the full listing via `./supertool 'ops'`. Plain `'ops'` always returns everything.

### Hard-block native tools (optional)

If you want to force the model to batch via supertool — typical for autonomous / Kevin-style runs — block the competing tools at the Claude Code layer. Two paths:

**Settings (interactive sessions):** add a `permissions.deny` block to `.claude/settings.json`:

```json
{
  "permissions": {
    "deny": ["Grep", "Glob", "LS", "Bash(find:*)", "Bash(cat:*)", "Bash(grep:*)", "Bash(ls:*)", "Bash(sed:*)", "Bash(awk:*)", "Bash(tail:*)", "Bash(head:*)"]
  }
}
```

**CLI flag (`claude -p` bypass mode):**

```bash
claude -p "..." --permission-mode bypassPermissions \
  --disallowedTools "Grep,Glob,LS,Bash(find:*),Bash(cat:*),Bash(grep:*),Bash(ls:*),Bash(sed:*),Bash(awk:*),Bash(tail:*),Bash(head:*)"
```

`--allowedTools` is [ignored in bypass mode](https://github.com/anthropics/claude-code/issues/12232) — always use `--disallowedTools` when bypassing.

---

## Operations

~40 ops across reads, search, edits, symbol mapping, and meta. The full reference lives in [docs/operations/index.md](docs/operations/index.md) with per-category pages and a dedicated [`map`](docs/operations/map.md) deep-dive.

Quick examples:

```bash
./supertool 'read:src/Foo.py' 'grep:TODO:src/' 'map:src/'
```

---

## Input forms

Three ways to pass arguments. Full reference: [docs/input-forms.md](docs/input-forms.md).

- **Colon-CLI** (default) — `read:PATH:OFFSET:LIMIT`. Use `:::` when content contains colons: `edit:::OLD:::NEW:::PATH`.
- **`@file` route** — JSON payload for `edit`/`replace_lines`/`paste`/`vim` when content is multi-line or shell-hostile: `edit:@.max/my-edit.json`.
- **`batch:@file`** — mixed reads + writes in one round-trip: `batch:@.max/ops.json` (bare array or `{continue_on_error, ops}` wrapper).

---

## Validators — squiggle-on-save for the LLM

Every mutating op (`edit`, `replace`, `replace_lines`, `paste`, `vim`) runs matching validators on the result. Syntax fail → atomic rollback. The model gets an immediate error receipt and retries cleanly.

Example: edit a `.json` file with a missing comma → `jsonlint` catches it → file reverts → receipt shows the parse error with line/col.

17 validators bundled out of the box (PHP, XML, JSON, YAML, INI, Python, Bash, JS, TS, SCSS, Markdown, Ruby, Dockerfile, Go, Terraform, Rust, TOML). Graceful skip when toolchain missing.

Full reference: [docs/validators.md](docs/validators.md) — bundled list, how they hook in, adding your own.

---

## Formatters — normalize before validate

Formatters run after every edit, before validators — `edit → format → validate → rollback if validate fails`. They mutate the file in place (`prettier --write`, `gofmt -w`) so validators always see canonical output.

`prettier` ships as the first bundled formatter. `rollback_on_fail` defaults to `false` — formatters are cosmetic; the validator is the safety net.

Full reference: [docs/formatters.md](docs/formatters.md) — config shape, bundled formatters, adding your own (gofmt, black, rustfmt, phpcbf).

---

## `.supertool.json` — project configuration

Supertool works with no configuration. The `.supertool.json` is optional — it enables self-documenting ops for LLM onboarding via `./supertool 'introduction' 'ops'`. Create one in your project root; supertool walks up from cwd to find it. A starter template ships as `.supertool.example.json`.

Full reference (sections, `builtin-ops` overrides, custom ops, aliases, dispatch order, placeholders, env vars): [docs/configuration.md](docs/configuration.md).

---

## Presets — reusable op packs

8 presets ship out of the box (`git`, `github`, `gitlab`, `claude-log`, `hashnode`, `devto`, `bluesky`, `xml`). Each has a dedicated reference page in [`docs/presets/`](docs/presets/index.md) covering ops, common workflows, env vars, and authoring notes.

Writing your own: see [docs/contributing.md](docs/contributing.md).

### Legacy `check:` syntax

The `check:PRESET:PATH` op still works — it reads from the `ops` section first, then falls back to `.supertool-checks.json` for backward compatibility. New projects should use direct ops (`mypy:file`) instead of `check:mypy:file`.

---

## RTK integration

When [rtk](https://github.com/reachingforthejack/rtk) is installed, supertool automatically delegates `read`, `grep`, and `wc` to RTK for compressed output. No configuration needed — detected via `which rtk` at first use.

- With RTK + compact: uses `rtk read --level aggressive` (maximum compression)
- With RTK, no compact: uses `rtk read` (RTK formatting, no stripping)
- Without RTK + compact: native regex-based blank/comment stripping
- Without RTK, no compact: supertool's own output (default)

RTK is optional. Supertool works identically without it — RTK is just an accelerator.

---

## Batch multiple ops in one call

**Six or seven ops per call is routine; two is too few.**

```bash
supertool \
    'read:src/Module.py' \
    'read:src/Permissions.py' \
    'read:src/Options.py' \
    'grep:extends:src/:20' \
    'grep:@related:src/:10' \
    'glob:src/Components/**/*.xml' \
    'glob:src/EventsManagers/*.py'
```

One round-trip. Seven ops worth of output. The session-start hook reminds the model of this each session.

---

## Anti-patterns the tool catches

The tool **auto-promotes** these wasted patterns silently, but you should still recognize them and batch up front:

- `glob:concrete/path.xml` followed by `read:concrete/path.xml` — glob on a path with no wildcards is useless; just `read:`. SuperTool auto-reads it.
- `grep:FOO:single_file.py` followed by `read:single_file.py` — same file, two turns. SuperTool auto-reads if the file is < 20KB with a match.
- A second SuperTool call whose ops could have fit in the first.

**Self-check:** if the output contains `[auto-read: ...]`, SuperTool just salvaged a wasted turn you asked for. Tighten your next prompt to batch up front.

---

## Measuring adoption

Every SuperTool call is logged to `/tmp/supertool-calls.log` with this format:

```
2026-04-16 21:05:42 | user=alice ppid=74394 entry=cli | ops=3 out=12400b | read:a.py read:b.py grep:X:src/:20
```

Fields:

- `user=` — the shell user
- `ppid=` — parent process (stable within one Claude Code session, useful for grouping)
- `entry=` — how Claude Code was invoked (`cli`, `sdk`, etc.)
- `ops=N` — number of ops in this call
- `out=Nb` — output bytes emitted to the model

### Single-op rate (adoption signal)

```bash
awk -F'|' '{ for (i=1;i<=NF;i++) if ($i ~ /ops=/) print $i }' /tmp/supertool-calls.log \
  | sort | uniq -c | sort -rn
```

A healthy run has most calls at `ops=3+`. A run dominated by `ops=1` means the model is using SuperTool but not batching — tighten the system prompt.

### Estimated savings vs. no-batching baseline

```bash
awk -F'|' '
  { for (i=1;i<=NF;i++) if ($i ~ /ops=/) { gsub(/[^0-9]/,"",$i); t+=$i; n++ } }
  END { printf "%d ops in %d calls → %d round-trips saved vs all-single\n", t, n, t-n }
' /tmp/supertool-calls.log
```

Each saved round-trip avoids one prefix cache re-read. The bigger your prefix, the bigger the saving per trip.

---

## Contributing

See [docs/contributing.md](docs/contributing.md) — custom ops, presets, validators, running tests, submitting upstream.

---

## Platform compatibility

**Linux/macOS:** works out of the box.

**Windows:** works via Git Bash or WSL (the plugin's `hooks/session-start.sh` + `.githooks/pre-push` are bash scripts; the Python tool itself is cross-platform). Native `cmd.exe` / PowerShell without bash won't fire the hooks.

**Paths with spaces:** fine. Arguments arrive via `sys.argv` pre-tokenized by the shell, so `supertool "'read:/home/jo bob/file.py'"` works unchanged.

**Windows drive letters:** the tool recognizes `C:\...` and `D:/...` automatically and reassembles them after colon-splitting. So `supertool 'read:C:\Users\file.py'` and `supertool 'grep:needle:C:/src:20'` both parse correctly. If you hit edge cases, forward slashes (`C:/path`) work everywhere on Windows too.

**Temp/log location:** the call log uses `tempfile.gettempdir()` — macOS: `/var/folders/.../T/supertool-calls.log`, Linux: `/tmp/supertool-calls.log`, Windows: `%TEMP%\supertool-calls.log`.

---

## Design decisions

- **One file.** `supertool.py` is ~980 LoC (16 ops, 3 integration tiers). No package, no `setup.py`, no required deps. Drop in and use.
- **Python 3.9+.** macOS ships 3.9 via CommandLineTools; we don't force upgrades.
- **No MCP server.** MCP is server-process-and-JSON-RPC ceremony for what's literally "run a script, get output." A Bash-invoked binary is simpler, faster, and plugs into Claude Code's existing `--allowedTools`/`--disallowedTools` flow.
- **Trade Python work for LLM tokens.** LLM compute is expensive; local CPU is cheap. Any time the model would spend tokens computing, parsing, formatting, or finding — supertool should spend milliseconds instead. Richer op output (state hints, guards, semantic anchors, auto-formatting, syntax checks) is not feature creep — it's the whole thesis. Heavy Python is fine if it shaves tokens off the model side.

---

## License

[Community License](LICENSE) — free for personal, educational, and internal business use. © 2026 Digital Process Tools.
