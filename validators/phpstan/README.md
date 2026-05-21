# phpstan validator adapter

Runs [PHPStan](https://phpstan.org/) on a single file and emits SCHEMA.md-compliant JSON.

## Env vars

| Var              | Default    | Description                                      |
|------------------|------------|--------------------------------------------------|
| `PHPSTAN_BIN`    | `phpstan`  | Path to phpstan binary                           |
| `PHPSTAN_CONFIG` | _(none)_   | Path to neon config file (adds `-c FILE`)        |
| `PHPSTAN_MEMORY` | `1G`       | PHP `memory_limit` passed via `-d`               |
| `PHPSTAN_LEVEL`  | _(none)_   | Analysis level (adds `--level N`)                |

## Example `.supertool.json` snippet

```json
{
  "validators": {
    "phpstan": {
      "cmd": "python3 {supertool_dir}/validators/phpstan/phpstan.py {file}",
      "match": "*.php",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "rollback_on_fail": false,
      "timeout": 60,
      "env": {
        "PHPSTAN_BIN": "./vendor/bin/phpstan",
        "PHPSTAN_CONFIG": "phpstan.neon",
        "PHPSTAN_MEMORY": "512M",
        "PHPSTAN_LEVEL": "8"
      }
    }
  }
}
```

## Output shape

Follows [SCHEMA.md](../SCHEMA.md). Errors include `source_context` (5-line window centered on the error line).
