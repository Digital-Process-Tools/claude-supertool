# psr validator adapter

Runs [PHP CodeSniffer](https://github.com/squizlabs/PHP_CodeSniffer) on a single file and emits SCHEMA.md-compliant JSON.

## Env vars

| Var              | Default   | Description                                      |
|------------------|-----------|--------------------------------------------------|
| `PSR_BIN`        | `phpcs`   | Path to phpcs binary                             |
| `PSR_STANDARD`   | `PSR12`   | Coding standard (`PSR12`, `PSR2`, or custom)     |
| `PSR_EXCLUDE`    | _(none)_  | Comma-separated paths to ignore                  |
| `PSR_SEVERITY`   | `9`       | Warning severity threshold (0–10)                |
| `PSR_EXTENSIONS` | `php`     | Comma-separated file extensions to check         |

## Example `.supertool.json` snippet

```json
{
  "validators": {
    "psr": {
      "cmd": "python3 {supertool_dir}/validators/psr/psr.py {file}",
      "match": "*.php",
      "hooks_into": [],
      "rollback_on_fail": false,
      "timeout": 30,
      "env": {
        "PSR_BIN": "./vendor/bin/phpcs",
        "PSR_STANDARD": "PSR12",
        "PSR_SEVERITY": "9",
        "PSR_EXCLUDE": "src/Generated/"
      }
    }
  }
}
```

`hooks_into: []` keeps this manual-only — run explicitly via `./supertool 'validate:src/Foo.php:psr'`.

## Output shape

Follows [SCHEMA.md](../SCHEMA.md). Each error carries `severity` (`"error"` or `"warning"`), `code` set to the phpcs source identifier (e.g. `PSR12.Files.FileHeader.SpacingAfterBlock`), `col` for the column number, and `source_context` (5-line window centered on the error line).
