# phpmd validator adapter

Runs [PHPMD](https://phpmd.org/) on a single file and emits SCHEMA.md-compliant JSON.

## Env vars

| Var                   | Default                                                        | Description                              |
|-----------------------|----------------------------------------------------------------|------------------------------------------|
| `PHPMD_BIN`           | `phpmd`                                                        | Path to phpmd binary                     |
| `PHPMD_RULESETS`      | _(auto-detect, see below)_                                    | Comma-separated rulesets or rule files. Setting it is an **explicit override** — it disables project auto-detection. When unset, the adapter auto-detects the project CI rulesets, falling back to the built-in `cleancode,codesize,controversial,design,naming,unusedcode` |
| `PHPMD_FORMAT`        | `text`                                                         | Output format (keep `text` for parsing)  |
| `PHPMD_EXCLUDE`       | _(none)_                                                       | Value for `--exclude` flag               |
| `PHPMD_NO_AUTODETECT` | _(none)_                                                       | Set to `1` to skip project-ruleset auto-detection and use the built-in default |

## Project ruleset auto-detection

When `PHPMD_RULESETS` is not set (and `PHPMD_NO_AUTODETECT` is not `1`), the adapter walks up from the file being validated looking for a `gitlab-ci/md/*.xml` directory — the project's CI phpmd ruleset. If found, it runs phpmd with those project XML files **plus** the built-in categories the project does not override (a project XML named `design.xml` supersedes the built-in `design`), mirroring what CI enforces so a local finding matches a CI finding.

The emitted JSON carries a `ruleset_source` field recording which path was taken: `"project"` (auto-detected CI XMLs), `"env"` (explicit `PHPMD_RULESETS`), or `"default"` (built-in fallback).

## Example `.supertool.json` snippet

```json
{
  "validators": {
    "phpmd": {
      "cmd": "python3 {supertool_dir}/validators/phpmd/phpmd.py {file}",
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
