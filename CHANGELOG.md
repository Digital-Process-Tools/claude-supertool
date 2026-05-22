# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
