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
| `ok`          | bool             | yes      | Pass/fail. Validator rolls back on `false` if op marked rollback.     |
| `count`       | int              | yes      | Issue count. Used for before/after diff arithmetic.                   |
| `errors`      | array of objects | yes      | `[]` when ok. Each: `{line, col, severity, code, msg}`.               |
| `duration_ms` | int              | yes      | Wall time. For perf tuning.                                           |
| `metrics`     | object           | no       | Tool-specific counters (`tests_total`, `tests_passed`, etc.). Numeric values. Used by renderer for before/after diff on metric keys even when `count` is unchanged. |

| `diff`        | string           | no       | Unified diff produced by the tool (e.g. rector). Rendered as a fenced block below all errors in verbose mode. Ignored in default mode. |

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

## Adding a validator

1. Write `.claude/scripts/validators/<tool>.sh`
2. Register in `.supertool.json` under `validators` block
3. Done. Core picks it up automatically.
