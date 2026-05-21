# MCP Integration Spec — Hidden Tooling Backends

**Status:** ✅ v1 shipped · 2026-05-21
**Sub-PRs:** [#126](https://github.com/Digital-Process-Tools/claude-supertool/pull/126) client primitives · [#127](https://github.com/Digital-Process-Tools/claude-supertool/pull/127) config + routing + `op_resolve` · [#128](https://github.com/Digital-Process-Tools/claude-supertool/pull/128) workspace References + Symbols

## v2 Roadmap (deferred from v1)

| Item | Spec ref | Why deferred |
|---|---|---|
| Cache layer (`(server, tool, file_sha, args)` key) | §7 | Validator-cache framework needs a small extension; ship after v1 lands so we can benchmark first |
| Verbose-mode fallback logging | §8 | UX nicety — note in verbose output when MCP miss → heuristic |
| Crash recovery + backoff | §5 | Single re-spawn with 500ms backoff; TODO marker placed in `_recv` |
| Env var expansion (`$INTELEPHENSE_LICENSE`) | §3 | Config-time interpolation for secrets |
| LLM-visible MCP wrapper op (`mcp:<server>:<tool>:<args>`) | §10 | Out of v1 scope — call any MCP tool directly via supertool |
| `hover` / `rename` / `implementers` / `callers` ops | §10 | First-class workspace ops backed by MCP when present |
| Config validation (malformed `tools` / `env`) | — | Silent today; should error at load time |

---

## 1. Motivation

LLMs working with supertool today face two surfaces: supertool ops (`resolve`, `refs`, `read`, `grep`…) and MCP tools registered directly in Claude Code (`mcp__php_lsp__definition`, `mcp__playwright__click`…). Mental context switches, inconsistent naming, and no shared caching or fallback.

The token cost is the sharper problem. Each MCP server registered in Claude Code injects its full tool schema into every conversation. With 5 tooling servers at ~10 tools each and ~400 tokens per tool definition, that's **~20K tokens permanently consumed** before the first user message — and those tools may never be called.

Existing workspace ops (`resolve`, `refs`, `symbols`) are heuristic: regex over source files. They're fast and offline, but imprecise — they miss overloaded names, cross-file type resolution, and dynamic dispatch. LSP-backed equivalents would be exact.

**Goal:** Route tooling MCP servers through supertool as hidden backends. The LLM calls `resolve:Foo:File.php` exactly as today. Supertool routes to the LSP, returns the result. Zero tokens added to the LLM's tool catalog.

---

## 2. Visibility Model — Hidden vs Visible MCPs

Not all MCP servers should be hidden. The distinction:

| Type | Recommended model | Examples |
|------|------------------|---------|
| **Tooling backends** — read-only, deterministic, workspace-scoped | **Hidden** — supertool owns lifecycle, exposes as ops | `php-lsp`, `pyright`, `tsserver`, `rust-analyzer` |
| **Action backends** — side-effects, persistent state, user-facing auth | **Visible** — registered in Claude Code, LLM calls directly | `playwright`, `gmail`, `slack`, `google-drive` |

Decision rule: if the server reads workspace state and returns structured data → hidden. If it mutates external state or requires user authentication → visible.

Hidden servers never appear in the LLM's tool schema. They are supertool plumbing.

---

## 3. Config Schema

Add an `mcp` block to `.supertool.json`:

```json
{
  "mcp": {
    "php-lsp": {
      "cmd": "claude-mcp-php-lsp",
      "match": "*.php",
      "env": {
        "INTELEPHENSE_LICENSE": "your-key-here"
      },
      "tools": {
        "resolve": "definition",
        "refs": "references",
        "hover": "hover",
        "rename": "rename",
        "implementers": "implementers",
        "callers": "callers"
      },
      "timeout": 30
    },
    "pyright": {
      "cmd": "pyright-mcp",
      "match": "*.py",
      "tools": {
        "resolve": "definition",
        "refs": "references",
        "hover": "hover"
      },
      "timeout": 20
    },
    "tsserver": {
      "cmd": "typescript-mcp",
      "match": "*.{ts,tsx}",
      "tools": {
        "resolve": "definition",
        "refs": "findReferences",
        "hover": "quickInfo",
        "rename": "rename"
      },
      "timeout": 25
    }
  }
}
```

**Field reference:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cmd` | string | yes | Command to spawn the MCP server |
| `match` | glob | recommended | File extension glob — same `_match_glob` used by validators/formatters |
| `env` | object | no | Per-server env vars injected at spawn time |
| `tools` | object | yes | Maps supertool canonical op names → MCP server tool names |
| `timeout` | int (seconds) | no | Per-call timeout; defaults to 30 |

`match` is recommended but not required — see [Open Questions](#12-open-questions).

**Canonical op names** (supertool side): `resolve`, `refs`, `hover`, `rename`, `implementers`, `callers`, `definition` (alias for `resolve`).

---

## 4. MCP Wire Protocol

Supertool communicates with hidden MCP servers over **JSON-RPC 2.0 via stdio** — the standard MCP transport. Spec: https://modelcontextprotocol.io/

Messages supertool needs to implement (minimal client):

| Message | Direction | Purpose |
|---------|-----------|---------|
| `initialize` | supertool → server | Handshake on spawn; declares client capabilities |
| `initialized` | server → supertool | Confirms server is ready |
| `tools/list` | supertool → server | Validates config tool names exist; called once after init |
| `tools/call` | supertool → server | Invokes a tool with args; main request/response |
| `shutdown` | supertool → server | Graceful termination on exit |

**Not implemented in v1:** notifications, prompts, resources, sampling, progress. Tooling backends are unary request/response — nothing else is needed.

Each `tools/call` request shape:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "definition",
    "arguments": {
      "file": "/abs/path/to/File.php",
      "symbol": "Foo"
    }
  }
}
```

Response is server-specific; supertool extracts the `content` array and formats for LLM consumption.

---

## 5. Lifecycle

**Spawn:** Lazy. Server starts on the first op that routes to it. No pre-warming at supertool startup.

**Lifetime:** One server process per match group, kept alive for the supertool session. The process stays in supertool's process tree. No IPC socket — stdio only.

**Shutdown:** Supertool sends `shutdown` then closes stdin on exit. An `atexit` handler covers normal exits. SIGTERM/SIGINT propagates to child processes via process group.

**Crash recovery:** If a server process dies mid-session, supertool:
1. Detects the closed pipe
2. Re-spawns once after a 500ms backoff
3. Retries the failed op
4. If the retry fails, falls back to heuristic (see [Fallback](#8-fallback-strategy)) and logs a one-line warning in verbose mode

No crash loop — one re-spawn attempt maximum per server per session.

---

## 6. Tool Routing

Dispatch flow for `resolve:Foo:File.php`:

```
1. Detect target file extension → ".php"
2. Scan mcp config: find first server where _match_glob(match, "*.php") is true → "php-lsp"
3. Look up mcp["php-lsp"].tools["resolve"] → "definition"
4. Spawn php-lsp if not running (lazy init, handshake)
5. Call tools/call {name: "definition", arguments: {file: "/abs/File.php", symbol: "Foo"}}
6. Format response → return to caller
7. On any failure → fall back to heuristic resolve
```

Same dispatch for `refs`, `hover`, `rename`, `implementers`, `callers`.

If no MCP server matches the file extension, supertool proceeds directly to heuristic — no error, no warning. The MCP layer is purely additive.

Multiple servers with overlapping `match` patterns: first match wins (config order). Document this explicitly in the config reference — order matters.

---

## 7. Caching

LSP responses are cached using the existing validator cache framework:

**Cache key:** `(file_sha256, abs_file_path, tool_name, serialized_args)`

**Invalidation:** File SHA changes on edit → cache miss → fresh LSP call. This is automatic if supertool tracks file SHA after `edit`/`replace_lines` ops (which it already does for validator post-edit runs).

**TTL:** File-change-based, same as validators. No wall-clock expiry.

**Workspace-level cache warming** (future): on `tools/list` response, supertool could pre-resolve the current file. Deferred to v2.

---

## 8. Fallback Strategy

Three tiers per op, evaluated in order:

| Tier | Condition | Behavior |
|------|-----------|---------|
| **1. MCP server** | Configured, healthy, match found | Full LSP response |
| **2. Heuristic** | MCP not configured, or server failed | Current supertool implementation (`_grep_recursive`, regex-based `resolve`, etc.) |
| **3. Error** | Both fail with hard error | Op returns error message |

Failures at tier 1 are silent in normal mode. In verbose mode (`-v`), a one-line note appears: `[mcp] php-lsp unavailable, falling back to heuristic`. The LLM sees the heuristic result and nothing else.

This preserves backward compatibility: supertool without any MCP config behaves identically to today.

---

## 9. Token Impact

| Scenario | Tokens consumed per conversation |
|----------|----------------------------------|
| 5 MCP servers registered visibly in Claude Code | ~20,000 tokens (5 × 10 tools × ~400 tokens) |
| Same 5 servers as hidden supertool backends | **0 tokens** — not in tool catalog |
| Per-call overhead (hidden vs direct) | ~5ms extra JSON-RPC hop; negligible |

The token saving is permanent and per-conversation. A 20K token reduction in every session is equivalent to recovering ~15% of a 128K context window before the first message.

Visible action MCPs (playwright, gmail) remain registered in Claude Code as today — their schema cost is intentional because the LLM needs to call them directly.

---

## 10. v1 Scope

**In scope for v1 — ✅ all shipped (PRs #126, #127, #128):**
- ✅ Config schema (`mcp` block in `.supertool.json`) — parsed; explicit validation deferred to v2
- ✅ Single MCP server per extension match (first match wins, dict insertion order)
- ✅ Synchronous `tools/call` only (no streaming)
- ✅ Lazy spawn + session-lifetime server process
- ✅ Graceful shutdown via `atexit` + explicit `shutdown` message
- ✅ Heuristic fallback on any failure
- ✅ Manual config only (no auto-discovery via `tools/list`)
- ✅ Integration into `op_resolve` + workspace References + Symbols sections
- ✅ Unit tests for MCP client primitives + routing + workspace integration (35+ tests)

**Out of scope for v1:**
- Persistent server lifetime across supertool sessions
- Streaming MCP responses (multi-chunk tooling results)
- Multi-server load balancing or round-robin
- LLM-visible MCP wrapping (`mcp:<server>:<tool>:<args>` op form) — defer to v2
- Auto-discovery: scanning `tools/list` to build the op→tool mapping automatically
- Server health checks at startup (lazy spawn handles this implicitly)

---

## 11. Getting Started — PHP via Intelephense

This section walks through wiring PHP LSP support into supertool. Steps use DVSI as the example repo but the pattern is language-agnostic — repeat for Python (`pyright --stdio`), TypeScript (`typescript-language-server --stdio`), etc.

### Step 1. Install intelephense globally

```bash
npm install -g intelephense
```

Verify: `intelephense --stdio` (should hang waiting for LSP input — kill with Ctrl-C).

Optional: paid license unlocks code actions and workspace-wide rename. Free tier covers go-to-def, refs, and hover. License: https://intelephense.com/

### Step 2. Get an MCP bridge for LSP

intelephense speaks LSP; supertool speaks MCP. A bridge is needed. Two options:

- **`cclsp`** ([github.com/ktnyt/cclsp](https://github.com/ktnyt/cclsp)) — generic MCP↔LSP bridge that auto-routes. One install, many languages.
  ```bash
  npm install -g cclsp
  ```
- **Custom thin wrapper** — a small Python script that spawns intelephense and translates MCP `tools/call` ↔ LSP `textDocument/definition` etc. Reasonable if cclsp's tool naming doesn't match supertool's canonical ops.

For DVSI, start with `cclsp`. It exposes tools like `definition`, `references`, `hover` which align with supertool's canonical op names.

### Step 3. Wire into `.supertool.json`

```json
{
  "mcp": {
    "php-lsp": {
      "cmd": "cclsp --lsp 'intelephense --stdio'",
      "match": "*.php",
      "env": {
        "INTELEPHENSE_LICENSE": "$INTELEPHENSE_LICENSE"
      },
      "tools": {
        "resolve": "definition",
        "refs": "references",
        "hover": "hover"
      },
      "timeout": 30
    }
  }
}
```

For DVSI, add a `.class.php` glob if intelephense doesn't pick up that suffix automatically — match the validator's pattern: `"match": "*.{php,class.php}"`.

> ⚠️ The `$INTELEPHENSE_LICENSE` env var reference assumes the var is exported in your shell. Env var expansion in `.supertool.json` is on the v2 roadmap (see [Open Questions §12](#12-open-questions)). For now, paste the literal license value or export the env var before running supertool.

### Step 4. Smoke test

From the DVSI repo root:

```bash
./supertool 'resolve:SiCore\\Annotations\\SiModuleDescription:Dvsi/dvsi-private/src2/SiOAuthPennylane/SiOAuthPennylaneModule.class.php'
```

Expected: returns the absolute path to `SiModuleDescription.class.php` via intelephense (precise — includes autoload resolution).

Compare to without MCP (rename the `mcp` block temporarily): the heuristic glob should find the same file, but slower and less reliable on overloaded names.

### Step 5. Wire workspace

```bash
./supertool 'workspace:Dvsi/dvsi-private/src2/SiOAuthPennylane/SiOAuthPennylaneModule.class.php'
```

The `## Imports`, `## References`, and `## Symbols` sections now consult intelephense. Compare output to the heuristic version: imports resolve faster, references include cross-namespace usage, symbols include inherited methods.

### Step 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `MCPServer 'php-lsp' marked dead` after first call | intelephense crashed during indexing | Check intelephense logs (`~/.intelephense/`). Re-install. |
| `resolve` always returns `not found` | cclsp tool name mismatch | Run `cclsp --list-tools` and align the `tools` mapping in `.supertool.json` |
| First call is very slow (>10s) | intelephense indexing on cold start | Expected. Subsequent calls are fast. Increase `timeout` to 60 if you hit the limit. |
| "INTELEPHENSE_LICENSE invalid" | License typo or expired | Check license at intelephense.com. Free tier works for go-to-def + refs. |
| Heuristic still firing | `match` glob doesn't cover `.class.php` | Add explicit glob: `"match": "*.{php,class.php}"` |

### Step 7. What's next

After PHP works, repeat for Python (`pyright --stdio`), TypeScript (`typescript-language-server --stdio`), Rust (`rust-analyzer`), etc. Same shape: install LSP server → wrap via cclsp → drop into `.supertool.json`.

---

## 12. Open Questions

Design choices that need team input before implementation starts:

1. **`match` required or optional?**
   If `match` is omitted, the server has no automatic routing — it could only be called via explicit naming (`mcp:php-lsp:hover:...`). Is on-demand (no match) a valid use case, or should `match` be required for hidden servers?

2. **Config-declared tool mapping vs auto-discovery?**
   The current spec requires explicit `tools` mapping in config. Alternative: call `tools/list` on first spawn and auto-map by canonical name. Auto-discovery is more ergonomic but adds startup latency and requires canonical name alignment across LSP implementations. Tradeoff?

3. **License key / secrets handling?**
   `env` in `.supertool.json` is convenient but the file is typically committed. Options: (a) env var references (`"INTELEPHENSE_LICENSE": "$INTELEPHENSE_LICENSE"` expanded at spawn time), (b) separate `.supertool.secrets.json` (gitignored), (c) system keychain integration. What's the right security boundary?

4. **Max concurrent server processes per session?**
   With 3 language servers active, that's 3 long-lived processes. Is there a cap? Should supertool enforce a `maxServers` budget, or leave resource management to the user?

5. **Error surfacing granularity?**
   Current spec: failures are silent in normal mode, one-line note in verbose. Should the LLM ever be informed that it's getting heuristic results instead of LSP results? Could affect trust in `resolve` output.

---

## 13. PR Plan

Three sub-PRs for v1, each reviewable independently:

### PR 1 — MCP Client Primitives

**Scope:** Everything below the routing layer.
- `supertool/mcp_client.py` (or equivalent): spawn, stdio transport, JSON-RPC 2.0 encode/decode
- `initialize` / `initialized` handshake
- `tools/list` call with response parsing
- `tools/call` with timeout and error handling
- `shutdown` with `atexit` registration
- Crash detection + single re-spawn with backoff
- Unit tests: mock server subprocess, test all message types, test crash recovery

No config integration, no routing. The client is a standalone, testable module.

### PR 2 — Config Schema + Extension Routing + `op_resolve` Integration

**Scope:** Wiring the client into supertool's op dispatch.
- Parse and validate `mcp` block in `.supertool.json`
- `_match_glob` routing: file extension → server
- Op→tool name resolution via config `tools` mapping
- Integration into `op_resolve`: MCP call first, heuristic fallback
- Cache layer: file SHA keying on LSP responses
- Verbose-mode logging for fallback events
- Integration tests: `resolve:Foo:File.php` end-to-end with a test MCP server

### PR 3 — Roll Out to Remaining Workspace Ops

**Scope:** Extend MCP routing to all workspace ops.
- `op_refs` → `refs` tool
- `op_symbols` → `symbols` tool (if server supports it)
- `op_hover` → `hover` tool
- `op_rename` → `rename` tool (write-path; needs extra validation before PR 3)
- Update op reference docs with MCP-backed behavior notes
- Update `.supertool.json` config reference with full `mcp` block documentation

---

*Written by Max, 2026-05-21. Design review before PR 1 begins.*
