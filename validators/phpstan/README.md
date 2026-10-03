# phpstan validator adapter

Runs [PHPStan](https://phpstan.org/) on a single file and emits SCHEMA.md-compliant JSON.

## Env vars

| Var              | Default    | Description                                      |
|------------------|------------|--------------------------------------------------|
| `PHPSTAN_BIN`    | `phpstan`  | Path to phpstan binary                           |
| `PHPSTAN_CONFIG` | _(none)_   | Path to neon config file (adds `-c FILE`)        |
| `PHPSTAN_MEMORY` | `1G`       | PHP `memory_limit` passed via `-d`               |
| `PHPSTAN_LEVEL`  | _(none)_   | Analysis level (adds `--level N`)                |
| `PHPSTAN_SKIP_PATTERNS` | _(none)_ | Extra comma-separated, case-insensitive substrings by which this project's PHPStan announces it declined to run |

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

## What a result covers, and what it means when there isn't one

**Inheritance is in scope.** Analysing one file does not mean PHPStan only sees
one file. Parent classes, interfaces and traits are resolved through the
config's `paths` / `scanDirectories`, so inheritance-compatibility rules —
`property.extraNativeType`, `method.childParameterType`,
`return.phpDocType` — do fire on a single-file run, reported against the child.
Give the adapter your project config (`PHPSTAN_CONFIG`); without one, PHPStan
has no way to find a parent and says so with `class.notFound` rather than
falling quiet.

The one case it cannot see through: if your `ignoreErrors` suppresses
`class.notFound`, an unreachable parent becomes silent _and_ the inheritance
rules cannot run. Don't ignore that identifier project-wide.

**A refusal is not a pass.** When PHPStan declines to analyse the file — it
sits under `excludePaths.analyse`, matches no configured path, or the run dies
— it prints to stderr and puts nothing on stdout. That is an absence of
information, and the adapter returns the third state rather than a verdict:

```json
{"tool": "phpstan", "file": "src/Foo.php", "ok": true, "count": 0,
 "errors": [], "duration_ms": 607, "skipped": "[ERROR] No files found to analyse."}
```

A `skipped` result never renders a `✗`, never rolls back an edit and is never
cached (see [SCHEMA.md](../SCHEMA.md), "Skipped: the third state"). An exit the
adapter _cannot_ explain is reported as an error naming the exit code — an
unknown failure is not a refusal, and swallowing it is the same mistake
pointing the other way. Only a genuinely quiet success — empty stdout, exit 0 —
is read as clean.
