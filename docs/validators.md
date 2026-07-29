# Validators — full reference

## What validators are

Validators are squiggle-on-save for the LLM. After every mutating op — `edit`, `replace`, `replace_lines`, `paste`, `append`, `vim` — supertool runs the matching validators against the result file. If one fails and `rollback_on_fail: true` is set, the file reverts atomically to its pre-edit state and the model gets an immediate error receipt with the parse error, line number, and column. No broken files sitting around. No "edit succeeded, discovered it three turns later."

The model retries with real information instead of hallucinating a fix.

## The built-in syntax backstop

That paragraph used to be true only of repos that had configured the right validator, and the gap was invisible: this repository itself wired `lsp-diag` — a *semantic* diagnostics pass served by a warm language-server daemon, with `rollback_on_fail: false` — as its only Python validator. Nothing in the chain ever asked whether the file parsed, so an edit that wrote an unterminated string literal into a `.py` file was reported as `lsp-diag : ok (no new errors)` and left on disk. Twice in one evening, both times on payloads with tricky escaping, which is exactly the case the check exists for ([#477](https://github.com/Digital-Process-Tools/claude-supertool/issues/477)).

So `py-syntax` is not a configured validator — it is built into supertool and it always applies:

| | |
|---|---|
| Runs on | `*.py`, for `edit`, `replace`, `replace_lines`, `paste`, `append`, `vim` |
| How | `compile()` in supertool's own process — no subprocess, no daemon, no cache, microseconds |
| Rolls back | Yes, when the file parsed *before* the op and does not parse after |
| Config needed | None. It runs in a repo with no `.supertool.json` at all |
| Deferred to | A configured validator matching the same file that declares `"syntax": true` |

It reverts **regressions only**. A file that was already unparseable can still be edited, a broken file can be repaired, and a new file created broken is reported red but not deleted — there is no pre-edit state to restore. Anything it cannot answer (unreadable file, unknown builtin) comes back as `skipped`, never as ok.

**It is Python-only, and that is a real limit, not an oversight.** The interpreter running supertool can parse Python for free; it cannot parse PHP, TypeScript or Go without a toolchain that may not be installed. For those, declare a parse check in `.supertool.json` with `rollback_on_fail: true` (see the bundled list below) — and add `"syntax": true` to it so the backstop stands down where you have your own.

## Why an LSP validator cannot be that backstop

`lsp-diag` looks like it covers the same ground for every language it matches. It does not, and the reason is structural rather than a bug in the adapter.

It queries a **warm** language-server daemon, and a warm daemon answers about the document it holds, not about the bytes on disk. Measured against cclsp/Pyright: `get_diagnostics` is served from a per-URI cache that `publishDiagnostics` fills, and `ensureFileOpen` returns early once a file is open — so nothing re-reads the file for the rest of the daemon's life. A `.py` file opened clean and then broken on disk still reported *"No diagnostics found"* after a 20-second wait and an `mtime` bump, while a cold daemon reported the unterminated literal instantly.

The sharp part is that **supertool causes this itself, by doing the right thing.** Every mutating op takes a pre-edit baseline so the receipt can report a *delta* rather than a total — and that baseline pass is exactly what opens the document in the daemon. The check is therefore strongest on files the daemon has never seen and blind on the one under edit. Skipping the baseline would not fix it either: it would only move the first-open to the after-pass, so the *second* edit of the same file goes stale instead of the first, and every pre-existing error would render as a regression.

Since supertool speaks MCP tool calls rather than LSP, it cannot send a `didChange` to fix this: cclsp's document sync is internal and reachable from no tool it exposes. The remaining lever — restarting the server per edit — trades a stale answer for a full re-index on every edit, which is the cost #488 exists to avoid.

So `lsp-diag` **declines instead of guessing**. On the post-edit pass it reports:

```
lsp-diag    : skipped     (stale document — warm LSP daemon answers from its pre-edit copy, not from disk)   0.3s
```

Read that as *"nobody checked the semantics of this edit"*, because nobody did. Everything supertool's own messages produce — no LSP configured, `MCP server 'X' unavailable` (routine since #488 stopped short-budget validators spawning daemons), transport errors, empty results — reports the same way, naming the reason. None of them is a pass.

Two consequences worth stating plainly:

- **For `.py`, the syntax guarantee is unaffected** — `py-syntax` above is in-process, reads the file itself, and cannot go stale.
- **For `.php`, `.ts`, `.js`, `.jsx`, `.tsx`, `lsp-diag` was never a post-edit syntax check** and now stops looking like one. If you want a real guarantee for those, configure a parse check with `rollback_on_fail: true` from the table below. The green row that used to be there was not covering you.

If your LSP genuinely re-reads the file on every query, opt back in through the validator's `env` block:

```json
"lsp-diag": {
  "cmd": "{python} {supertool_dir}/validators/lsp-diag/lsp-diag.py {file}",
  "match": "*.{py,php,js,ts,jsx,tsx}",
  "env": { "SUPERTOOL_LSP_RESYNC_ON_QUERY": "1" }
}
```

## How they hook in

Each validator entry declares a `hooks_into` array listing the mutating ops it should run after:

```json
"hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"]
```

Validators are declared per-file-type in `.supertool.json` under `validators`, keyed by validator name. Each entry matches files via a `match` glob:

```json
{
  "validators": {
    "jsonlint": {
      "cmd": "python3 {supertool_dir}/validators/jsonlint/jsonlint.py {file}",
      "match": "*.json",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "rollback_on_fail": true,
      "timeout": 10
    }
  }
}
```

Multiple entries for the same language are fine — `*.yml` and `*.yaml` need separate entries, for example.

You can also invoke any validator explicitly, without an edit, via the `validate` op:

```bash
./supertool 'validate:src/Foo.php'        # all matching validators
./supertool 'validate:src/Foo.php:phplint' # specific validator
```

## Graceful skip

When the underlying toolchain is missing (e.g. `stylelint` not installed, `terraform` not on PATH), the validator wrapper warns and exits 0. Supertool stays fully usable in any repo without pre-installed dependencies. No validator failure blocks an unrelated edit.

A validator that declined to run is a **third state**, distinct from both "clean" and "has errors": it produced no information about the file. An adapter reports it by adding a `skipped` key to its SCHEMA.md JSON — the `skipped` key alone is the marker, and an adapter that has no verdict must omit `ok`/`count`/`errors` entirely rather than pad them with `ok: true` (a receipt carrying both reads as a pass to anything keying off `ok`). The row renders as:

```
phpstan-mcp : skipped    (path outside --paths allowlist)     0.1s
```

Skips never enter the before/after delta, never render a `✗`, are never cached (a skip is decided by config, and the cache key is a content hash), and — most importantly — **never roll back an edit**, even for a validator with `rollback_on_fail: true`. Counting a refusal-to-run as one error would let a scope-config mismatch revert perfectly good code.

Build the dict with `validators/common/refusal.py:skipped()` rather than by hand — that helper and this paragraph disagreed for a year (#515), and the shape here is the one that won. Consumers branch on the presence of `skipped` before reading any verdict key; they cannot do otherwise, since the reason string exists only on a skip, which is why the "uniform shape saves consumers a branch" argument for padding does not hold here. `tests/test_skipped_shape_contract_515.py` pins this prose, the helper and the core's own built-in skips against each other, so the three cannot drift apart again.

`phpstan-mcp`, `phpmd-mcp` and the cold `phpstan` adapter recognise their tool's own refusal messages (`--paths` allowlist, "no files found to analyse"). An exit they cannot explain still reports as an error — swallowing an unknown failure is the same category mistake pointing the other way. Teach an adapter a house-specific refusal with `PHPSTAN_MCP_SKIP_PATTERNS` / `PHPMD_MCP_SKIP_PATTERNS` / `PHPSTAN_SKIP_PATTERNS`: extra comma-separated, case-insensitive substrings.

**`PHPSTAN_MCP_PATHS` — decide the refusal locally instead of paying for it.** Recognising the refusal still costs the full daemon round trip that produced it: ~9.2s per edited file, spent to be told phpstan would not look at it (#412). Set `PHPSTAN_MCP_PATHS` to the analysis roots — `os.pathsep`- or comma-separated, absolute or relative to the working directory — and `phpstan-mcp` answers `skipped (path outside PHPSTAN_MCP_PATHS allowlist)` without opening the socket:

```json
"phpstan": {
  "cmd": "PHPSTAN_MCP_PATHS=src:lib MCP_PHPSTAN_BIN=… {python} {supertool_dir}/validators/phpstan-mcp/phpstan-mcp.py {file}"
}
```

It is opt-in, and it must stay that way, because the two ways it can be wrong are not equally bad. **Skipping a file that IS in scope loses the analysis silently** — the file looks handled and is not. **Analysing a file that is NOT in scope** wastes a round trip and still returns the right answer. So the adapter never infers the scope, never reads it out of `phpstan.neon`, and never defaults it on: unset (or blank) means "no local knowledge, the daemon decides", exactly as before. Setting the var is the repo asserting it knows the scope, and the skip reason names the var rather than `--paths` so a wrong skip points at this line of config instead of at a tool that never saw the file. Matching is on `os.path.abspath` with an `os.sep` boundary, so `/srcbad/Foo.php` is not inside `/src`.

Two things to keep in mind when setting it. It belongs on the **validator entry**, not the shell — each `.supertool.json` entry pins its own daemon and its own scope, so `phpstan` and `phpstan-fwk` need different values. And do not set it on an entry that uses `resolve` to hand the adapter a **temporary** file (the `phpstan-component` pattern): the resolved path lives outside the repo, would fall outside every root, and every such edit would skip.

**Silence is the refusal that hides best.** PHPStan does not announce a declined file on stdout at all — it writes `[ERROR] No files found to analyse.` to stderr, exits non-zero, and leaves stdout empty. Any adapter that treats "no output" as "no findings" turns that into `ok: true, count: 0` (#263), and a green meaning *"I analysed nothing"* is byte-identical to one meaning *"I analysed it and it is fine"*. An adapter with no parseable result must therefore decide between three outcomes and never default into the fourth: a recognised refusal is `skipped`, an unexplained non-zero exit is an error naming the exit code, and only a genuinely quiet success — empty output, exit 0 — keeps the clean verdict. When quoting the tool's words back as the skip reason, quote **the line that refused**, not the first line printed: analysers open with preamble (`Note: Using configuration file ...`) that names the config and explains nothing.

## Bundled validators

Enable any of these by copying the relevant entry from `.supertool.example.json` into your project's `.supertool.json`. Each ships as a thin Python wrapper that delegates to the real tool and handles graceful skip when the tool is absent.

| Language / format   | Validator name   | Requires                           | Notes                                      |
|---------------------|------------------|------------------------------------|--------------------------------------------|
| PHP                 | `phplint`        | `php` on PATH                      | Uses `php -l`                              |
| XML                 | `xmllint`        | `libxml2` (`xmllint` CLI)          | Reports line + column on parse error       |
| JSON                | `jsonlint`       | stdlib only                        | No external dep — always available         |
| YAML (`.yml`)       | `yaml-check`     | PyYAML (`pip install pyyaml`)      | Separate entry needed for `.yaml`          |
| YAML (`.yaml`)      | `yaml-check-yaml`| PyYAML (`pip install pyyaml`)      | Identical logic, different glob            |
| INI                 | `inilint`        | stdlib only                        | No external dep                            |
| Python              | `py-compile`     | stdlib only                        | Uses `py_compile` — syntax only, not type  |
| Python (types)      | `pyright`        | `pyright` (`npm install -g pyright`)| Real type-check via `pyright --outputjson` |
| Bash                | `bash-check`     | `bash` on PATH                     | Uses `bash -n`                             |
| JavaScript          | `node-check`     | `node` on PATH                     | Uses `node --check`                        |
| CSS / SCSS          | `stylelint`      | `stylelint` npm package            | Config from project `.stylelintrc`         |
| TOML                | `tomllint`       | stdlib (3.11+) or `tomli`          | Falls back to `tomli` on 3.10              |
| Markdown            | `markdownlint`   | `markdownlint` CLI                 | `npm install -g markdownlint-cli`          |
| Ruby                | `ruby-check`     | `ruby` on PATH                     | Uses `ruby -c`                             |
| Dockerfile          | `hadolint`       | `hadolint` on PATH                 | Catches both syntax and best-practice lint |
| TypeScript (`.ts`)  | `tsc-check`      | `tsc` (`npm install -g typescript`)| `--noEmit` — no output files written       |
| TypeScript (`.tsx`) | `tsc-check-tsx`  | `tsc` (`npm install -g typescript`)| Separate entry needed for `.tsx`           |
| Go                  | `gofmt-check`    | `gofmt` (ships with Go)            | Fails on formatting diff, not just syntax  |
| Terraform           | `terraform-check`| `terraform` CLI                    | Uses `terraform validate`                  |
| Rust                | `cargo-check`    | `cargo` (ships with Rust)          | Uses `cargo check` — full type resolution  |
| PHP — static  | `phpstan`        | `phpstan` binary (PATH or via `PHPSTAN_BIN` env)  | Env-configured. See [README](../validators/phpstan/README.md) |
| PHP — mess    | `phpmd`          | `phpmd` binary (PATH or via `PHPMD_BIN` env)      | Env-configured. Auto-detects the project's `gitlab-ci/md/*.xml` CI rulesets so local findings match CI (`PHPMD_NO_AUTODETECT=1` to opt out; `ruleset_source` in the output records which path was used). See [README](../validators/phpmd/README.md) |
| PHP — style   | `psr`            | `phpcs` binary (PATH or via `PSR_BIN` env)        | Env-configured. See [README](../validators/psr/README.md)  |
| Git           | `git-status`     | `git` binary                                      | Reports working-tree delta (`+N -N <state>`) post-edit |
| Prettier — check | `prettier-check` | `prettier` binary (PATH or via `PRETTIER_BIN` env) | Env-configured read-only formatting check. See [README](../validators/prettier-check/README.md) |


## Adding your own

Any tool that exits non-zero on bad input works. Three lines of JSON and a new language is supported.

Example for Elixir:

```json
"elixir-check": {
  "cmd": "elixir -c {file}",
  "match": "*.ex",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "rollback_on_fail": true,
  "timeout": 10
}
```

The `{file}` placeholder is replaced with the absolute path to the file being validated. `{supertool_dir}` is also available for pointing at bundled wrapper scripts. See `validators/SCHEMA.md` for the full adapter contract if you want structured error output (line numbers, error categories, fix suggestions). PRs for languages you edit regularly are welcome.

## Rollback semantics

`rollback_on_fail` controls whether the file reverts when validation fails:

| Value   | When to use                                                                  |
|---------|------------------------------------------------------------------------------|
| `true`  | Syntax errors, parse failures — a broken file is worse than no edit         |
| `false` | Style/lint warnings where the edit itself is valid — warn but keep the file |

**`true` is the right default for most validators.** A PHP file with a missing `}` is not a partially-edited file — it's a broken file. Rollback gives the model a clean retry surface instead of a corrupt starting point.

`false` makes sense for opinionated linters like `hadolint` or `markdownlint` where violations are informational and the file is still structurally valid.

**Parallel execution** — validators use the project's `parallel` setting. When multiple validators match a single file (e.g. a `.ts` file matching both `tsc-check` and `stylelint`), they run concurrently. The first failure triggers rollback if `rollback_on_fail` is set; remaining validators still complete.

## Field reference

Full list of `.supertool.json` validator config fields:

| Field              | Notes                                                                                  |
|--------------------|----------------------------------------------------------------------------------------|
| `cmd`              | Shell command. `{file}` → target path. `{supertool_dir}` → supertool install dir.      |
| `match`            | Glob filter on the target path (default `*`).                                          |
| `exclude`          | Glob (or list of globs) to skip even when `match` hits — e.g. `"*tests/*"`. Per-validator on purpose: `phpunit` must still scan tests. |
| `hooks_into`       | Op names to wrap (subset of `edit`, `replace`, `replace_lines`, `paste`, `vim`).       |
| `rollback_on_fail` | Restore pre-edit file content if the validator's count went up or ok flipped to false. |
| `resolve`          | Shell cmd returning an alternate target path (e.g. source-file → test-file).           |
| `timeout`          | Seconds. Default 60.                                                                    |
| `opt_in`           | If true, validator only runs on explicit request via the `validate` op.                |
| `tier`             | `"fast"` (default) or `"slow"`. `slow` defers the validator to end-of-call, deduped by `(validator, path)` — runs once per unique pair regardless of how many ops touched the file. See [Slow tier](#slow-tier--defer-to-end-of-call). |
| `env`              | Optional `{KEY: VAL}` block merged into the subprocess environment. Values are coerced to strings. Useful for pointing wrappers at a project-local binary or config without touching the system environment. |
| `mcp_autospawn`    | `true` lets this validator **create** a warm MCP daemon. Default `false`: a validator may use a daemon that is already warm but may not start one, because a cold MCP server indexes for 30-60s (longer on big repos) while the validator's own budget is measured in seconds — the validator is killed, the orphaned daemon keeps its index resident for the full `idle_timeout`. Set `true` only when the validator's `timeout` genuinely covers a cold start. See [MCP integration → daemon lifetime](mcp-integration.md). |

### env — usage

Pass tool-specific config without shell exports:

```json
"phpstan": {
  "cmd": "python3 {supertool_dir}/validators/phpstan/phpstan.py {file}",
  "match": "*.php",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "rollback_on_fail": false,
  "timeout": 60,
  "env": {
    "PHPSTAN_BIN": "./vendor/bin/phpstan",
    "PHPSTAN_CONFIG": "phpstan.neon",
    "PHPSTAN_LEVEL": "8"
  }
}
```

The `env` block is merged on top of the inherited process environment (`os.environ | spec.env`), so unset keys fall through to whatever the shell already has.

## Slow tier — defer to end of call

Heavy validators (phpstan, phpunit, rector, tsc, cargo-check) often take 5–30s per run. In a multi-op batch — three edits on the same file — running them after each op is pure waste: same file content gets re-analyzed N times.

Set `"tier": "slow"` on the validator and supertool defers it: queued during the batch, drained once at end-of-call, deduped by `(validator, path)` so each pair runs exactly once regardless of how many ops touched it.

```json
"phpstan": {
  "cmd": "python3 {supertool_dir}/validators/phpstan/phpstan.py {file}",
  "match": "*.php",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "tier": "slow",
  "rollback_on_fail": false,
  "timeout": 60
}
```

Behavior:

- **Single-op calls always run inline**, regardless of tier — defer only kicks in for multi-op batches where dedup pays off.
- **Failures don't roll back prior ops.** Slow-tier results arrive after every op has already committed; mirrors the deferred-formatter contract. Keep `rollback_on_fail: false` on slow validators.
- **Output appears under a `[validators-deferred]` header**, separate from the per-op `[validators]` block, so it's clear which results came from the dedup pass.
- **Default is `"fast"`** — omitting the field preserves the original per-op behavior. Zero change for users who don't opt in.

Use `slow` for anything where a single analysis pass on the final file content is enough. Use `fast` (default) when you want per-op rollback semantics (syntax linters, json/yaml/xml parsers — these are cheap anyway).

## Caching

Results are auto-cached at `~/.cache/supertool/validators/`, keyed on `sha256(file_content) + name + cmd + tool fingerprint`. Validators skip re-running when neither the file nor the tools behind them have changed.

Disable caching per-call with the `SUPERTOOL_NO_VALIDATOR_CACHE=1` env var, or per-project with `"validator_cache": false` in `.supertool.json`.

**Tool fingerprint.** The key describes what did the analysing, not only what was analysed — otherwise a result computed by a buggy analyser keeps being replayed after the analyser is fixed. (Found the hard way: `mcp-phpstan-warm` 0.6.0 → 0.7.0 fixed two staleness bugs, and the very next run still served 0.6.0's wrong answers, in 0.2s, doing no work at all.) The fingerprint is a hash of `stat` signatures — `size` + `mtime_ns` — from two sources:

| source | catches |
|--------|---------|
| every token of the resolved `cmd` that is an existing file | adapter script edits, interpreter and binary swaps |
| `fingerprint_paths` on the validator spec | a specific config or binary that validator depends on |
| `validator_fingerprint_paths` at config top level | project-wide: the lockfile |

A lockfile is the high-value entry. Many analysers launch through a wrapper script whose own bytes never change between versions — composer bin proxies are exactly that — so stat-ing the launcher proves nothing. `composer.lock` (or `package-lock.json`) changes on **any** dependency upgrade:

```json
// .supertool.json
{
  "validator_fingerprint_paths": ["composer.lock"],
  "validators": {
    "phpstan": {
      "cmd": "...",
      "fingerprint_paths": ["phpstan.neon"]
    }
  }
}
```

Paths that don't exist contribute nothing rather than failing the lookup — a missing lockfile makes the fingerprint weaker, never disables caching. Fingerprints are computed once per process, so this costs a handful of `stat` calls per run, not per file.

**TTL.** Entries expire `validator_cache_ttl_hours` after they're written (default `24`; set `0` to disable expiry). This is the backstop for staleness the key still can't see: tool upgrades and adapter edits are now keyed directly by the fingerprint above, but a transient engine failure a clean re-run would pass, or a config file nobody listed in `fingerprint_paths`, still slips through. Expiry is on access: a stale entry is treated as a miss, re-runs, and is rewritten with a fresh timestamp. No cron needed.

**Engine failures are never cached.** The core cache filter is generic: it excludes a result whose error *code* is non-deterministic (MCP transport errors, non-zero adapter exits) and never inspects tool-specific message text (per `SCHEMA.md`: "Validator core never parses tool-specific output"). Real findings (PHPStan types, `rector.refactor` suggestions) are deterministic and stay cached. Message-level engine glitches — rector's `System error: ...` reflection failures and `Call to a member function toMutatingScope() on null` — are dropped at the source by the adapter, configured per-mcp via the `validators.rector.engine_glitches` prop in `.supertool.json` (a JSON list of case-sensitive substrings the adapter reads straight from `.supertool.json`; built-in defaults apply when the prop is absent). Add a new signature there — no code change. This prevents a transient warm-daemon hiccup from freezing a failure that replays on every later run (the June-2026 2100-poisoned-entries incident). See `validators/rector-mcp/rector-mcp.py`.

## Declining instead of guessing

A validator has three states, not two: `ok`, a finding, and **`skipped`** — an absence of information. A skip never renders a ✗, never triggers `rollback_on_fail`, and is never written to the cache. It exists so that a checker which *cannot* answer says so, rather than emitting a verdict that looks exactly like one it worked out.

**Daemon not installed — the checker that cannot run declines** ([#531](https://github.com/Digital-Process-Tools/claude-supertool/issues/531)). The four warm-process adapters (`phpstan-mcp`, `rector-mcp`, `phpmd-mcp`, `phpunit-mcp`) resolve their daemon binary before spawning it. When it is absent — the normal case for any `cwd:` pointed at a git worktree, where `composer install` never ran — that used to surface as a finding:

```
phpstan-mcp : 1 err  (pre-existing — not from this edit)
     adapter  RuntimeError: mcp-phpstan-warm not found at: /tmp/.../wt-foo
```

A checker reporting on a file it never opened. In a `batch:` of six edits that is eighteen lines of false failure competing with the real validator output, which is the thing the block is being read for. It now reads:

```
phpstan-mcp : skipped     (mcp-phpstan-warm not found at: /tmp/.../wt-foo)
```

**The distinction is made at the raise site, not by matching the message.** `refusal.DaemonUnavailable` is raised only where the binary is looked for and found absent — a missing file at a configured path, or a bare name absent from `$PATH`. Everything that happens *after* a real binary is found — a spawn that dies, a handshake that times out, a daemon that answers badly — remains a `1 err` with an `adapter` code. Guessing towards silence there is how a genuinely broken validator starts looking clean, which is a worse failure than the noise this removes. `DaemonUnavailable` subclasses `RuntimeError`, so existing handling around `resolve_bin` is unchanged.

The skip reason keeps the install command (`composer require --dev dpt/mcp-phpstan-warm`, …) on one line, so a validator that is off because nothing is installed is still discoverable from the row that says so. `phplint` is unaffected and still runs in worktrees — it is what actually catches syntax problems there.

**No transport is the same absence, one layer earlier.** The warm analysers are spoken to over a Unix domain socket, and GH-hosted Windows Python builds do not expose `socket.AF_UNIX` even though the OS supports it — the MCP client in `supertool.py` has said so in a comment since it was written, and `tests/test_security_mcp_daemon_148.py` skips its whole module for it. On those builds no warm validator was ever reachable, and each one reported an `adapter` error instead. They now decline through the same `DaemonUnavailable` marker:

```
phpstan-mcp : skipped     (warm daemon needs socket.AF_UNIX, which this Python build does not expose (Windows) …)
```

The check is made before the binary lookup, because without a transport the binary is irrelevant and the platform is the reason a reader can act on. It is not a `getattr` shim over `os.geteuid`: `presets/mcp/_paths.py` is merely the *first* thing that breaks on the way down, and patching only that would have moved the crash three lines later into `socket.socket(socket.AF_UNIX, …)`, producing the same wrong output from a less legible place.

**The ownership check that crashed there now refuses rather than passing.** `runtime_dir()` verifies that the per-user runtime directory belongs to us. Where `os.geteuid` does not exist that comparison is not merely unavailable, it is unanswerable — `st_uid` is a constant `0` on Windows and carries no information. Defaulting it to "ours" would trade a loud failure for a quiet one on the single check whose whole job is to be suspicious, so it exits with a stated reason instead. Nothing reaches it on such a platform today (the adapters decline earlier), but `stop.py` and `status.py` call it too, and the next caller meets a sentence rather than an `AttributeError`.

**`warm_unsafe` — targets a warm process cannot judge.** Warm-process validators (`phpstan-mcp`, `rector-mcp`, `phpunit-mcp`) keep a long-lived daemon so each edit does not re-pay a cold boot. That daemon carries state, and for some targets the state is the answer. The case that produced this field (#345): `mcp-phpunit-warm` runs the project's `phpunit.xml` bootstrap in its long-lived **parent** and forks a child per test call, so every resource that bootstrap opened — a DB handle, a session, a framework singleton — is shared across the parent and all children. A test that touches one of them can fail under the daemon and pass under a cold `phpunit` run of the same file at the same commit. That red is not a fact about the file, and there is no way to tell it apart from a real one by reading the output.

So the project declares which targets are out of the warm runner's reach:

```json
// .supertool.json
{
  "validators": {
    "phpunit": {
      "cmd": "... phpunit-mcp.py {file}",
      "resolve": "bash .claude/scripts/validators/resolve_test.sh {file}",
      "warm_unsafe": ["extends\\s+SiControllerTestCase"]
    }
  }
}
```

Each entry is a Python regex matched against the content of the **resolved** target — the file the adapter would actually have been given, not the file you edited. A hit short-circuits before the adapter is spawned at all:

```
phpunit     : skipped     (warm-unsafe: target matches /extends\s+SiControllerTestCase/ — this va)    -
```

**What this costs.** A warm-unsafe target is not validated inline *at all* — its greens are declined along with its reds, because a green from a runner you have just proved unreliable is worth no more than its red. Run those files through the cold op (`phpunit:PATH`, `phpstan:PATH`) or leave them to CI. The trade is deliberate and is the same one #482 made: a visible gap beats an invisible wrong answer.

**What this is not.** It is not suppression of "pre-existing" failures. A pre-existing failure is a real failure, and hiding it is how a broken file starts looking clean; regression-only rollback (below) already handles those correctly by comparing against a pre-edit baseline. `warm_unsafe` addresses a different problem one layer up — a result that is not about the file at all.

**Failure modes bias towards running.** An unreadable target, a pattern that is not a string, and a pattern that does not compile are all ignored, and one bad pattern does not disarm the good ones beside it. The failure mode of a muting feature is a validator that has silently stopped working, so a config typo leaves the validator running rather than quietly turning it off. Target reads are capped at 256 KB.

## Manual run

Run validators explicitly against any file without an edit op:

```bash
./supertool 'validate:src/Foo.php'                    # all matching validators
./supertool 'validate:src/Foo.php:phplint,phpstan'    # filtered to named validators
./supertool 'validate:a.php,b.php,c.php'              # list form — many files, config loaded once
./supertool 'validate:a.php,b.php:@syntax'            # only parser/compiler validators
```

Useful for a pre-commit sweep or spot-checking a file you didn't edit this session.

**List form** — pass a comma-separated path list (`validate:f1,f2,…[:tool_filter]`) to validate several files in one invocation; the config is loaded once for the whole batch and each path is independently security-checked at dispatch. Single-file `validate:PATH` is unchanged.

**`@syntax` filter** — the special filter `@syntax` selects only validators that declare `"syntax": true` in their spec (the parser/compiler tier), keeping that scope in config rather than a hardcoded caller list. Falls back to the bundled name list for older configs that predate the flag.

## Output example

After an edit that breaks PHP syntax with `rollback_on_fail: true`:

```
[validators]
phplint : 0 → 1        (+1)   ✗
  + L42 parse  Parse error: syntax error, unexpected token "{" in ... on line 42

[rolled back] phplint regressed; file restored
```

The model gets a clean retry surface with the exact line and error — no broken file left behind.

## Adapter contract

Custom validators must conform to the adapter contract in `validators/SCHEMA.md`. Each adapter takes one file arg and prints a JSON object on stdout with a standardised shape (ok, count, errors). The bundled adapters (`validators/phplint/`, `validators/xmllint/`, etc.) are the reference implementations.

Two optional output fields worth knowing: `source_context` (array of source lines centered on the error, rendered indented under each error in verbose mode) and `diff` (unified diff string, rendered as a fenced block — useful for tools like Rector that produce a suggested patch). Full spec in [SCHEMA.md](../validators/SCHEMA.md).

## resolve — map a source file to its real target

By default a validator runs against the file the op touched. The optional `resolve` key lets it run against a *different* file derived from that one — the canonical case is "edited a source file, run its test." `resolve` is a shell cmd that takes the edited file (`{file}`) and prints the path to run instead:

```json
"phpunit": {
  "cmd": "... {file}",
  "match": "*.php",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "resolve": "bash .claude/scripts/resolve_test.sh {file}",
  "tier": "slow"
}
```

**Contract — supertool reads stdout only:**

- Non-empty stdout → that path becomes the validator's target.
- Empty stdout → the validator is **skipped** for this op (nothing sensible to run).

The exit code is **ignored** by the validator path. That is deliberate, and it's what makes the advisory below possible.

### advice — config-driven post-op hints

Autonomous flows that write only through supertool bypass the editor/git-hook reminders to do follow-up work (write a test, regenerate the XSD, …). The top-level `advice` block emits non-blocking `[advice]` lines after a mutating op when a rule matches:

```json
// .supertool.json
{
  "advice": {
    "newTest": {
      "hooks_into": ["paste"],
      "match": "*.php",
      "when": "new-file",
      "resolveFromValidator": true,
      "message": "new class without test"
    },
    "newComponent": {
      "hooks_into": ["edit", "paste"],
      "match": "*.php",
      "contains": "extends \\w*ComponentBase|implements \\w*IComponent",
      "message": "XSD/cache regen likely (dvsi_xsd + dvsi_clearcache)"
    }
  }
}
```

Each rule is gated by (all optional):

| field | meaning | default |
|-------|---------|---------|
| `hooks_into` | ops that trigger the rule | all mutating (`edit`, `paste`, `append`, `replace`, `replace_lines`, `vim`) |
| `match` | path glob | `*` |
| `when` | `new-file` \\| `existing-file` \\| `always` — gated on whether the file existed before the op | `always` |
| `contains` | regex tested against the **content the op added** (lines present after but not before) — fires only when the op *introduces* the pattern, not when the file already held it | — (no content gate) |
| `resolve` | a subprocess (a source→target resolver) emitting a would-be target | — |
| `resolveFromValidator` | reuse the first `resolve` cmd declared on a validator instead of duplicating it | `false` |
| `message` | the line shown; `{target}`/`{path}`/`{op}` interpolate. A message with no `{target}` gets ` — consider <target>` appended when a resolver produced one | `""` |

A `paste` that **creates a new `*.php` file** with no resolvable sibling test (the `newTest` rule above) appends:

```
[advice]
ℹ new class without test — consider tests/unit/SiX/FooTest.php
```

#### The resolve contract

A `resolve`/`resolveFromValidator` rule reuses the same `resolve` cmd declared on a validator, so the source→target path logic lives in exactly one place. Because the advisory needs the *would-be* target (which doesn't exist yet) while the validator path needs stdout to stay empty on a miss, the resolver signals the two cases on different channels:

| case | stdout | stderr | exit |
|------|--------|--------|------|
| target exists | target path | — | 0 |
| no target | *(empty)* | would-be target | 3 |

stdout stays empty on a miss → the validator still skips, unchanged. The miss target rides on **stderr**, flagged by **exit 3** → only the advisory reads it. Advisory only: it never blocks the write.

## Format-on-save

See [formatters.md](formatters.md) — formatters run after every edit, before validators, normalizing whitespace and style before the safety check runs.
