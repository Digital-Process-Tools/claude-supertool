# Validators — full reference

## What validators are

Validators are squiggle-on-save for the LLM. After every mutating op — `edit`, `replace`, `replace_lines`, `paste`, `vim` — supertool runs the matching validators against the result file. If one fails and `rollback_on_fail: true` is set, the file reverts atomically to its pre-edit state and the model gets an immediate error receipt with the parse error, line number, and column. No broken files sitting around. No "edit succeeded, discovered it three turns later."

The model retries with real information instead of hallucinating a fix.

## How they hook in

Each validator entry declares a `hooks_into` array listing the mutating ops it should run after:

```json
"hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"]
```

Validators are declared per-file-type in `.supertool.json` under `validators`, keyed by validator name. Each entry matches files via a `match` glob:

```json
{
  "validators": {
    "jsonlint": {
      "cmd": "python3 {supertool_dir}/validators/jsonlint/jsonlint.py {file}",
      "match": "*.json",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "rollback_on_fail": true,
      "timeout": 10
    }
  }
}
```

Multiple entries for the same language are fine — `*.yml` and `*.yaml` need separate entries, for example.

You can also invoke any validator explicitly, without an edit, via the `validate` op:

```bash
./supertool 'validate:src/Foo.php'        # all matching validators
./supertool 'validate:src/Foo.php:phplint' # specific validator
```

## Graceful skip

When the underlying toolchain is missing (e.g. `stylelint` not installed, `terraform` not on PATH), the validator wrapper warns and exits 0. Supertool stays fully usable in any repo without pre-installed dependencies. No validator failure blocks an unrelated edit.

## Bundled validators

Enable any of these by copying the relevant entry from `.supertool.example.json` into your project's `.supertool.json`. Each ships as a thin Python wrapper that delegates to the real tool and handles graceful skip when the tool is absent.

| Language / format   | Validator name   | Requires                           | Notes                                      |
|---------------------|------------------|------------------------------------|--------------------------------------------|
| PHP                 | `phplint`        | `php` on PATH                      | Uses `php -l`                              |
| XML                 | `xmllint`        | `libxml2` (`xmllint` CLI)          | Reports line + column on parse error       |
| JSON                | `jsonlint`       | stdlib only                        | No external dep — always available         |
| YAML (`.yml`)       | `yaml-check`     | PyYAML (`pip install pyyaml`)      | Separate entry needed for `.yaml`          |
| YAML (`.yaml`)      | `yaml-check-yaml`| PyYAML (`pip install pyyaml`)      | Identical logic, different glob            |
| INI                 | `inilint`        | stdlib only                        | No external dep                            |
| Python              | `py-compile`     | stdlib only                        | Uses `py_compile` — syntax only, not type  |
| Bash                | `bash-check`     | `bash` on PATH                     | Uses `bash -n`                             |
| JavaScript          | `node-check`     | `node` on PATH                     | Uses `node --check`                        |
| CSS / SCSS          | `stylelint`      | `stylelint` npm package            | Config from project `.stylelintrc`         |
| TOML                | `tomllint`       | stdlib (3.11+) or `tomli`          | Falls back to `tomli` on 3.10              |
| Markdown            | `markdownlint`   | `markdownlint` CLI                 | `npm install -g markdownlint-cli`          |
| Ruby                | `ruby-check`     | `ruby` on PATH                     | Uses `ruby -c`                             |
| Dockerfile          | `hadolint`       | `hadolint` on PATH                 | Catches both syntax and best-practice lint |
| TypeScript (`.ts`)  | `tsc-check`      | `tsc` (`npm install -g typescript`)| `--noEmit` — no output files written       |
| TypeScript (`.tsx`) | `tsc-check-tsx`  | `tsc` (`npm install -g typescript`)| Separate entry needed for `.tsx`           |
| Go                  | `gofmt-check`    | `gofmt` (ships with Go)            | Fails on formatting diff, not just syntax  |
| Terraform           | `terraform-check`| `terraform` CLI                    | Uses `terraform validate`                  |
| Rust                | `cargo-check`    | `cargo` (ships with Rust)          | Uses `cargo check` — full type resolution  |
| PHP — static  | `phpstan`        | `phpstan` binary (PATH or via `PHPSTAN_BIN` env)  | Shell wrapper, env-configured. See [README](../validators/phpstan/README.md) |
| PHP — mess    | `phpmd`          | `phpmd` binary (PATH or via `PHPMD_BIN` env)      | Shell wrapper, env-configured. See [README](../validators/phpmd/README.md) |


## Adding your own

Any tool that exits non-zero on bad input works. Three lines of JSON and a new language is supported.

Example for Elixir:

