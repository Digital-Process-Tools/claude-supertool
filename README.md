<p align="center">
  <img src="supertool-banner.webp" alt="SuperTool — cut your Claude Code bill by 50%" width="900">
</p>

# supertool

> **Cut your Claude Code bill by 50%.**
> `git-status`, but it tells you what to do next.

[![Tests](https://github.com/Digital-Process-Tools/claude-supertool/actions/workflows/tests.yml/badge.svg)](https://github.com/Digital-Process-Tools/claude-supertool/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![OS](https://img.shields.io/badge/tested%20on-Linux%20%7C%20macOS%20%7C%20Windows-blue)](https://github.com/Digital-Process-Tools/claude-supertool/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/license-Community-brightgreen)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.14.1-orange)](.claude-plugin/plugin.json)

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
- **`gl-mr:NUMBER`** / **`gh-pr:NUMBER`** — full MR/PR dashboard: branch, pipeline, reviewer, approval, diff stat, per-file name-status (A/D/R/M) list, comments. Replaces 4-5 `glab`/`gh` calls.
- **`gl-mrs`** — MR triage board: your open MRs + per-MR pipeline status + which already have a `watch` poller running + an actionable footer. Pairs with `watch` to auto-watch every failing MR.
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

### Plain / ASCII output mode (hooks & CI)

Op output uses `⚠` / `✓` glyphs — nice UX for the model, a liability for anything that parses the output without UTF-8/locale guarantees (git hooks, `grep`, CI on a non-UTF-8 console). Pass `--plain` (or set `SUPERTOOL_PLAIN=1`) to emit ASCII-only output: `[WARN]` / `[OK]` / `[FAIL]` / `[INFO]` in place of the glyphs, with the stable section keys (`Red flags in added lines`, `Forbidden paths`, …) intact for grepping.

```bash
./supertool --plain 'git-diff:staged'        # flag
SUPERTOOL_PLAIN=1 ./supertool 'git-diff:staged'   # env (propagates to preset subprocesses)
```

The flag exports `SUPERTOOL_PLAIN=1` so preset ops (run as subprocesses) inherit it. Stdout/stderr are also reconfigured to UTF-8 at startup as cheap insurance, so a stray glyph in diffed content never crashes the process on a cp1252 console. Default (rich) output is unchanged.

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

Supertool prunes its own caches: a `gc` sweep runs by itself at most once an hour, and `./supertool 'gc'` previews it without deleting — [docs/operations/meta.md](docs/operations/meta.md#gc--cache-retention), retention keys in [docs/configuration.md](docs/configuration.md#gc--cache-retention).

Quick examples:

```bash
./supertool 'read:src/Foo.py' 'grep:TODO:src/' 'map:src/'
```

---

## Input forms

Three ways to pass arguments. Full reference: [docs/input-forms.md](docs/input-forms.md).

- **Colon-CLI** (default) — `read:PATH:OFFSET:LIMIT` (or `read:PATH:START-END` for an explicit, inclusive line range). Use `:::` when content contains colons: `edit:::OLD:::NEW:::PATH`.
- **`@file` route** — JSON payload for `edit`/`replace_lines`/`paste`/`append`/`vim` when content is multi-line or shell-hostile: `edit:@.max/my-edit.json`.
- **`batch:@file`** — mixed reads + writes in one round-trip: `batch:@.max/ops.json` (bare array or `{continue_on_error, ops}` wrapper).

---

## Validators — squiggle-on-save for the LLM

Every mutating op (`edit`, `replace`, `replace_lines`, `paste`, `append`, `vim`) runs matching validators on the result. Syntax fail → atomic rollback. The model gets an immediate error receipt and retries cleanly.

**What that guarantee actually covers.** Python is unconditional: a built-in in-process parse check (`py-syntax`) runs on every mutating op against a `.py` file and reverts an edit that made it unparseable, with no configuration and no toolchain. **Every other language is opt-in.** A `.php`, `.ts` or `.json` file rolls back only if `.supertool.json` declares a validator that matches it *and* sets `rollback_on_fail: true` — with no such entry the file is not syntax-checked at all, and a receipt with no red in it means only that nothing ran. Before quoting the guarantee in an agent brief, check which half you are in.

Example: edit a `.json` file with a missing comma → `jsonlint` catches it → file reverts → receipt shows the parse error with line/col.

18 validators bundled out of the box (PHP, XML, JSON, YAML, INI, Python syntax + types, Bash, JS, TS, SCSS, Markdown, Ruby, Dockerfile, Go, Terraform, Rust, TOML). Graceful skip when toolchain missing.

Results are cached per file-content hash **plus a fingerprint of the tools themselves** (adapter scripts, binaries, and any `validator_fingerprint_paths` such as your lockfile), so upgrading an analyser invalidates the answers it produced instead of replaying them. A TTL (`validator_cache_ttl_hours`, default 24h) backstops whatever the key still can't see. Non-deterministic engine failures are never cached.

Full reference: [docs/validators.md](docs/validators.md) — bundled list, how they hook in, caching, adding your own.

---

## Formatters — normalize before validate

Formatters run after every edit, before validators — `edit → format → validate → rollback if validate fails`. They mutate the file in place (`prettier --write`, `gofmt -w`) so validators always see canonical output.

`prettier` ships as the first bundled formatter. `rollback_on_fail` defaults to `false` — formatters are cosmetic; the validator is the safety net.

A formatter rewrites the **whole file**, so it runs only where the repo shows it wants one: the tool's own config (`.prettierrc`, `phpcs.xml`, `pyproject.toml` with `[tool.black]`, …), searched from the *edited file's* directory up to **its** repo root; a manifest naming the tool; or an `env` entry in the spec carrying the rules. Otherwise the file is validated and left alone, and the receipt says which formatter was skipped. Set `"requires_config": false` on a spec to always run it, or `SUPERTOOL_FORMAT_WITHOUT_CONFIG=1` for a whole invocation. Tools supertool has no marker for are never gated.

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

## MCP integration — warm LSP for `resolve` / `refs` / `workspace`

Heuristic grep is fast but lies. A real language server (intelephense, pyright, typescript-language-server, gopls, rust-analyzer) knows where every symbol is defined — but spawning one per CLI call pays a 5-60s cold-index every time.

Supertool ships a long-lived **MCP daemon** as the `mcp` preset. The daemon owns one MCP server subprocess (typically [cclsp](https://github.com/ktnyt/cclsp) wrapping an LSP), stays warm across `supertool` invocations, and answers `resolve` / `refs` / `workspace` via Unix socket in milliseconds.

```bash
# 1. install LSP + MCP↔LSP bridge
npm install -g intelephense cclsp

# 2. point cclsp at the LSP
cat > .claude/cclsp.json <<'EOF'
{ "servers": [{ "extensions": ["php"], "command": ["intelephense", "--stdio"] }] }
EOF

# 3. wire supertool: add "mcp" preset + mcp block in .supertool.json
#    (see docs/mcp-integration.md for the JSON shape)

# 4. use it — first call auto-spawns the daemon, subsequent calls hit warm LSP
./supertool 'resolve:My\Namespace\TargetClass:src/Caller.php'
# → My\Namespace\TargetClass → /abs/path/src/My/Namespace/TargetClass.php  (<1s warm)
```

Ops shipped by the preset: `mcp_daemon`, `mcp_status`, `mcp_stop`, `mcp_stop_all`.

LSP-backed ops once wired: `resolve`, `refs`, `diag`, `hover`, `rename`, and per-section LSP routing inside `workspace`.

Full reference (architecture, all five LSP ops, recipe for adding a new MCP server / language, tool name reference, troubleshooting): [docs/mcp-integration.md](docs/mcp-integration.md).

---

## Notifiers — observe ops in flight

Notifiers are validators' read-friendly sibling. Same `hooks_into` / `match` shape, but **spawn-and-forget** — fire on reads as well as writes, never block the parent op, no rollback semantics. They exist so external tools can tap supertool's op stream: editor sync, Slack pings, audit logs, anything.

```json
"notifiers": {
  "my-observer": {
    "cmd": "python3 observe.py {op} {file} {line} {line_end} {before_file}",
    "match": "*",
    "hooks_into": ["edit", "replace", "paste", "vim", "around_line", "between", "read"]
  }
}
```

Placeholders: `{op}`, `{file}`, `{line}`, `{line_end}`, `{before_file}` (pre-edit content path for mutating ops), `{supertool_dir}`. Unset values render as empty strings.

Full reference: [docs/notifiers.md](docs/notifiers.md).

---

## Cursor Witness — watch the agent work in your editor

The flagship notifier consumer. A VSCode/Cursor extension listens on a Unix socket; supertool writes one JSON event per op. When the agent **edits** a file, Cursor opens it in a side-by-side **diff view** (before vs after). When the agent **reads** a range, the lines highlight in blue, the editor scrolls to center, and the highlight fades after 4 seconds. Status bar shows the recent op with op-type icons.

Closest thing to pair-programming with an autonomous agent: the agent's work becomes visible in your editor as it happens, no extra commands.

### Install

One script:

```bash
bash notifiers/cursor-witness/install.sh           # Cursor (default)
bash notifiers/cursor-witness/install.sh --vscode  # also VSCode
```

The script checks Node ≥18, compiles the TypeScript extension, symlinks it into the editor's extensions directory, and prints the JSON snippet to drop into your project's `.supertool.json` notifier block. Reload your editor (`Cmd+Shift+P` → `Developer: Reload Window`) — status bar should show `$(eye) Max: idle`.

### Self-hosted

supertool's own `.supertool.json` already wires cursor-witness — every edit to supertool source surfaces in Cursor for whoever's running the agent locally. The simplest dogfood signal.

### Debugging

When the agent fires an op but Cursor doesn't react, enable the notifier debug logger:

```bash
SUPERTOOL_NOTIFIER_DEBUG=1 ./supertool 'edit:::OLD:::NEW:::FILE'
tail -f /tmp/supertool-notifier-debug.log
```

Or set `"notifier_debug": true` in your `.supertool.json` for persistent traces. Override the log path with `SUPERTOOL_NOTIFIER_DEBUG_LOG=/path/to/log`.

### Reference

- Setup, settings, troubleshooting: [docs/cursor-witness.md](docs/cursor-witness.md)
- Notifier protocol (generic): [docs/notifiers.md](docs/notifiers.md)

---

## Watch — react to external events while you're away

The `watch` preset spawns background pollers that emit events when external state changes — a PR's checks flip red, a GitLab pipeline finishes, an MR gets a new comment. Pair it with the [claude-channel](notifiers/claude-channel/README.md) MCP server and Claude Code wakes up mid-session to handle the event without you typing anything.

### Two layers

1. **`watch` preset** — `watch:SOURCE:ID` spawns a detached poller. Events emit to a UDS socket, a status file, and macOS Notification Center. Bundled sources: `github-pr`, `gitlab-mr`, `gl-pipeline` and `gh-run` for a CI pipeline/workflow-run id with no MR or PR attached, `gitlab-mr-feed` for discovery, and `gl-runners` for CI runner health. Write your own source in ~50 lines. [What is worth watching, and what is not](docs/presets/watch.md#what-can-be-watched-and-what-cannot).
2. **`claude-channel` MCP server** — TypeScript / Bun. Binds the UDS socket and pushes events into a running Claude Code session via the [Channels feature](https://code.claude.com/docs/en/channels.md) (research preview, v2.1.80+). Optional — the watch preset is useful even without it.

Events are fire-and-forget and pollers die with the machine, so at session start an event-driven view knows nothing — and "knows nothing" looks exactly like "all green". That is what `./supertool 'radar'` is for: one idempotent op that reconciles registered tiers against live truth, heals their watchers under a respawn cap, and never renders an unknown as green. Run it on every session start.

**Radar has no default tier — you register what you watch.** With `ops.radar.radar_tiers` unset it refuses and names the fix, because an unconfigured radar that prints nothing is byte-identical to a healthy one:

```json
{ "ops": { "radar": { "radar_tiers": { "gl-mrs": {}, "gl-runners": {} } } } }
```

`gl-mrs` is the GitLab MR board — live MRs are authoritative, watchers are respawned for open MRs that lost theirs, and a feed poller keeps discovering MRs opened mid-session. `gl-runners` adds CI runner health. Any op joins by exposing `radar_report(options)`.

- Preset deep-dive (ops, `radar`, sources, discovery feed, event contract, lifecycle, writing a source): [docs/presets/watch.md](docs/presets/watch.md)
- MCP server (install, security, event format): [notifiers/claude-channel/README.md](notifiers/claude-channel/README.md)

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

## Security — cwd containment

Every path arg supertool sees is checked against the current working directory. A malicious `.supertool.json` or prompt-injected op like `paste:~/.ssh/authorized_keys:::pwned` or `read:/etc/passwd` is rejected with a clean error. Symlinks crossing the boundary are caught (realpath follows them). NUL bytes rejected early. See [issue #146](https://github.com/Digital-Process-Tools/claude-supertool/issues/146) for the full threat model.

**Opt-out** (any one is enough):

```bash
# 1. Env var — CI / one-off:
export SUPERTOOL_ALLOW_OUTSIDE_CWD=1
```

```json
// 2. Project-pinned in .supertool.json (most ergonomic for daily dev):
{ "allow_outside_cwd": true }
```

Env var only counts the literal `"1"`. `"0"`, `"false"`, empty — all stay strict (fail closed). Env precedence over JSON for one-off override.

Default excludes (`grep` / `glob` / `tree` / `map`) prune `.env/`, `.max/`, `.ssh/`, `.aws/`, `.gnupg/`, `.kube/`, `.docker/`, `.terraform/`, `.chef/`, `.npm/`, `secrets/`, `credentials/` so tokens don't surface into an LLM's context.

**Vim shell verbs (`:!`, `:%!`, `:r !`) are disabled by default** — they're full shell exec inside a vim macro, full RCE if a prompt-injected payload reaches them. Opt-in:

```bash
export SUPERTOOL_ALLOW_VIM_SHELL=1
```

Editor verbs (i/a/o/d/s/etc.) work unconditionally. See [issue #147](https://github.com/Digital-Process-Tools/claude-supertool/issues/147).

## Contributing

See [docs/contributing.md](docs/contributing.md) — custom ops, presets, validators, running tests, submitting upstream.

---

## Platform compatibility

**Linux/macOS:** works out of the box.

**Windows:** works via Git Bash or WSL (the plugin's `hooks/session-start.sh` + `.githooks/pre-push` are bash scripts; the Python tool itself is cross-platform). Native `cmd.exe` / PowerShell without bash won't fire the hooks. The pre-push hook needs a real `pythonX.Y` on `PATH` (or `PYTHON=` pointing at one) — it will not run the bare name `python3`, which on Windows can resolve to the App Execution Alias stub and block forever inside `git push`. See [docs/contributing.md](docs/contributing.md#running-tests).

**Paths with spaces:** fine. Arguments arrive via `sys.argv` pre-tokenized by the shell, so `supertool "'read:/home/jo bob/file.py'"` works unchanged.

**Windows drive letters:** the tool recognizes `C:\...` and `D:/...` automatically and reassembles them after colon-splitting. So `supertool 'read:C:\Users\file.py'` and `supertool 'grep:needle:C:/src:20'` both parse correctly. If you hit edge cases, forward slashes (`C:/path`) work everywhere on Windows too.

**Temp/log location:** the call log uses `tempfile.gettempdir()` — macOS: `/var/folders/.../T/supertool-calls.log`, Linux: `/tmp/supertool-calls.log`, Windows: `%TEMP%\supertool-calls.log`.

---

## Design decisions

- **One file.** `supertool.py` is ~980 LoC (16 ops, 3 integration tiers). No package, no `setup.py`, no required deps. Drop in and use.
- **Python 3.9+.** macOS ships 3.9 via CommandLineTools; we don't force upgrades.
- **Supertool isn't an MCP server.** For the ops supertool ships (run a script, return output), MCP would be ceremony — Bash-invoked binaries are simpler, faster, and plug into Claude Code's existing `--allowedTools`/`--disallowedTools` flow. But supertool *consumes* MCP servers when you want LSP-grade accuracy on `resolve`/`refs`/`workspace` — see the [MCP integration](#mcp-integration--warm-lsp-for-resolve--refs--workspace) above.
- **Trade Python work for LLM tokens.** LLM compute is expensive; local CPU is cheap. Any time the model would spend tokens computing, parsing, formatting, or finding — supertool should spend milliseconds instead. Richer op output (state hints, guards, semantic anchors, auto-formatting, syntax checks) is not feature creep — it's the whole thesis. Heavy Python is fine if it shaves tokens off the model side.

---

## License

[Community License](LICENSE) — free for personal, educational, and internal business use. © 2026 Digital Process Tools.
