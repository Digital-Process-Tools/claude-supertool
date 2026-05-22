# Warm-process MCP servers (DPT)

Real-world MCP servers built on top of supertool's daemon. They keep heavy PHP tools (Rector, PHPUnit) bootstrapped across calls — same daemon pattern as `cclsp` for LSPs.

## Servers

| Package | Cold | Warm | Speedup | GitHub |
|---------|------|------|---------|--------|
| `dpt/mcp-rector-warm` | ~4500ms | ~500ms | ~14× | [repo](https://github.com/Digital-Process-Tools/mcp-rector-warm) |
| `dpt/mcp-phpunit-warm` | ~1600ms | ~50ms | ~25× | [repo](https://github.com/Digital-Process-Tools/mcp-phpunit-warm) |

Both are MIT/community-licensed, on Packagist, compatible with any MCP client (Claude Desktop, Cline, Continue, Cursor, Zed, supertool).

## Validator adapters (in this repo)

- `validators/rector-mcp/rector-mcp.py` — talks to mcp-rector-warm via UDS
- `validators/phpunit-mcp/phpunit-mcp.py` — talks to mcp-phpunit-warm via UDS

Each adapter:

1. Reads `MCP_<TOOL>_BIN`, `MCP_<TOOL>_WORKING_DIR`, `MCP_<TOOL>_CONFIG` env vars
2. Computes UDS socket path (sha1 of `cwd::daemon-name`)
3. Connects, or auto-spawns daemon via `presets/mcp/daemon.py`
4. Sends MCP `initialize` + `tools/call`
5. Formats response as SCHEMA.md validator JSON

## Example wiring (.supertool.json)

```json
{
  "mcp": {
    "rector-warm": {
      "cmd": ["vendor/bin/mcp-rector-warm",
               "--working-dir=/abs/path/to/project",
               "--config=/abs/path/to/project/rector.php"],
      "match": "*.php",
      "timeout": 120,
      "idle_timeout": 1800
    }
  },
  "validators": {
    "rector": {
      "cmd": "MCP_RECTOR_WORKING_DIR=/abs/path MCP_RECTOR_CONFIG=/abs/path/rector.php python3 {supertool_dir}/validators/rector-mcp/rector-mcp.py {file}",
      "match": "*.php",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "timeout": 120
    }
  }
}
```

## Per-tool gotchas

### Rector

- Parallel mode forcibly disabled via `--debug` (workers can't respawn from MCP binary).
- Rector ships with runtime-prefixed Symfony namespace (`RectorPrefix<date>\\...`); runner detects prefix via `ReflectionClass` for forward compat.

### PHPUnit

- 5 static singletons must reset between calls (`EventFacade`, `Registry`, `TestResultFacade`, `CodeCoverage`, `OutputFacade`) — otherwise `EventFacadeIsSealedException` on second call.
- `DefaultPrinter` writes directly to `php://stdout` via `fwrite` (bypasses `ob_start`). Run forces `--no-output` and captures structured results via `--log-junit` to a temp file.

## PHPStan?

Researched 2026-05-22 — viable but hostile. PHPStan ships as phar with runtime-prefix; in-process embedding deadlocks the worker event loop. Two real paths:

1. **Subprocess + phpstan's built-in result cache** (~1 day, 200-400ms warm)
2. **TCP worker daemon** (~3-4 days, 50-200ms warm) — be the parent, hold one `phpstan worker` warm via NDJSON-over-TCP

Not yet built.
