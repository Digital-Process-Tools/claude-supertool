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
| Go                                       | `gofmt`         | Go toolchain                                     | Ships with Go — no extra install              |
| Rust                                     | `rustfmt`       | Rust toolchain                                   | Ships with `rustup` — `rustup component add rustfmt` |
| PHP (PSR-12)                             | `phpcbf`        | PHP_CodeSniffer via Composer                     | `composer global require squizlabs/php_codesniffer` |
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

## Output example

After an edit on a `.json` file with `prettier` configured:

```
edited: src/config.json

[formatters]
[formatter] prettier: some warning message
```

When the formatter succeeds silently (the normal case), no formatter block appears — the output is clean.