```json
"elixir-check": {
  "cmd": "elixir -c {file}",
  "match": "*.ex",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "rollback_on_fail": true,
  "timeout": 10
}
```

The `{file}` placeholder is replaced with the absolute path to the file being validated. `{supertool_dir}` is also available for pointing at bundled wrapper scripts. See `validators/SCHEMA.md` for the full adapter contract if you want structured error output (line numbers, error categories, fix suggestions). PRs for languages you edit regularly are welcome.

## Rollback semantics

`rollback_on_fail` controls whether the file reverts when validation fails:

| Value   | When to use                                                                  |
|---------|------------------------------------------------------------------------------|
| `true`  | Syntax errors, parse failures — a broken file is worse than no edit         |
| `false` | Style/lint warnings where the edit itself is valid — warn but keep the file |

**`true` is the right default for most validators.** A PHP file with a missing `}` is not a partially-edited file — it's a broken file. Rollback gives the model a clean retry surface instead of a corrupt starting point.

`false` makes sense for opinionated linters like `hadolint` or `markdownlint` where violations are informational and the file is still structurally valid.

**Parallel execution** — validators use the project's `parallel` setting. When multiple validators match a single file (e.g. a `.ts` file matching both `tsc-check` and `stylelint`), they run concurrently. The first failure triggers rollback if `rollback_on_fail` is set; remaining validators still complete.

## Field reference

Full list of `.supertool.json` validator config fields:

| Field              | Notes                                                                                  |
|--------------------|----------------------------------------------------------------------------------------|
| `cmd`              | Shell command. `{file}` → target path. `{supertool_dir}` → supertool install dir.      |
| `match`            | Glob filter on the target path (default `*`).                                          |
| `hooks_into`       | Op names to wrap (subset of `edit`, `replace`, `replace_lines`, `paste`, `vim`).       |
| `rollback_on_fail` | Restore pre-edit file content if the validator's count went up or ok flipped to false. |
| `resolve`          | Shell cmd returning an alternate target path (e.g. source-file → test-file).           |
| `timeout`          | Seconds. Default 60.                                                                    |
| `opt_in`           | If true, validator only runs on explicit request via the `validate` op.                |
| `env`              | Optional `{KEY: VAL}` block merged into the subprocess environment. Values are coerced to strings. Useful for pointing wrappers at a project-local binary or config without touching the system environment. |

### env — usage

Pass tool-specific config without shell exports:

```json
"phpstan": {
  "cmd": "bash {supertool_dir}/validators/phpstan/phpstan.sh {file}",
  "match": "*.php",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "rollback_on_fail": false,
  "timeout": 60,
  "env": {
    "PHPSTAN_BIN": "./vendor/bin/phpstan",
    "PHPSTAN_CONFIG": "phpstan.neon",
    "PHPSTAN_LEVEL": "8"
  }
}
```

The `env` block is merged on top of the inherited process environment (`os.environ | spec.env`), so unset keys fall through to whatever the shell already has.

## Caching

Results are auto-cached at `~/.cache/supertool/validators/`, keyed on `sha256(file_content) + name + cmd`. Validators skip re-running when the file hasn't changed since the last pass.

Disable caching per-call with the `SUPERTOOL_NO_VALIDATOR_CACHE=1` env var, or per-project with `"validator_cache": false` in `.supertool.json`.

## Manual run

Run validators explicitly against any file without an edit op:

```bash
./supertool 'validate:src/Foo.php'                    # all matching validators
./supertool 'validate:src/Foo.php:phplint,phpstan'    # filtered to named validators
```

Useful for a pre-commit sweep or spot-checking a file you didn't edit this session.

## Output example

After an edit that breaks PHP syntax with `rollback_on_fail: true`:

```
[validators]
phplint : 0 → 1        (+1)   ✗
  + L42 parse  Parse error: syntax error, unexpected token "{" in ... on line 42

[rolled back] phplint regressed; file restored
```

The model gets a clean retry surface with the exact line and error — no broken file left behind.

## Adapter contract

Custom validators must conform to the adapter contract in `validators/SCHEMA.md`. Each adapter takes one file arg and prints a JSON object on stdout with a standardised shape (ok, count, errors). The bundled adapters (`validators/phplint/`, `validators/xmllint/`, etc.) are the reference implementations.

Two optional output fields worth knowing: `source_context` (array of source lines centered on the error, rendered indented under each error in verbose mode) and `diff` (unified diff string, rendered as a fenced block — useful for tools like Rector that produce a suggested patch). Full spec in [SCHEMA.md](../validators/SCHEMA.md).

## Format-on-save

See [formatters.md](formatters.md) — formatters run after every edit, before validators, normalizing whitespace and style before the safety check runs.
