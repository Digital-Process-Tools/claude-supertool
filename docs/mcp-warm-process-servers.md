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
3. Connects, or auto-spawns daemon via `presets/mcp/_spawn.py` → `presets/mcp/daemon.py`
4. Sends MCP `initialize` + `tools/call`
5. Formats response as SCHEMA.md validator JSON

## Daemon lifecycle contract

**At most one live daemon per (kind, config fingerprint), and a request is never
served by a daemon whose config has since changed.** Both halves are load-bearing:
one daemon still holding stale config just makes the wrong answer consistent.

Per daemon, in the runtime dir (`$XDG_RUNTIME_DIR/supertool/mcp/` or
`~/Library/Caches/supertool/mcp/` — owned by you, mode `0700`, and with every
directory above it owned by you or root and not writable by anyone else, or the
daemon refuses to start: see
[mcp-integration.md](mcp-integration.md#the-directories-above-the-runtime-dir-are-checked-too-607)),
all named `supertool-mcp-<sha1[:12]>`:

| File      | Written by | Meaning                                     |
| --------- | ---------- | ------------------------------------------- |
| `.sock`   | daemon     | UDS the adapter talks to                    |
| `.pid`    | daemon     | ownership claim — `O_CREAT\|O_EXCL`, atomic |
| `.fp`     | daemon     | fingerprint of the config it booted with    |
| `.lock`   | adapter    | `flock` serialising check-and-spawn         |

- **A daemon is usable only if all three of** socket exists, pid is alive (via the
  shared `presets/_proc.py` probe), and the on-disk fingerprint equals the current
  one. Socket without pid is a stale file with no listener; pid without socket is a
  daemon still booting or dead mid-publish.
- **Spawning is serialised** by an exclusive `flock` on `.lock`, with the usability
  check re-run *after* the lock is taken. Concurrent callers arriving inside the
  startup window — the seconds between `Popen` and the socket being bound — wait
  for the winner instead of each starting their own (#451).
- **`daemon.py` claims the pidfile before any side effect.** A daemon that finds
  the slot taken exits without unlinking the socket and without spawning its MCP
  server child, so losing the race costs one short-lived Python process.
- **A live daemon whose fingerprint no longer matches is reaped (`SIGTERM`, then
  `SIGKILL`) and respawned.** `SIGTERM` is what lets it tear down its own MCP
  server child; skipping it orphans a heavy PHP process.
- **Only a caller that can wait may create a daemon.** Nothing reaps a daemon
  whose client died — the double-fork severs the link, and `idle_timeout` counts
  from the last bridged byte, not from the caller's liveness. A validator killed
  at its 3s budget therefore leaves a fully-indexed MCP server resident for the
  whole idle window having answered nothing. Supertool stamps
  `SUPERTOOL_MCP_AUTOSPAWN=0` into every validator adapter's environment, which
  the adapter's children inherit: use a warm daemon, never start one, and fail
  fast on a miss instead of polling. Opt a validator back in with
  `"mcp_autospawn": true` when its timeout genuinely covers a cold start (#475).
  `ensure_daemon` reads the flag *after* the warm-daemon fast path and *before*
  the spawn lock, so suppression removes creation and never use. It declines by
  raising `_spawn.AutospawnSuppressed`, and each adapter turns that into a
  `skipped` receipt whose reason names the flag — until #1743 nothing under
  `validators/` or `presets/mcp/` read the variable at all, and `rector-mcp`
  with it set to `0` spent its full 30s spawn budget raising the daemon it had
  been told not to create.
- **Fingerprint = content, not mtime**: the resolved mcp spec (json, key-sorted)
  plus sha256 of every existing file named in its `cmd`/`args`/`env` — the config
  file, and the `mcp-*-warm` binary when the spec names it by path (absolute or
  project-relative, as `.supertool.json` normally does), so a server upgrade
  retires the daemon running the old one. Re-saving a file unchanged keeps the
  daemon warm, and a spec edit that changes only whitespace is not a change.
  Two limits worth knowing: a bare `$PATH` name (`"cmd": "mcp-phpmd-warm"`) is
  not resolved and so not hashed, and only files named **directly** in the spec
  are hashed — a `phpstan.neon` with `includes:` re-fingerprints when the
  top-level file changes, not when the include does.
- **A stale pidfile never wedges a spawn.** A dead owner's pidfile is cleared under
  `O_EXCL` — no daemon at all is a worse outcome than a duplicate.

`mcp_status` lists what is running; `mcp_stop:NAME` / `mcp_stop_all` retire them.

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
