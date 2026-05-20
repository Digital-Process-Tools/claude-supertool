# Contributing to supertool

Three ways to extend supertool: add a custom op for this project, bundle ops into a preset, or add a validator that runs after writes.

---

## Quick start

**Custom op** — one-project command, lives in `.supertool.json`. Done in 5 lines. See [Custom ops](#custom-ops) below.

**Preset** — reusable bundle for a tool or platform (e.g. GitLab, Kubernetes). Shareable across projects. See [Presets](#presets) below, or the [preset catalog](presets/index.md) for shipped examples.

**Validator** — post-write hook that runs after a file is saved. Syntax check, lint, type check. See [Validators](validators.md) for the adapter contract and field reference.

---

## Custom ops

Declare ops in `.supertool.json` under `"ops"`:

```json
{
  "ops": {
    "mypy": {
      "cmd": "mypy {file}",
      "timeout": 30,
      "description": "Type-check a Python file.",
      "syntax": "mypy:FILE",
      "example": "mypy:src/app/module.py"
    },
    "lint": "ruff check {file}"
  }
}
```

Shorthand string ops (`"lint": "ruff check {file}"`) work with a 60s default timeout. Full object form gives you explicit timeout, description, syntax, and example.

### Op schema

| Key | Required | Description |
|-----|----------|-------------|
| `cmd` | yes | Shell command to run. Supports placeholders (see below). |
| `timeout` | no | Seconds before the subprocess is killed. Default: 60. |
| `description` | no | One-line description shown in `ops` listing. |
| `syntax` | no | Usage pattern shown in help, e.g. `mypy:FILE`. |
| `example` | no | Concrete example, e.g. `mypy:src/app/module.py`. |
| `status` | no | `"experimental"` or `"stable"`. Informational only. |

### Placeholders

| Placeholder | Expands to | Example |
|-------------|-----------|---------|
| `{file}` | First argument, shell-quoted, treated as file path | `cat {file}` |
| `{dir}` | Directory of `{file}` | `ls {dir}` |
| `{arg}` | First argument, shell-quoted, no path validation | `glab issue view {arg}` |
| `{args}` | All arguments, each shell-quoted | `python3 tool.py {args}` |
| `{path}` | Preset directory with trailing `/` (presets only) | `python3 {path}gitlab/issue.py {arg}` |

Use `{file}`/`{dir}` for file operations, `{arg}`/`{args}` for non-file arguments (issue numbers, job IDs, etc.).

### Dispatch order

Built-in ops → custom ops (including preset ops) → aliases. Built-ins always win. Project ops override preset ops on name conflict.

### Extra config keys as environment variables

Any key in an op config that isn't a reserved key (`cmd`, `timeout`, `description`, `syntax`, `example`, `status`) is passed to the subprocess as a `SUPERTOOL_`-prefixed environment variable:

```json
{
  "ops": {
    "job": {
      "cmd": "python3 job.py {arg}",
      "lines": 80,
      "error_patterns": "ERROR,FAIL,Fatal"
    }
  }
}
```

The script receives `SUPERTOOL_LINES=80` and `SUPERTOOL_ERROR_PATTERNS=ERROR,FAIL,Fatal`. Use this to tune op behavior from JSON without modifying scripts.

---

## Presets

A preset is a JSON file declaring ops (and optionally aliases and validators) for a specific tool or platform.

### File layout

```
presets/
  mytools.json          # op manifest
  mytools/
    status.py           # helper scripts co-located here
    deploy.py
```

Place the manifest at `./presets/NAME.json` (project-level) or `~/.config/supertool/presets/NAME.json` (user-level). Helper scripts go in `./presets/NAME/` — the `{path}` placeholder resolves to the manifest's directory with a trailing `/`.

### Resolution order

1. `./presets/{name}.json` — project-level (team-specific, committed to the repo)
2. `~/.config/supertool/presets/{name}.json` — user-level (personal, not committed)
3. `{supertool install dir}/presets/{name}.json` — shipped with supertool

First found wins. Project ops always override preset ops on name conflict.

### Preset schema

Same op schema as custom ops, wrapped in a manifest:

```json
{
  "description": "My team's deploy tools",
  "requires": "kubectl",
  "ops": {
    "deploy-status": {
      "cmd": "python3 {path}mytools/status.py {arg}",
      "timeout": 15,
      "description": "Check deployment status for a service.",
      "syntax": "deploy-status:SERVICE",
      "example": "deploy-status:api-gateway"
    }
  }
}
```

The `requires` field is documentation only — supertool does not enforce it at runtime.

Enable the preset in `.supertool.json`:

```json
{ "presets": ["mytools"] }
```

See [docs/presets/index.md](presets/index.md) for the shipped preset catalog and more authoring notes.

---

## Validators

Validators are post-write hooks — they run after a file is written and report errors back to the caller. See [docs/validators.md](validators.md) for the full adapter contract and field reference. Don't duplicate that here — it's the authoritative source.

---

## Helper script conventions

- **Python stdlib preferred.** No third-party dependencies. If an op needs `requests`, reconsider the design.
- **Exit 0 on graceful skip.** If the required CLI tool is missing, print a friendly message and exit 0. Don't fail the whole supertool call because `kubectl` isn't installed.
- **Validators output JSON.** See [validators.md](validators.md) for the exact schema. Other scripts can output anything — supertool passes it through as-is.
- **One file per op.** `gitlab/issue.py`, `gitlab/mr.py`, `gitlab/pipeline.py` — not one monolithic `gitlab.py` with a dispatch table.
- **Scripts are co-located with their preset.** `presets/mytools/status.py`, not `scripts/status.py`. The `{path}` placeholder makes this work without hardcoded paths.

---

## Running tests

```bash
python3 -m pytest tests/
```

293 tests, 80% minimum coverage (enforced by pytest-cov). Current: 94%.

Enable the pre-push hook (runs pytest + enforces 80% coverage before every push):

```bash
git config core.hooksPath .githooks
```

The hook is in `.githooks/pre-push`, committed to the repo. Bypass with `git push --no-verify` (discouraged).

## Submitting upstream

Want to add a preset, op, or validator to the shipped supertool?

- **Branch naming:** `feat/short-description` for features, `fix/short-description` for bugs, `docs/short-description` for documentation.
- **One feature per PR.** A new preset is one PR. Adding a validator adapter is one PR. Bundling both makes review harder.
- **Tests in `tests/`.** New ops and validators need test coverage. Check existing tests for the pattern.
- **README update if introducing new shape.** If your PR adds a new top-level config key or changes op schema, update the README config reference section.
- **Commit messages:** `feat: add kubectl preset` / `fix: {path} placeholder on Windows` / `docs: add contributing guide`. Present tense, imperative mood, lowercase.
