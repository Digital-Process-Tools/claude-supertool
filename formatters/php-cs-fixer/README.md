# php-cs-fixer formatter adapter

Runs [PHP CS Fixer](https://github.com/PHP-CS-Fixer/PHP-CS-Fixer) on a single file and emits SCHEMA.md-compliant JSON with before/after line diff metrics.

## Env vars

| Var                 | Default          | Description                      |
|---------------------|------------------|----------------------------------|
| `PHPCSFIXER_BIN`    | `php-cs-fixer`   | Path to php-cs-fixer binary      |
| `PHPCSFIXER_CONFIG` | _(none)_         | `--config` path (optional)       |

## Example `.supertool.json` snippet

```json
{
  "formatters": {
    "php-cs-fixer": {
      "cmd": "python3 {supertool_dir}/formatters/php-cs-fixer/php-cs-fixer.py {file}",
      "match": "*.php",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "rollback_on_fail": false,
      "timeout": 30,
      "env": {
        "PHPCSFIXER_BIN": "./vendor/bin/php-cs-fixer",
        "PHPCSFIXER_CONFIG": ".php-cs-fixer.php"
      }
    }
  }
}
```

## Output shape

Follows [SCHEMA.md](../SCHEMA.md). On success: `ok=true`, `metrics.lines_added` / `metrics.lines_removed` from the before/after diff. Exit code 0 = no changes, exit code 1 = fixes applied (both treated as success). Exit code ≥16 is an error. On missing binary: `ok=false` with `"PHPCSFIXER_BIN not found"` in the error message.
