# Validator Output Schema

Universal JSON. Every adapter emits this shape. Validator core never parses tool-specific output.

## Schema

```json
{
  "tool": "phplint",
  "file": "src/Foo.php",
  "ok": true,
  "count": 0,
  "errors": [],
  "duration_ms": 42
}
```

## Fields

| Field         | Type             | Required | Notes                                                                 |
|---------------|------------------|----------|-----------------------------------------------------------------------|
| `tool`        | string           | yes      | Adapter id (`phplint`, `phpstan`, `phpunit`, `prettier`, ...)         |
| `file`        | string           | yes      | Path validated. Repo-relative.                                        |
| `ok`          | bool             | yes*     | Pass/fail. Validator rolls back on `false` if op marked rollback. *Absent on a `skipped` result. |
| `count`       | int              | yes*     | Issue count. Used for before/after diff arithmetic. *Absent on a `skipped` result. |
| `errors`      | array of objects | yes*     | `[]` when ok. Each: `{line, col, severity, code, msg}`. *Absent on a `skipped` result. |
| `duration_ms` | int              | yes      | Wall time. For perf tuning.                                           |
| `metrics`     | object           | no       | Tool-specific counters (`tests_total`, `tests_passed`, etc.). Numeric values. Used by renderer for before/after diff on metric keys even when `count` is unchanged. |

| `diff`        | string           | no       | Unified diff produced by the tool (e.g. rector). Rendered as a fenced block below all errors in verbose mode. Ignored in default mode. |
| `skipped`     | string           | no       | Reason the validator declined to analyse the file (scope allowlist, tool absent, ...). Its presence — not its value — marks the result as a third state. |

### Skipped: the third state

`ok` alone has two values and the world has three: clean, broken, and **never looked at**. A validator that refused to run has produced no information about the file, so folding that into either `ok` value is a lie in one direction or the other.

