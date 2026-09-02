# Formatters — full reference

## What formatters are

Formatters are the cosmetic counterpart to validators. After every mutating op — `edit`, `replace`, `replace_lines`, `paste`, `append`, `vim` — supertool runs the matching formatters against the result file, normalizing whitespace, quotes, and import order before validators check correctness.

Run order:

```
edit → formatter(s) → validator(s) → rollback if validate fails
```

Formatters mutate the file in place (e.g. `prettier --write`, `gofmt -w`). Validators then see the canonical, formatted result — so the diff the model gets reflects real structural changes, not noise from trailing spaces or quote style.

## How they hook in

Each formatter entry declares a `hooks_into` array listing the mutating ops it should run after — same as validators:

```json
"hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"]
```

Formatters are declared per-file-type in `.supertool.json` under `formatters`, keyed by formatter name. Each entry matches files via a `match` glob:

```json
{
  "formatters": {
    "prettier": {
      "cmd": "prettier --write {file}",
      "match": "*.{xml,scss,css,js,json,yml,yaml,md}",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
      "rollback_on_fail": false,
      "timeout": 30
    }
  }
}
```

## Bundled formatters

Enable any of these by copying the relevant entry from `.supertool.example.json` into your project's `.supertool.json`.

| Language / format                        | Formatter name  | Requires                                         | Notes                                         |
|------------------------------------------|-----------------|--------------------------------------------------|-----------------------------------------------|
| XML, SCSS, CSS, JS, JSON, YAML, Markdown | `prettier`      | `prettier` npm package                           | `npm install -g prettier`                     |
| Python                                   | `black`         | `black` pip package                              | `pip install black`                           |
| Python (SCHEMA adapter)                  | `ruff-format`   | `ruff` (already a dependency of most Python CI)  | `formatters/ruff-format/ruff-format.py` — metrics + structured errors, unlike the raw-`cmd` rows above (#2085) |
| Go                                       | `gofmt`         | Go toolchain                                     | Ships with Go — no extra install              |
| Rust                                     | `rustfmt`       | Rust toolchain                                   | Ships with `rustup` — `rustup component add rustfmt` |
| PHP (PSR-12, SCHEMA adapter)             | `phpcbf`        | PHP_CodeSniffer via Composer                     | `formatters/phpcbf/phpcbf.py` — metrics + structured errors, unlike the raw-`cmd` rows above; `composer global require squizlabs/php_codesniffer` |
| PHP (SCHEMA adapter)                     | `php-cs-fixer`  | `php-cs-fixer` (PHP-CS-Fixer)                    | `formatters/php-cs-fixer/php-cs-fixer.py` — metrics + structured errors; `composer global require friendsofphp/php-cs-fixer` |
| JS, TS, CSS, JSON, YAML, Markdown (SCHEMA adapter) | `prettier-write` | `prettier` npm package                  | `formatters/prettier-write/prettier-write.py` — metrics + structured errors, an alternative to the raw-`cmd` `prettier` row above |
| Bash / shell                             | `shfmt`         | `shfmt` binary                                   | `brew install shfmt` / `go install mvdan.cc/sh/v3/cmd/shfmt@latest` |
| Terraform / HCL                          | `terraform-fmt` | Terraform CLI                                    | Ships with Terraform — `brew install terraform` |
| Ruby                                     | `rubocop`       | RuboCop gem                                      | `gem install rubocop`; `-a` = auto-fix only   |

## Adding your own

Any tool that rewrites the file in place works. Three lines of JSON and a new language is supported.

Examples:

```json
"gofmt": {
  "cmd": "gofmt -w {file}",
  "match": "*.go",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "rollback_on_fail": false,
  "timeout": 10
}
```

```json
"black": {
  "cmd": "black {file}",
  "match": "*.py",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "rollback_on_fail": false,
  "timeout": 10
}
```

```json
"rustfmt": {
  "cmd": "rustfmt {file}",
  "match": "*.rs",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "rollback_on_fail": false,
  "timeout": 15
}
```

```json
"phpcbf": {
  "cmd": "phpcbf {file}",
  "match": "*.php",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "rollback_on_fail": false,
  "timeout": 15
}
```

The `{file}` placeholder is replaced with the absolute path to the file being formatted. `{supertool_dir}` is also available for pointing at local wrapper scripts.

## Difference from validators

| Aspect              | Formatters                          | Validators                          |
|---------------------|-------------------------------------|-------------------------------------|
| Purpose             | Normalize style (cosmetic)          | Check correctness (safety)          |
| Mutates file        | Yes — rewrites in place             | No — read-only                      |
| `rollback_on_fail`  | Default `false`                     | Default `true` (recommended)        |
| Run order           | First (after edit)                  | Second (after formatters)           |
| Output              | Warns on failure, continues         | Diff block + optional rollback      |

`rollback_on_fail: false` is the right default for formatters. If `prettier` chokes on a file that's structurally valid, the edit should still land — the validator is the safety net. Set `rollback_on_fail: true` only if you want a formatter failure to revert the file entirely.

## Repo opt-in

A formatter rewrites the whole file, so one that runs where the repo does not
format turns a two-line edit into a hundred-line diff — and in a repo with
hand-aligned markdown tables it is also simply wrong.

A formatter therefore runs only where there is evidence the repo wants it:

| Evidence | Example |
|---|---|
| The tool's own config file, searched from the edited file's directory up to **its** repo root | `.prettierrc`, `phpcs.xml`, `.php-cs-fixer.php`, `pyproject.toml` with `[tool.black]` |
| A manifest that names the tool | `package.json` holding a `"prettier"` key |
| An `env` entry in the spec carrying the rules | `"PHPCBF_STANDARD": "PSR12"` — no repo config expected |
| `"requires_config": false` in the spec | always run, whatever the repo looks like |
| `"requires_config": ["house.toml"]` | run only when that marker is present |
| The tool is unknown to supertool | never gated — absence of knowledge is not evidence of opt-out |

The search stops at the repo root (the first directory holding `.git`), and
symlinks are resolved first, so a file reached through a link is judged by the
repo that actually holds it. `SUPERTOOL_FORMAT_WITHOUT_CONFIG=1` restores the
old always-run behaviour for a whole invocation.

The gate applies to the automatic post-edit hook and to `format_staged`, which
sweeps files nobody named and usually runs from a pre-commit hook. It does
**not** apply to `format:PATH` — there the caller named one file and said what
they want done to it, and a tool that silently declined would be the wrong
answer.

Note that the *policy* — which `.supertool.json` is loaded — still comes from the
shell's cwd, while the *opt-in evidence* comes from the edited file's own repo.
Prefixing a call with `cwd:PATH` switches the policy too, which is what you want
when editing another checkout from this shell.

## Graceful skip

When the underlying tool is missing (e.g. `prettier` not installed), the formatter warns and continues. No formatter failure blocks an unrelated edit. The project stays fully usable without pre-installed dependencies.

## Field reference

Full list of `.supertool.json` formatter config fields:

| Field              | Notes                                                                              |
|--------------------|------------------------------------------------------------------------------------|
| `cmd`              | Shell command. `{file}` → target path. `{supertool_dir}` → supertool install dir. |
| `match`            | Glob filter on the target path (default `*`).                                      |
| `hooks_into`       | Op names to wrap (subset of `edit`, `replace`, `replace_lines`, `paste`, `vim`).  |
| `rollback_on_fail` | Restore pre-edit file content if the formatter fails. Default `false`.             |
| `timeout`          | Seconds. Default 30.                                                               |
| `requires_config`  | Opt-in override: `false` = always run; a list of filename globs = run only when one is present. Default: supertool's marker table for known tools. |
| `env`              | Extra environment for the command. A key ending `_CONFIG` / `_STANDARD` / `_RULES` / `_RULESET` counts as repo opt-in. |

## Output example

After an edit on a `.json` file with `prettier` configured:

```
edited: src/config.json

[formatters]
[formatter] prettier: some warning message
```

When the formatter succeeds silently (the normal case), no formatter block appears — the output is clean.

## `verify_failed` — a SCHEMA-adapter formatter that could not confirm its own result

The four SCHEMA-adapter formatters (`ruff-format`, `php-cs-fixer`, `prettier-write`, `phpcbf` — see "Bundled formatters" above) compute `metrics.lines_added` / `lines_removed` by re-reading the file after the tool exits. If that re-read hits an `OSError` (the file was deleted or its permissions changed between the format run and the read), falling back to "no changes" would publish `ok: true` with `metrics: {lines_added: 0, lines_removed: 0}` — byte-identical to the receipt for a file the tool genuinely left untouched (#2162).

When that happens, the payload instead carries `verify_failed: "<reason>"` alongside the same `ok: true`, 0/0 metrics. `_formatter_render_row` reads it before the silent-no-op check, so this case always renders a row — `formatted, but could not verify what changed: <reason>` — rather than disappearing into the quiet-clean-run path every genuine no-op takes. Its absence means the metrics are a real measurement; its presence means the 0/0 is "could not tell", not "nothing happened".
