# phpcbf formatter adapter

Runs [PHP Code Beautifier and Fixer](https://github.com/squizlabs/PHP_CodeSniffer) on a single file and emits SCHEMA.md-compliant JSON with before/after line diff metrics.

## Env vars

| Var               | Default   | Description                                  |
|-------------------|-----------|----------------------------------------------|
| `PHPCBF_BIN`      | `phpcbf`  | Path to phpcbf binary                        |
| `PHPCBF_STANDARD` | `PSR12`   | Coding standard (`PSR12`, `PSR2`, or custom) |

## Example `.supertool.json` snippet

```json
{
  "formatters": {
    "phpcbf": {
      "cmd": "python3 {supertool_dir}/formatters/phpcbf/phpcbf.py {file}",
      "match": "*.php",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "rollback_on_fail": false,
      "timeout": 30,
      "env": {
        "PHPCBF_BIN": "./vendor/bin/phpcbf",
        "PHPCBF_STANDARD": "PSR12"
      }
    }
  }
}
```

## Output shape

Follows [SCHEMA.md](../SCHEMA.md). On success: `ok=true`, `metrics.lines_added` / `metrics.lines_removed` from the before/after diff. Exit code 1 from phpcbf (fixes applied) is treated as success. Exit code ≥2 is an error. On missing binary: `ok=false` with `"PHPCBF_BIN not found"` in the error message.