An adapter reports it by emitting `"skipped": "<reason>"` and **omitting `ok`, `count` and `errors` entirely** (#515). A receipt carrying `ok: true` reads as a pass to anything keying off `ok`, which is the mistake the third state exists to end; leaving the verdict keys out makes a skip structurally impossible to misread as one. `tool`, `file` and `duration_ms` stay — they describe the attempt, not a verdict.

Consumers must branch on the presence of `skipped` before reading any verdict key. Every core consumer already does, and has to: the reason string exists only on a skip. Use `validators/common/refusal.py:skipped()` rather than building the dict by hand. The core then guarantees:

- the row renders as `skipped (<reason>)`, never `0 → 1 (+1) ✗`;
- the result is excluded from the before/after delta;
- the result is **not cached** (a skip is config-derived; the cache key is a content hash);
- the result **never triggers rollback**, whatever `rollback_on_fail` says.

### `adapter`: the reserved code for "no verdict was obtained"

`code: "adapter"` is reserved across every adapter for a failure of the adapter or its tool rather than a finding about the file: a binary that is absent, a timeout, output that would not parse, a tool that exited non-zero without saying anything about the file. It stays a real error — `ok: false`, `count: 1` — because the process ran and something is broken that someone has to fix; a fault routed to `skipped` is a validator quietly reporting clean.

Two consequences a consumer can rely on:

- **The message names what failed**, including the exit code and the tool's raw output when there is any, and says so explicitly when there is none.
- **The result is never cached.** `adapter` is in the core's `_NONDETERMINISTIC_ERROR_CODES`: a verdict that was never obtained is not a function of the file's content, and the cache key is a content hash, so caching one replays it until the file changes.
- **The result never triggers rollback, whatever `rollback_on_fail` says** — the same guarantee `skipped` carries above. `count: 1` is the channel this schema gives an absence, not a measurement of the file, so the core never subtracts it from a baseline in either direction and never reverts an edit over it (#969). The core's own timeout (`code: "orchestrator"`) is treated identically.

Emit it whenever the tool's output does not confirm it looked at the file. Where the boundary is genuinely unclear, prefer the finding: an `adapter` result is fully legible to a reader, while a real finding relabelled `adapter` sends them to the wrong place. See `docs/validators.md`, "Declining instead of guessing".

**`no_verdict` is core-internal — an adapter must never set it.** The core adds it to the `skipped` result *it* synthesises when an adapter produced nothing it could read (no output, non-JSON, a crash, a reply with neither `ok` nor `skipped`). It is the one signal separating "the core watched this adapter break down" from "a healthy adapter declined on its own terms", and under `$SUPERTOOL_REQUIRE_VALIDATORS` the first exits 1 while the second does not (#975). An adapter that sets it would be asking for its own scope decisions to be read as a broken gate. An adapter that is genuinely unable to run says so through `refusal.required()` and the `adapter` code above.

**What confirms it looked at the file is per-tool, and it is a located diagnostic, not the exit code** (#753). An adapter picks the marker out of its own tool's output format — `file:LINE:` (xmllint, ruby), `: line N:` (bash), a resolved-path header (node), `path:line:col:` (gofmt, cargo short format), `on <file> line N` (terraform). Two rules generalise from that sweep:

- **Never take a location from the output at large.** Search anchored to the diagnostic, and where the tool prints the path it resolved, check it against the file. `node --check` reports a missing module by printing `node:internal/modules/cjs/loader:1386` first, and a bare `:(\d+)` search turned that into the file's syntax-error line.
- **A finding you cannot place reports `line: null`.** Reclassifying and inventing a location are separate fixes; keeping a finding does not license borrowing a number for it.

Where a tool documents distinct exit codes, read them — `terraform fmt -check` returns `3` for "needs formatting" and `2` for "I failed" — but the located diagnostic is still the marker, because an exit code alone cannot say *which* file it is about.

### Error object

| Field            | Type             | Required | Notes                                              |
|------------------|------------------|----------|----------------------------------------------------|
| `line`           | int \| null      | yes      | 1-indexed. `null` if tool gives no location.       |
| `col`            | int \| null      | yes      | 1-indexed. `null` if not provided.                 |
| `severity`       | string           | yes      | `error` \| `warning` \| `info`                     |
| `code`           | string \| null   | yes      | Rule id (`missingType`, `PSR12.Files...`). Nullable. |
| `msg`            | string           | yes      | Human message. Single line preferred.              |
| `source_context` | array of strings | no       | Source lines near the error. The line containing the error uses `→` as separator; surrounding lines use `:`. Example: `["40:     return foo;", "41: ", "42→     bar();", "43: }", "44: "]`. Rendered indented under the error in verbose mode. Ignored in default mode. |

## Contract

- Adapter **always exits 0** if it produced JSON (even on tool failure). Core reads `.ok`.
- Adapter exits non-zero only on infrastructure failure (tool missing, file unreadable). Core treats as `{ok: false, errors: [{msg: "adapter failed"}]}`.
- Adapter writes **one JSON object on stdout**. Nothing else. Logs go to stderr.
- Input: one arg = file path.

## Spec fields (`.supertool.json` validators/formatters entries)

| Field             | Type             | Required | Notes                                                                                     |
|-------------------|------------------|----------|-------------------------------------------------------------------------------------------|
| `cmd`             | string           | yes      | Shell command. `{file}` and `{supertool_dir}` are substituted before execution.           |
| `hooks_into`      | array of strings | yes      | Ops that trigger this validator automatically (`edit`, `paste`, `vim`, ...).              |
| `match`           | string           | no       | Glob pattern to filter by filename (e.g. `*.php`). Matches all files when absent.        |
| `exclude`         | string or list   | no       | Glob (or list of globs) to skip even when `match` matches (e.g. `*tests/*`). Skip if any matches. |
| `timeout`         | int              | no       | Seconds before the subprocess is killed. Default 60 (validators), 30 (formatters).       |
| `rollback_on_fail`| bool             | no       | Revert the file if the validator reports a regression. Default false.                     |
| `opt_in`          | bool             | no       | When true, validator only runs when explicitly requested (not on every hook).             |
| `resolve`         | string           | no       | Shell command that maps the edited file to the file the adapter should receive.           |
| `env`             | object           | no       | Extra environment variables merged into the subprocess env (`os.environ \| spec.env`). Values are coerced to strings. Example: `{"PHPSTAN_LEVEL": "8", "PHPSTAN_CONFIG": "phpstan.neon"}`. |
| `warm_unsafe`     | string or list   | no       | Regex (or list of regexes) matched against the **resolved** target's content. A hit makes the validator return `skipped` instead of running the adapter — for targets whose verdict this validator's warm process cannot be trusted to produce (see `docs/validators.md`, "Declining instead of guessing"). Never a ✗, never a rollback, never cached. Unreadable targets and patterns that do not compile are ignored, so a config typo cannot silently mute a validator. Example: `["extends\\\\s+SiControllerTestCase"]`. |
| `engine_glitches` | list of strings  | no       | Adapter-specific (rector-mcp). Case-sensitive substrings identifying non-deterministic warm-daemon engine glitches (PHP fatals / engine-state corruption — NOT findings about the file) that the adapter drops at the source so they never surface as a red or reach the cache. The adapter reads this prop straight from `.supertool.json`; built-in defaults apply when absent. Example: `["System error:", "toMutatingScope() on null"]`. |
| `tier`            | string           | no       | `"fast"` (default) or `"slow"`. `fast` runs inline per-op with per-op rollback semantics. `slow` defers to end-of-call, deduped by `(validator, path)` — runs once per unique pair regardless of how many ops touched the file. Failures reported under `[validators-deferred]` header; no auto-rollback. Use `slow` for heavy validators (phpstan, phpunit, rector) to avoid re-running on every edit in a multi-op batch. |

## Adding a validator

1. Write `validators/<tool>/<tool>.sh` (or `.py`)
2. Register in `.supertool.json` under `validators` block
3. Done. Core picks it up automatically.
