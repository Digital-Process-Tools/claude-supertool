# prettier-check validator adapter

Runs [`prettier --check`](https://prettier.io/docs/en/cli.html#--check) on a single file and emits SCHEMA.md-compliant JSON.

Three states, not two. `ok=true` if the file is already formatted; `ok=false` with a single formatting error if prettier would rewrite it; and `skipped` — no verdict at all — when prettier exited 0 without opening the file, which is what a `.prettierignore` (or `--ignore-path`) match looks like. That last one is decided by a second call, `prettier --file-info FILE`, carrying the same `--config`/`--ignore-path` flags as the check; a probe that cannot answer is also `skipped`, never `ok` ([#1601](https://github.com/Digital-Process-Tools/claude-supertool/issues/1601)).

## Env vars

| Var                   | Default     | Description                                           |
|-----------------------|-------------|-------------------------------------------------------|
| `PRETTIER_BIN`        | `prettier`  | Path to prettier binary                               |
| `PRETTIER_CONFIG`     | _(none)_    | Path to prettier config file (adds `--config FILE`)   |
| `PRETTIER_IGNORE_PATH`| _(none)_    | Path to ignore file (adds `--ignore-path FILE`)       |

## Example `.supertool.json` snippet

```json
{
  "validators": {
    "prettier-check": {
      "cmd": "python3 {supertool_dir}/validators/prettier-check/prettier-check.py {file}",
      "match": "*.{xml,scss,css,js,json,yml,yaml,md}",
      "hooks_into": [],
      "rollback_on_fail": false,
      "timeout": 15,
      "env": {
        "PRETTIER_BIN": "./node_modules/.bin/prettier",
        "PRETTIER_CONFIG": ".prettierrc"
      }
    }
  }
}
```

`hooks_into: []` — manual-only. The `prettier` formatter (`prettier --write`) already fires automatically after every edit; this check is opt-in via `validate:FILE:prettier-check`.

## DVSI snippet

```json
"prettier-check": {
  "cmd": "python3 {supertool_dir}/validators/prettier-check/prettier-check.py {file}",
  "match": "*.{xml,scss,css,js,json,yml,yaml,md}",
  "hooks_into": [],
  "rollback_on_fail": false,
  "timeout": 15,
  "env": {
    "PRETTIER_BIN": "prettier",
    "PRETTIER_CONFIG": ".prettierrc"
  }
}
```

## Output shape

Follows [SCHEMA.md](../SCHEMA.md). No `source_context` — prettier's "needs formatting" is whole-file, not line-specific.
