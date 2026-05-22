# phpunit-mcp validator adapter

Warm-process PHPUnit validator for supertool. Routes through the [`mcp-phpunit-warm`](https://github.com/Digital-Process-Tools/mcp-phpunit-warm) MCP server via supertool's UDS daemon. ~4× faster per-edit than spawning `vendor/bin/phpunit` fresh each time.

## Install

```bash
composer global require dpt/mcp-phpunit-warm
# or as a dev dep in your project
composer require --dev dpt/mcp-phpunit-warm
```

That puts `mcp-phpunit-warm` on `$PATH` (or `vendor/bin/`).

## Wire into `.supertool.json`

**1. Declare the MCP server in the `mcp` block:**

```json
{
  "mcp": {
    "phpunit-warm": {
      "cmd": [
        "mcp-phpunit-warm",
        "--working-dir=/absolute/path/to/your/project",
        "--config=/absolute/path/to/your/project/phpunit.xml"
      ],
      "match": "*.php",
      "timeout": 300,
      "idle_timeout": 1800
    }
  }
}
```

**2. Register the validator:**

```json
{
  "validators": {
    "phpunit": {
      "cmd": "MCP_PHPUNIT_WORKING_DIR=/abs/path/to/project MCP_PHPUNIT_BIN=/abs/path/to/project/vendor/bin/mcp-phpunit-warm python3 {supertool_dir}/validators/phpunit-mcp/phpunit-mcp.py {file}",
      "match": "*.php",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "resolve": "bash .claude/scripts/validators/resolve_test.sh {file}",
      "timeout": 300
    }
  }
}
```

## Env vars

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_PHPUNIT_BIN` | `mcp-phpunit-warm` (resolved via `$PATH`) | Path to the MCP server binary |
| `MCP_PHPUNIT_WORKING_DIR` | `cwd` | Project root |
| `MCP_PHPUNIT_DAEMON_NAME` | `phpunit-warm` | Must match the key in `.supertool.json` mcp block |

## Output

Returns SCHEMA.md-compliant JSON with:

- `ok` — true if all tests pass
- `count` — failure + error count
- `errors[]` — each with line, severity, code (`phpunit.failure` or `phpunit.error`), message + 5-line source context
- `metrics` — `tests_total`, `tests_passed`, `tests_skipped`, `assertions`

## See also

- [`mcp-phpunit-warm`](https://github.com/Digital-Process-Tools/mcp-phpunit-warm) — the underlying MCP server
- [`validators/rector-mcp/`](../rector-mcp/) — same pattern for Rector
