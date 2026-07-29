# MCP Integration

**Status:** ✅ shipped · daemon transport, preset-packaged

Supertool talks to MCP servers (cclsp, custom LSP wrappers, anything that speaks the
[Model Context Protocol](https://modelcontextprotocol.io/)) so ops like `resolve`,
`refs`, `diag`, `hover`, `rename`, and `workspace` can return real LSP answers
instead of grep heuristics.

## Why it exists

LLMs navigating a codebase with `grep` and glob patterns are slow and inaccurate. A
language server (intelephense, pyright, typescript-language-server, gopls, etc.) knows
where every symbol is defined, what references it, and what types it has — instantly,
once warm. The problem: an LSP cold-starts in 5–60s on a large repo, and supertool is
a CLI that exits between calls.

The integration:

- A small **MCP daemon** stays alive between supertool invocations
- The daemon owns one MCP server subprocess (e.g. `cclsp` wrapping `intelephense`)
- The LSP indexes once, stays warm, answers later calls in milliseconds
- Supertool connects to the daemon over a Unix socket, sends NDJSON JSON-RPC

Result: an LLM agent gets editor-quality navigation with no per-call indexing penalty.

## Architecture

```
┌─────────────────────┐    UDS     ┌──────────────────┐  stdio   ┌──────────────┐
│ supertool (per-call)│ ◀─NDJSON──▶│ MCP daemon       │ ◀──────▶│ MCP server   │
│ MCPClient           │            │ (long-lived)     │          │ (cclsp, etc.)│
└─────────────────────┘            │ owns subprocess  │          │              │
                                   │ idle-timeout 10m │          │ wraps LSP    │
                                   └──────────────────┘          └──────────────┘
                                                                       │ stdio
                                                                       ▼
                                                                ┌──────────────┐
                                                                │ language     │
                                                                │ server       │
                                                                │ (intelephense│
                                                                │  /pyright/…) │
                                                                └──────────────┘
```

Components:

| Piece | Lives in | Role |
|---|---|---|
| `MCPClient` | `supertool.py` | UDS client; connects to daemon, auto-spawns one on first call |
| `presets/mcp/daemon.py` | preset | Daemon: owns MCP subprocess + bridges UDS↔stdio |
| `presets/mcp/status.py` | preset | List running daemons |
| `presets/mcp/stop.py` | preset | Graceful stop (SIGTERM, SIGKILL fallback); exit status carries the outcome |
| `presets/mcp.json` | preset | Declares `mcp_daemon` / `mcp_status` / `mcp_stop` / `mcp_stop_all` ops |

Socket path: `/tmp/supertool-mcp-<sha1(cwd+name)[:12]>.sock` — per-repo + per-server isolation.

Wire format: newline-delimited JSON-RPC 2.0 — same encoding the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) speaks over stdio.

## LSP-backed ops

Five supertool ops route through MCP when configured. Each maps to a specific MCP tool
via the `mcp.<server>.tools` block.

| Supertool op | Syntax | What it does | Heuristic fallback |
|---|---|---|---|
| `resolve` | `resolve:SYMBOL:FILE` | Symbol → file path. With LSP: workspace-wide. Without: glob heuristic on PHP FQN / Python dotted / JS relative | ✓ |
| `refs` (via `workspace`) | `workspace:FILE` (References section) | Find all references to the file's main symbol | ✓ grep |
| `diag` | `diag:FILE` | LSP diagnostics (errors, warnings, hints) for the file | ✗ no LSP → error message |
| `hover` | `hover:SYMBOL:FILE` | Type signature + doc for the symbol. Internally: locate via `resolve` tool, then `hover` tool at identifier position | ✗ no LSP → error message |
| `rename` | `rename:OLD:NEW:FILE` | Workspace-atomic rename across all files. cclsp's `rename_symbol` writes `.bak` backups | ✗ no LSP → error message |
| `workspace` | `workspace:FILE` | Composite view: file content + Diagnostics + Symbols + Imports + References. Each section uses LSP when its tool is mapped, else heuristic | ✓ per-section |

**Two-step ops**: `hover` calls two MCP tools per invocation — first `tools.resolve` to find the symbol's line:col, then `tools.hover` at the identifier offset. Both mappings are required for `hover`.

**Hard-fail ops**: `diag`, `hover`, `rename` only make sense with a real LSP. Without a matching `mcp.<server>.tools.<op>` mapping, they return a clear error rather than falling back to grep.

## Quickstart — PHP via Intelephense

Three minutes from clean repo to working LSP-backed `resolve`.

### 1. Install LSP + bridge

```bash
npm install -g intelephense cclsp
```

[`cclsp`](https://github.com/ktnyt/cclsp) is a generic MCP↔LSP bridge. Free intelephense
covers go-to-def, references, hover, workspace symbols — that's enough.

### 2. Configure the bridge

Create `.claude/cclsp.json` (cclsp's own config — points it at the LSP):

```json
{
  "servers": [
    { "extensions": ["php"], "command": ["intelephense", "--stdio"], "rootDir": "." }
  ]
}
```

### 3. Wire supertool

Edit `.supertool.json`:

```json
{
  "presets": ["mcp"],
  "mcp": {
    "php-lsp": {
      "cmd": "cclsp",
      "match": "*.{php,class.php}",
      "env": { "CCLSP_CONFIG_PATH": ".claude/cclsp.json" },
      "tools": {
        "resolve": "find_workspace_symbols",
        "refs":    "find_references",
        "diag":    "get_diagnostics",
        "hover":   "get_hover",
        "rename":  "rename_symbol"
      },
      "timeout": 60
    }
  }
}
```

- `cmd` — what the daemon spawns (an MCP server)
- `match` — glob; supertool routes ops on matching files through this server
- `env` — environment for the spawned MCP server
- `tools` — maps supertool op names to MCP tool names exposed by the server. Omit any op you don't want to use; that op falls back to the heuristic path (where one exists)
- `timeout` — request timeout in seconds (LSP cold-start can be slow; 60s is comfortable)
- `infra_patterns` — list of substrings that mark a tool result as an infrastructure condition (timeout/overload) rather than a real diagnostic. Some servers (cclsp) swallow their own internal timeout and hand it back as normal text content (e.g. `orchestrator timeout after 3s`) with the MCP `isError` flag unset — without this, `diag` would count that text as a phantom `+1` diagnostic that reads like the edit caused a regression (#346). Matched results are returned prefixed `op: …` so adapters drop them. Defaults to `["orchestrator timeout", "timed out after"]`; the structural `isError` flag is always honored regardless
- `stopOnNewFile` — `true` to SIGTERM this daemon when a mutating op (`edit`/`paste`/etc.) **creates a brand-new file** matching `match`. The warm LSP holds a reflection cache that doesn't index new classes, so it reports phantom errors on a just-created file (#239); stopping it forces the next validator run to cold-start a daemon that sees the file. Cost: that one post-create validate pays the cold-reindex (~30-60s on a large repo). Leave unset for servers that index new files fine.

### 4. Use it

```bash
$ ./supertool 'resolve:My\Namespace\TargetClass:src/Caller.php'
My\Namespace\TargetClass → /abs/path/src/My/Namespace/TargetClass.php
```

First call spawns the daemon detached, waits for the socket, then the LSP indexes (cold,
5–60s on big repos). Subsequent calls hit a warm daemon: <1s.

## Daemon lifecycle

| Op | Effect |
|---|---|
| `mcp_daemon:NAME` | Start daemon for `NAME` (blocking; append `--detach` to background) |
| `mcp_status` | List running daemons: name, hash, pid, status, uptime, idle, socket |
| `mcp_stop:NAME` | Graceful stop (SIGTERM, SIGKILL after 3s) |
| `mcp_stop_all` | Stop every supertool MCP daemon |

The daemon shuts down automatically after `idle_timeout` (default 600s = 10 minutes).
Configure per-server via `"idle_timeout": N` in the `mcp` block.

### Who is allowed to create a daemon

`idle_timeout` is measured from the last byte of traffic, not from the caller — a
detached daemon has no link back to whoever spawned it, so **a caller that dies
still leaves a daemon running for the full window.** That is the right trade for
an interactive op (the next call gets a warm LSP) and the wrong one for a caller
that was never going to survive the cold start: a validator on a 3s budget is
killed long before the LSP answers, and the daemon it created goes on to index
the repository to ~1.3 GB and hold it for ten minutes having served nothing
(#475).

So creating a daemon is gated on provenance, via `SUPERTOOL_MCP_AUTOSPAWN`:

| Value                    | Effect                                                             |
| ------------------------ | ------------------------------------------------------------------ |
| unset (default)          | Auto-spawn allowed — the interactive path, unchanged.               |
| `0` / `false` / `no` / `off` | Connect to a warm daemon if one exists; never start one. A miss fails immediately rather than polling for the connect budget. |
| anything else            | Auto-spawn allowed.                                                 |

Supertool sets it to `0` in the environment of every validator adapter it runs,
and normal environment inheritance carries it to the adapter's own children — so
`lsp-diag.py`, which shells `supertool diag:FILE`, is covered without either
side knowing about the other. Per-validator opt-in is `"mcp_autospawn": true`
(see [validators → field reference](validators.md#field-reference)); exporting
the variable yourself overrides it for any caller.

**Consequence worth stating plainly:** a short-budget validator no longer warms
the LSP as a side effect. It reports LSP diagnostics when a daemon is already
warm and skips cleanly when one is not. Warm the daemon from an op that can
afford to wait — `./supertool 'mcp_daemon:NAME --detach'`, or any interactive
`diag:` / `refs:` / `resolve:` call.

When `MCPClient` can't connect, it spawns the daemon detached via:

```python
subprocess.Popen([sys.executable, daemon.py, NAME, "--detach"], start_new_session=True)
```

and retries the connect for ~7.5s before giving up. For ops with a heuristic fallback
(`resolve`, `refs`, `workspace`), giveup is silent — supertool runs the heuristic and
the call succeeds (slower, less accurate). For LSP-only ops (`diag`, `hover`, `rename`),
giveup surfaces a clear error message so the caller knows the LSP is the bottleneck.

### When the stop fails

Invalidation is an optimization and never blocks the op — a `stop.py` that
refuses, crashes or cannot be spawned leaves your `edit:` untouched and
unreported. It does not, however, leave it *unknowable* (#547).

`stop.py` reports through its exit status, which is the only channel the
automatic caller has:

| Code | Meaning | Treated as |
|---|---|---|
| `0` | the daemon was running and is now gone | success |
| `1` | no daemon was running | success — nothing stale can come from nothing |
| `2` | bad arguments | failure |
| `3` | a daemon was found and is still there, or its pidfile was unreadable | failure |
| `4` | declined before looking — e.g. a runtime dir whose ownership cannot be verified (#544) | failure |

Only the failures are reported, and only on stderr behind
`SUPERTOOL_DEBUG=1`, never in the op's own output:

```
$ SUPERTOOL_DEBUG=1 ./supertool 'paste:::src/New.php:::<?php …'
[supertool debug] mcp stop php-lsp: failed —   failed to stop pid=4242 (…)
```

That gate is the point of the design. Invalidation runs behind every op that
creates a file, so a line in the op body would be user-facing noise on the
overwhelmingly common path where nothing is wrong — a worse trade than the
silence it replaces. Turn the gate on when a warm daemon is reporting phantom
errors on a file you just created; that is #239, and this line is how you find
out whether the safety net for it ran.

Custom ops using `restartMcp` are the exception, and only because they already
print a claim: a daemon that would not die is now listed as `FAILED to stop`
rather than counted in `restarted N daemon(s)`.

## Adding a new MCP server

Whether the server is an LSP via cclsp, a custom MCP wrapper, or any third-party MCP
binary, the wiring is the same.

### Recipe

1. **Get the MCP server runnable** — install it, verify `<binary> --help` (or whatever
   the server's spawn invocation is) works on its own. For cclsp+LSP, this means
   installing both cclsp and the language server (intelephense, pylsp, etc.) and
   declaring the LSP in `.claude/cclsp.json`.

2. **Add an `mcp` entry to `.supertool.json`** — pick a name, point `cmd` at the MCP
   server binary, set `match` to the file glob, declare `env` if the server needs it,
   map supertool ops to MCP tool names in `tools`:

   ```json
   "mcp": {
     "<name>": {
       "cmd": "<mcp-server-binary> [args]",
       "match": "*.<ext>",
       "env": { ... },
       "tools": {
         "resolve": "<MCP tool for symbol→file>",
         "refs":    "<MCP tool for find references>",
         "diag":    "<MCP tool for file diagnostics>",
         "hover":   "<MCP tool for symbol hover (position-based)>",
         "rename":  "<MCP tool for workspace rename>"
       },
       "timeout": 60
     }
   }
   ```

   Keys explained:
   - `cmd` — what `subprocess.Popen` calls (shlex-split if string). The daemon owns
     this process.
   - `match` — fnmatch glob; supertool routes ops on matching `from_file` paths through
     this server. Brace expansion (`*.{php,class.php}`) supported.
   - `env` — extra env vars passed to the spawned MCP server. Merged onto `os.environ`.
   - `tools` — maps supertool op (`resolve`/`refs`/etc.) to the MCP `tool` name the
     server exposes via `tools/list`. Without this, the op falls through to the
     heuristic path.
   - `timeout` — request timeout in seconds.
   - `infra_patterns` (optional) — substrings that mark a result as an infra
     condition (timeout/overload) not a real diagnostic; matched results are
     prefixed `op: …` so adapters drop them. Defaults to
     `["orchestrator timeout", "timed out after"]`. The structural
     MCP `isError` flag is always honored regardless (#346).
   - `idle_timeout` (optional) — daemon shuts itself down after this many seconds idle
     (default 600).
   - `stopOnNewFile` (optional) — `true` to SIGTERM this daemon when a mutating op
     creates a brand-new file matching `match`, so the next validator run cold-starts a
     daemon that has indexed it. Fixes phantom errors on new classes from the warm
     reflection cache (#239), at the cost of one cold reindex on that post-create run.

3. **Discover the tool names** — first time wiring a new MCP server, you don't know
   what tool names it exposes. Two quick options:

   ```bash
   # Run the server directly + ask via the Python SDK (one-off probe)
   pip install mcp
   python3 -c "
   import asyncio, os
   from mcp import ClientSession, StdioServerParameters
   from mcp.client.stdio import stdio_client
   async def m():
       p = StdioServerParameters(command='<binary>', args=[], env={**os.environ})
       async with stdio_client(p) as (r, w):
           async with ClientSession(r, w) as s:
               await s.initialize()
               t = await s.list_tools()
               for tool in t.tools: print(tool.name, '—', tool.description[:80])
   asyncio.run(m())"
   ```

   Or — once the server is wired into `.supertool.json` and the daemon is running —
   read the live `tools/list` via the daemon's socket using the same SDK against
   `socket_path`.

4. **Run** — `./supertool 'resolve:<SYMBOL>:<FILE>'`. First call spawns the daemon
   detached; LSP indexes (cold start ~30s on big repos), then warm. `mcp_status`
   confirms the daemon's running.

### Examples

#### Python (pylsp)

```bash
pip install "python-lsp-server[all]"
```

```json
// .claude/cclsp.json — add to the servers array
{ "extensions": ["py", "pyi"], "command": ["pylsp"], "rootDir": "." }
```

```json
// .supertool.json mcp block
"python-lsp": {
  "cmd": "cclsp",
  "match": "*.{py,pyi}",
  "env": { "CCLSP_CONFIG_PATH": ".claude/cclsp.json" },
  "tools": {
    "resolve": "find_workspace_symbols",
    "refs":    "find_references",
    "diag":    "get_diagnostics",
    "hover":   "get_hover",
    "rename":  "rename_symbol"
  },
  "timeout": 60
}
```

#### TypeScript

```bash
npm install -g typescript typescript-language-server
```

```json
// .claude/cclsp.json
{ "extensions": ["ts", "tsx", "js", "jsx"],
  "command": ["typescript-language-server", "--stdio"], "rootDir": "." }
```

```json
// .supertool.json mcp block
"ts-lsp": {
  "cmd": "cclsp",
  "match": "*.{ts,tsx,js,jsx}",
  "env": { "CCLSP_CONFIG_PATH": ".claude/cclsp.json" },
  "tools": {
    "resolve": "find_workspace_symbols",
    "refs":    "find_references",
    "diag":    "get_diagnostics",
    "hover":   "get_hover",
    "rename":  "rename_symbol"
  },
  "timeout": 60
}
```

#### Custom MCP server (no LSP)

If you have your own MCP server binary that exposes domain-specific tools (e.g. a
GraphQL schema walker, a custom code analyzer), point `cmd` straight at it:

```json
"my-tool": {
  "cmd": "/usr/local/bin/my-mcp-tool --stdio",
  "match": "*.graphql",
  "tools": { "resolve": "schema_lookup", "refs": "schema_references" },
  "timeout": 30
}
```

Same daemon owns it, same UDS protocol, same auto-spawn behavior.

### Pitfalls

- **Tool name mismatch** — if `tools.resolve` points at a name the server doesn't
  expose, MCP returns an error → supertool catches it → falls through to heuristic.
  Silently slower, not wrong. Confirm names via the probe in step 3.
- **Tool semantic mismatch** — names can match but behavior differs. cclsp's
  `find_definition` scans the *given file* for the symbol; `find_workspace_symbols`
  searches the whole index. Use whichever fits the op's intent. For supertool's
  `resolve` (FQN→file), `find_workspace_symbols` is the right pick.
- **Slow first call** — LSPs cold-index on first spawn. Free intelephense has no
  persistent disk index, so the daemon's warm-time-after-first-call is your savings.
  Don't kill the daemon between calls unless you want to pay the cold start again.
- **Warm state can *be* the answer** — a daemon is a process that boots once and is
  reused, so whatever that boot opened is shared by every call it serves. It is what
  makes the daemon fast, and it is the source of every "the warm tool disagrees with
  the cold tool" bug this project has filed: a document cache with no invalidation
  ([#482](https://github.com/Digital-Process-Tools/claude-supertool/issues/482)), an
  autoloader that cannot reload an edited class
  ([#265](https://github.com/Digital-Process-Tools/claude-supertool/issues/265)),
  engine state corrupted by the previous call
  ([#273](https://github.com/Digital-Process-Tools/claude-supertool/issues/273)),
  a test-framework bootstrap whose handles are inherited by every forked child
  ([#345](https://github.com/Digital-Process-Tools/claude-supertool/issues/345)).
  Before trusting a warm result, run the cold tool on the same file once and compare;
  a warm server that silently does *less* work looks exactly like a fast one. Where
  the disagreement cannot be told from a real finding by reading the output, the
  validator must **decline** rather than report — see `warm_unsafe` and "Declining
  instead of guessing" in [`validators.md`](validators.md).

## Tool name reference (cclsp)

cclsp exposes these tools (full list via `cclsp` + `tools/list`):

| Tool | Args | Notes |
|---|---|---|
| `find_definition` | `symbol_name`, `file_path` | Scans the file's symbols for the name — not a workspace-wide FQN search |
| `find_workspace_symbols` | `query` | Workspace-wide name search via LSP `workspace/symbol`. Best fit for FQN→file resolution |
| `find_references` | `symbol_name`, `file_path`, `include_declaration?` | LSP `textDocument/references` |
| `rename_symbol` | `symbol_name`, `file_path`, `new_name`, `dry_run?` | Workspace rename |
| `get_diagnostics` | `file_path` | Per-file LSP diagnostics |
| `get_hover` | `symbol_name`, `file_path` | LSP hover info |
| `restart_server` | — | Restart the LSP backend |

Supertool's MCP call sends `{symbol_name, file_path, query}` together so the same call
works whether the configured tool needs `symbol_name`/`file_path` (cclsp `find_definition`,
`find_references`) or `query` (cclsp `find_workspace_symbols`). Tools ignore unknown args.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `not found` instead of an LSP answer | Daemon didn't start or LSP cold-start exceeded timeout | Increase `timeout` to 120; check `mcp_status`; check `/tmp/supertool-mcp-<hash>.sock.stderr` for cclsp errors |
| Daemon dies repeatedly | LSP server crashed | Check `/tmp/supertool-mcp-<hash>.sock.stderr` (cclsp + LSP stderr is captured here) |
| `resolve` returns cclsp's "No symbols found in <file>" text | Using `find_definition` (file-scoped) instead of `find_workspace_symbols` | Switch the tool mapping for `resolve` |
| First call slow (~30s+) | Intelephense cold-indexing the repo | Expected; subsequent calls hit a warm daemon |
| `AF_UNIX path too long` in tests | macOS UDS path limit (~104 chars) | Use `/tmp/` paths, not pytest `tmp_path` |

## Implementation notes

- **Wire format**: NDJSON (`{json}\n` per message). Same as MCP SDK over stdio. Don't
  use LSP-style `Content-Length` framing — MCP doesn't use it.
- **Concurrency**: daemon serves one client at a time. New client = new bridge. Cclsp
  itself is single-threaded behind one stdio pair, so multi-client multiplexing would
  need request-ID routing in the daemon (not done; not needed for current usage).
- **Process lifecycle**: daemon uses `start_new_session=True` (not a manual double-fork)
  so it survives the spawning shell exiting.
- **Tests**: `tests/fixtures/mock_mcp_server.py` is a UDS NDJSON mock used by
  `tests/test_mcp_{client,routing,workspace}.py`. Each test gets its own socket in
  `/tmp/st-mock-<uuid>.sock`.
