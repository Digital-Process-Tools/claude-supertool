# phpmd validator adapter

Runs [PHPMD](https://phpmd.org/) on a single file and emits SCHEMA.md-compliant JSON.

## Env vars

| Var              | Default                                                        | Description                              |
|------------------|----------------------------------------------------------------|------------------------------------------|
| `PHPMD_BIN`      | `phpmd`                                                        | Path to phpmd binary                     |
| `PHPMD_RULESETS` | `cleancode,codesize,controversial,design,naming,unusedcode`    | Comma-separated rulesets or rule files   |
| `PHPMD_FORMAT`   | `text`                                                         | Output format (keep `text` for parsing)  |
| `PHPMD_EXCLUDE`  | _(none)_                                                       | Value for `--exclude` flag               |

## Example `.supertool.json` snippet

```json
{
  "validators": {
    "phpmd": {
      "cmd": "bash {supertool_dir}/validators/phpmd/phpmd.sh {file}",
      "match": "*.php",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "rollback_on_fail": false,
      "timeout": 30,
      "env": {
        "PHPMD_BIN": "./vendor/bin/phpmd",
        "PHPMD_RULESETS": "cleancode,codesize,unusedcode",
        "PHPMD_EXCLUDE": "src/Generated/"
      }
    }
  }
}
```

## Output shape

Follows [SCHEMA.md](../SCHEMA.md). Each error carries `severity: "warning"`, `code` set to the rule name, and `source_context` (5-line window centered on the error line).
