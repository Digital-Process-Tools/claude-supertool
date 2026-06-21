# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`restartMcp` key on custom ops.** A custom op that invalidates state the warm MCP daemons cache (autoload map, PHPStan/PHPUnit result caches, …) can declare `"restartMcp": true` (every server in the `mcp` block) or `"restartMcp": ["name", …]` (a subset). After the op's `cmd` **succeeds**, supertool SIGTERMs those daemons via the same `stop.py` path the new-file invalidation uses; each cold-starts on its next call with a fresh index. Skipped when the cmd fails. Lets a single cache-clearing op (e.g. `dvsi_clearcache`) also reset the warm LSP layer in one round-trip, instead of leaving daemons serving stale analysis until the idle timeout.
- **`gl-mrs` — MR triage board.** Lists MRs (default `author=@me`), sorted failing-first then stalest, each enriched in parallel with everything you'd otherwise round-trip for: pipeline status (a failure shows the failed **job name** — `phpstan2`, `test_unit_dpt` — i.e. the failure class, pre-empting a `gl-job` call), approval state, age, diff size, watch-state cross-reference (which MRs already have a live `watch` poller), and `conflict`/`draft`/`threads` flags. Footer points at the first failing-and-unwatched MR. Filters: `author`/`reviewer`/`assignee`/`label`/`milestone`/`state`/`per`. Flags: `nopipe`, `iids` (bare id list), `failed` (only failing). Tunable via `enrich_workers`/`enrich_cap`/`per_page`. "Mine" is just the default filter, not a special op — enumeration is a platform concern, kept out of the generic `watch` preset. See [docs/presets/gitlab.md](docs/presets/gitlab.md).
- **`gh-prs` — PR triage board (the `gl-mrs` twin).** Lists GitHub PRs (default `author=@me`), sorted failing-first then stalest. Per PR: check rollup (a failure shows the failing **check name** — the failure class, pre-empting a `gh-job` call), approval state (`reviewDecision`), age, diff size, watch-state cross-reference, and `draft`/`conflict`/`threads` flags. Footer points at the first failing-and-unwatched PR. Unlike GitLab, `gh pr list --json` returns checks + approval in **one** call, so the core board costs a single request; only unresolved review-thread counts enrich in a second parallel wave (skip with `nopipe`). Filters: `author`/`reviewer`/`assignee`/`label`/`state`/`per`. Flags: `nopipe`, `iids` (bare number list), `failed`. Closes [#274](https://github.com/Digital-Process-Tools/claude-supertool/issues/274). See [docs/presets/github.md](docs/presets/github.md).
- **`presets/watch/watch-mine.sh` — watch-a-whole-query supervisor.** Glue between a "list mine" op (`gl-mrs`/`gh-prs`) and the `watch` preset: runs the feed op, extracts ids, spawns one watcher each. Idempotent, so it's safe on `/loop`. `gl-mrs:failed,iids | watch` turns "ping me when any of my MRs goes red" into one looped command. See [docs/presets/watch.md](docs/presets/watch.md).
- **Configurable default `project` / `repo` for issue-create payloads.** `gl-issue-create` and `gh-issue-create` no longer require `project`/`repo` in every payload. Resolution order (most specific wins): explicit payload field → `defaults.gitlab_project` / `defaults.github_repo` in `.supertool.json` → the `origin` git remote, host-matched (`gitlab` / `github.com`). In the matching checkout the remote covers it with zero config. Closes [#270](https://github.com/Digital-Process-Tools/claude-supertool/issues/270). See [docs/presets/gitlab.md](docs/presets/gitlab.md) and [docs/presets/github.md](docs/presets/github.md).
- **`watch` preset — background event pollers with async wake into Claude Code.** New ops `watch:SOURCE:ID[:only=...]`, `unwatch:SOURCE:ID`, `watches`. Each invocation forks a detached poller that emits state-change events to a UDS socket, a status file, and macOS Notification Center. Source-plugin contract makes new sources ~50 lines. Bundled sources: `github-pr` (PR state, checks, reviews, comments, merge/close, conflicts) and `gitlab-mr` (MR state, pipeline, merge/close, conflicts). Closes [#165](https://github.com/Digital-Process-Tools/claude-supertool/issues/165). See [docs/presets/watch.md](docs/presets/watch.md).
- **`notifiers/claude-channel/` — MCP channel server for async wake.** TypeScript / Bun server that binds the watch UDS socket and pushes events into a running Claude Code session via the Channels feature (research preview, requires Claude Code v2.1.80+). Events arrive as `<channel source="claude-channel" watcher_source="<source>" id="<id>" event="<event>" ...>` tags. UDS file mode 0600 + localhost-only auth model. Launch with `claude --dangerously-load-development-channels server:claude-channel`. See [notifiers/claude-channel/README.md](notifiers/claude-channel/README.md).
- **`glob` brace expansion.** `glob:src/**/*.{json,xml}` now fans out into `*.json` + `*.xml` (shell/fd/ripgrep semantics) and dedupes results, instead of silently returning 0 files. Supports multiple groups (`{a,b}.{x,y}` → 4) and nesting (`{a,b{1,2}}` → 3). Patterns without braces are unchanged. Closes [#161](https://github.com/Digital-Process-Tools/claude-supertool/issues/161).
- **`validator_cache_ttl_hours` config** (default `24`, `0` disables). Validator cache entries expire this long after being written. The cache key only hashes file content, so without a TTL an entry outlives changes it can't see — an updated adapter/`rector.php`, or a transient engine failure. Expiry is on access (stale → miss → re-run → rewrite); no cron needed.

### Fixed

- **Validator cache no longer freezes transient engine failures.** `rector-mcp`'s warm daemon intermittently trips rector's own `System error: "ClassReflection must be resolved for class X"` reflection bug on test classes — warm-process-state dependent, not file dependent (a clean re-run and plain `rector` CLI pass the same file). That failure was cached keyed on file content and replayed on every later run, including its frozen `duration_ms`; 2100 entries were poisoned this way across a test suite. Two-layer fix: (1) `validators/rector-mcp/rector-mcp.py` drops `System error:` results at the source — an engine glitch is not a code finding; (2) the validator cache now excludes non-deterministic engine failures (MCP transport errors, non-zero exits, `System error:` messages) from being written. Real findings (PHPStan types, `rector.refactor`) stay cached.
- **`git-merge` now fetches before merging a local branch — no more silent stale merges.** The docstring promised "fetch + merge" but there was no fetch: `git-merge master` merged the *local* `master` ref, which is stale the moment origin moves ahead. Real-world bite: a fix landed on origin/master, but merging `master` into a feature branch silently used the lagging local ref and the fix "wasn't there" — the failing job kept failing. Now `_fresh_merge_ref()` resolves the ref's upstream, fetches it, and if the local branch is behind, redirects the merge to the upstream (e.g. `origin/master`) with a loud note: `local master was N behind origin/master — merging origin/master (latest) instead`. Offline / fetch-fail / no-upstream cases warn and fall back to the local ref (no hard failure). Remote-tracking refs like `origin/master` are refreshed directly.

## [0.14.0] — 2026-05-23

### Added

- **`validators/pyright/`** — Python type-check via `pyright --outputjson`. Sits next to `py-compile` (syntax-only). Hooks into `edit/replace/replace_lines/paste/vim` for `*.py`. Graceful skip when `pyright` not on PATH; pyright JSON 0-indexed `range.start` is converted to supertool's 1-indexed `line/col`. Configure via `.supertool.example.json`. README validator count bumped to 18.

### Security

- **`op_paste` containment ordering.** Previously `op_paste` called `os.makedirs(parent)` BEFORE `_atomic_write`'s `_safe_path` check, so a traversal path like `../../tmp/evil/foo` would create directories outside cwd before the write itself was rejected — leaving empty-dir pollution. `op_paste` now runs `_safe_path` at entry. Side benefit: closes the NUL-byte-in-path xfail (`_safe_path` rejects NUL before `os.replace` can leak a `ValueError`).

### Security

- **Hardening bundle.** Smaller defence-in-depth items from the audit (closes [#150](https://github.com/Digital-Process-Tools/claude-supertool/issues/150)):
  - **Gitcli flag smuggling**: `git-checkout REF`, `git-merge REF`, `git-blame PATH` reject refs/paths starting with `-` (e.g. `--orphan=evil`, `--abort`, `-X theirs`). `git-blame` uses `--` separator to keep a path-with-dash from being parsed as a flag.
  - **Regex ReDoS guards** on `grep`: pattern length capped at 1000 chars; nested unbounded quantifiers (`(a+)+`, `(.+)*`) rejected before they touch file content.
  - **`validate_staged` / `format_staged`** use `git diff -z --diff-filter=ACMR` (filenames with newlines/quotes survive) and reject symlinks — a staged symlink to `/etc/hosts` would otherwise be REWRITTEN by formatters.
  - **`xmllint` validator** runs with `--nonet --noent` so libxml2 can't fetch external entities or follow URI references during validation (XXE defence-in-depth).
  - **Validator cache HMAC** at `~/.cache/supertool/.cache_key` (mode 0600, 32 random bytes, generated on first use). Each cache entry wrapped with HMAC-SHA256 of its body. Attacker with write access to `~/.cache/supertool/validators/` cannot forge a passing `ok: true` entry without also reading the secret. Legacy unwrapped entries treated as miss.
- **Social publishing safety.** Three guards for the dev.to / bluesky publish + comment ops (closes [#149](https://github.com/Digital-Process-Tools/claude-supertool/issues/149)):
  - `file://` body must resolve under `.max/`, `drafts/`, `posts/`, or `blog/` (relative to cwd). Closes the credential-exfil vector: `bluesky_publish:file:///Users/.../app_password` now rejected before the file is read. Extend additively via `$SUPERTOOL_PUBLISH_BODY_ALLOWLIST=path1:path2` or `"publish_body_allowlist": [...]` in `.supertool.json`.
  - Confirm-default — `bluesky_publish` / `devto_publish` refuse to run without `|force`, `SUPERTOOL_NO_PUBLISH_CONFIRM=1` env, or `"no_publish_confirm": true` in `.supertool.json`. Blocks single-shot publish from a prompt-injected op.
  - Token-file mode check — `check_token_file_mode` refuses to load tokens at mode 0644+ (group/world-readable). Mirrors `ssh`'s SSH-key check.
- **MCP daemon UDS hardened.** Socket + pidfile + log moved from `/tmp/supertool-mcp-<hash>.{sock,pid,log,stderr}` (world-writable parent dir, predictable hash) to per-user runtime dir (`$XDG_RUNTIME_DIR/supertool/mcp/` on Linux, `~/Library/Caches/supertool/mcp/` on macOS, `~/.cache/supertool/mcp/` fallback). Dir created mode `0700`, ownership-checked at startup. Pidfile + log files open with `O_NOFOLLOW|O_CREAT|O_EXCL` so symlink-squatting attacks no longer overwrite victim files. Peer-uid check on every accept (defence in depth on top of parent-dir perms). Server names validated against `[A-Za-z0-9_-]{1,64}`. `os.umask(0o077)` reset in detach. Override via `$SUPERTOOL_RUNTIME_DIR`. Closes [#148](https://github.com/Digital-Process-Tools/claude-supertool/issues/148).
- **Vim shell verbs gated.** `:!cmd`, `:%!cmd`, `:N,M!cmd`, `:r !cmd` are disabled by default. A prompt-injected vim payload like `:!rm -rf ~` is rejected with a clean error. Editor verbs (i/a/o/d/s/etc.) are unaffected. Opt-in via `SUPERTOOL_ALLOW_VIM_SHELL=1` (env) **or** `"allow_vim_shell": true` in `.supertool.json`. Closes [#147](https://github.com/Digital-Process-Tools/claude-supertool/issues/147).
- **`:r FILE` honours cwd containment.** Vim's `:r FILE` (without `!`) now routes through `_safe_path` — reading `/etc/passwd` or `~/.ssh/id_rsa` into the buffer is rejected. Opt-out is the same `SUPERTOOL_ALLOW_OUTSIDE_CWD=1` as the rest of #146.
- **Path containment (`_safe_path`).** Every op path arg now resolves under cwd. A malicious `.supertool.json` or prompt-injected op like `paste:~/.ssh/authorized_keys:::pwned` or `read:/etc/passwd` is rejected with a clean error. Symlinks crossing the boundary are caught (realpath follows them). NUL bytes rejected early. Closes [#146](https://github.com/Digital-Process-Tools/claude-supertool/issues/146).
- **Opt-out**: set `SUPERTOOL_ALLOW_OUTSIDE_CWD=1` (env, one-off) **or** `"allow_outside_cwd": true` in `.supertool.json` (project-pinned, no env var dance). Env takes precedence.
- **Default excludes extended**: `.env`, `.env.*`, `.max/`, `.ssh/`, `.aws/`, `.gnupg/`, `.kube/`, `.docker/`, `.terraform/`, `.chef/`, `.npm/`, `secrets/`, `credentials/` are now pruned by `grep`/`glob`/`tree`/`map` so tokens don't surface into an LLM's context.
- Windows-compat: `os.path.normcase` handles case-insensitive drive letters (`c:\Users` == `C:\users`) and separator normalization.

## [0.13.1]

### Added

- Per-validator `cache: false` opt-out (`_validator_run_one`). Useful when the validator's input file isn't the only thing affecting results (e.g. phpunit: source + test + bootstrap + DI graph all matter, cache key only hashes the resolved file → stale hits when source changes).

### Changed

- `validators/rector-mcp/`: drops bare "would refactor X" entries that have no `applied_rectors` or diff. Rector returns these in `--debug` mode (which we need for speed on large configs) but they carry no actionable info. Real errors with rule names + diffs still pass through.

## [0.13.0]

### Added

- `validators/rector-mcp/` — bridges supertool validators to [`dpt/mcp-rector-warm`](https://github.com/Digital-Process-Tools/mcp-rector-warm) via UDS daemon. ~14× faster per edit on Rector validation.
- `validators/phpunit-mcp/` — bridges to [`dpt/mcp-phpunit-warm`](https://github.com/Digital-Process-Tools/mcp-phpunit-warm). ~25× faster per edit.
- `validators/phpstan-mcp/` — bridges to [`dpt/mcp-phpstan-warm`](https://github.com/Digital-Process-Tools/mcp-phpstan-warm) (PHPStan worker over TCP NDJSON). ~30× faster per edit.
- `docs/mcp-warm-process-servers.md` — overview of the three warm MCP servers and how to wire them.

All three adapters share the same shape: auto-spawn UDS daemon via `presets/mcp/daemon.py`, send MCP `initialize` + `tools/call`, format response as SCHEMA.md validator JSON.

## [0.11.0]

Initial public changelog. See git history for prior versions.

[Unreleased]: https://github.com/Digital-Process-Tools/claude-supertool/compare/v0.13.0...HEAD
[0.13.0]: https://github.com/Digital-Process-Tools/claude-supertool/releases/tag/v0.13.0
[0.11.0]: https://github.com/Digital-Process-Tools/claude-supertool/releases/tag/v0.11.0
