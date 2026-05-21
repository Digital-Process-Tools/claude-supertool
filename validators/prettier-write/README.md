# prettier-write formatter adapter

Runs [Prettier](https://prettier.io/) with `--write` on a single file and emits SCHEMA.md-compliant JSON with before/after line diff metrics.

## Env vars

| Var                    | Default    | Description                          |
|------------------------|------------|--------------------------------------|
| `PRETTIER_BIN`         | `prettier` | Path to prettier binary              |
| `PRETTIER_CONFIG`      | _(none)_   | `--config` path (optional)           |
| `PRETTIER_IGNORE_PATH` | _(none)_   | `--ignore-path` path (optional)      |

## Example `.supertool.json` snippet

```json
{
  "formatters": {
    "prettier": {
      "cmd": "python3 {supertool_dir}/validators/prettier-write/prettier-write.py {file}",
      "match": "*.{xml,scss,css,js,json,yml,yaml,md}",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "rollback_on_fail": false,
      "timeout": 30,
      "env": {
        "PRETTIER_BIN": "./node_modules/.bin/prettier",
        "PRETTIER_CONFIG": ".prettierrc"
      }
    }
  }
}
```

## Output shape

Follows [SCHEMA.md](../SCHEMA.md). On success: `ok=true`, `metrics.lines_added` / `metrics.lines_removed` from the before/after diff. On missing binary: `ok=false` with `"PRETTIER_BIN not found"` in the error message.
