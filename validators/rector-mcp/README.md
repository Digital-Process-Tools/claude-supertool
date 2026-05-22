# rector-mcp validator adapter

Warm-process Rector validator for supertool. Routes through the [`mcp-rector-warm`](https://github.com/Digital-Process-Tools/mcp-rector-warm) MCP server via supertool's UDS daemon. ~9× faster per-edit than spawning `vendor/bin/rector` fresh each time.

## Install

```bash
composer global require dpt/mcp-rector-warm
```

That puts `mcp-rector-warm` on `$PATH`.

## Wire into `.supertool.json`

**1. Declare the MCP server in the `mcp` block:**

```json
{
  "mcp": {
    "rector-warm": {
      "cmd": [
        "mcp-rector-warm",
        "--working-dir=/absolute/path/to/your/project",
        "--config=/absolute/path/to/your/project/rector.php"
      ],
      "match": "*.php",
      "timeout": 120,
      "idle_timeout": 1800
    }
  }
}
```

**2. Register the validator:**

```json
{
  "validators": {
    "rector": {
      "cmd": "MCP_RECTOR_WORKING_DIR=/abs/path/to/project MCP_RECTOR_CONFIG=/abs/path/to/project/rector.php python3 {supertool_dir}/validators/rector-mcp/rector-mcp.py {file}",
      "match": "*.php",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "timeout": 120
    }
  }
}
```

## Env vars

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_RECTOR_BIN` | `mcp-rector-warm` (resolved via `$PATH`) | Path to the MCP server binary. Override if not installed globally. |
| `MCP_RECTOR_WORKING_DIR` | `cwd` | Project root passed to the daemon. |
| `MCP_RECTOR_CONFIG` | (none) | Optional `--config=` flag passed to the daemon. |
| `MCP_RECTOR_DAEMON_NAME` | `rector-warm` | Must match the key in `.supertool.json`'s `mcp` block. |

## How it works

1. Adapter receives a file path from supertool.
2. Connects to UDS socket `/tmp/supertool-mcp-<hash>.sock` (hash = `sha1(cwd::daemon-name)[:12]`).
3. If socket missing → spawns `presets/mcp/daemon.py rector-warm --detach`, which spawns `mcp-rector-warm` and bridges its stdio over the socket.
4. Sends MCP `initialize` + `tools/call rector_process` over NDJSON.
5. Returns SCHEMA.md-compliant validator JSON on stdout.

Subsequent calls reuse the warm daemon — Rector container is hot.

## See also

- [`mcp-rector-warm`](https://github.com/Digital-Process-Tools/mcp-rector-warm) — the underlying MCP server
- `presets/mcp/daemon.py` — UDS daemon that auto-spawns and bridges MCP servers
- `validators/SCHEMA.md` — validator JSON contract
