# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

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
