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

## Text encoding

Two rules, both enforced by `tests/test_encoding_seam.py`. They exist because four separate defects came out of this seam one at a time ([#400](https://github.com/Digital-Process-Tools/claude-supertool/issues/400), [#415](https://github.com/Digital-Process-Tools/claude-supertool/pull/415), [#418](https://github.com/Digital-Process-Tools/claude-supertool/issues/418), [#431](https://github.com/Digital-Process-Tools/claude-supertool/pull/431)) — each found only once the previous one was fixed, and every one of them on a platform the author was not sitting on.

**1. Every text read and write names its codec.**

```python
path.read_text(encoding="utf-8")          # yes
open(path, encoding="utf-8")              # yes
open(path, "rb")                          # yes — binary decodes nothing
path.read_text()                          # no  — decodes with the locale
```

Without `encoding=`, Python decodes with `locale.getpreferredencoding()`: **cp1252** on a Windows console, **ASCII** under the C/POSIX locale that a great many cron jobs, containers and CI runners default to. Any file holding a `—` or a `✓` — which includes `presets/git.json`, shipped in this repo — then raises `UnicodeDecodeError`. A static AST scan over `supertool.py`, `presets/`, `hooks/`, `validators/`, `formatters/` and `notifiers/` fails the suite on a new one and names the file and line.

**2. A preset that prints non-ASCII must not depend on the console encoding.**

Presets run as separate processes and inherit none of supertool's stream setup. Supertool pins `PYTHONIOENCODING=utf-8` for every child it spawns, so an op invoked through supertool is covered without the script doing anything. A `presets/git/*.py` script is additionally required to call `use_utf8_stdout()` as the first statement of `main()`, because those are the ones run straight from a shell during conflict work, with no supertool in front of them. It lives in `presets/git/_git_common.py` — one definition, never a copy.

The failure this prevents is worth naming: printing a `✓` on a cp1252 console raises `UnicodeEncodeError` and kills the process *after* the work is done, so a commit that succeeded reports as a crash — and the operator runs it again.

**Testing this without a Windows runner.** Both halves reproduce on Linux and macOS, which is the whole point:

```bash
# read half — a bare open() becomes a hard error (3.10+)
python3 -X warn_default_encoding -W error::EncodingWarning supertool.py read:file.py

# stdout half — the Windows console default, anywhere
PYTHONIOENCODING=cp1252 python3 supertool.py git-status

# config decode — a C locale, with PEP 538/540 coercion disabled so it stays ASCII
LC_ALL=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 python3 supertool.py git-status
```

---

## Running tests

```bash
python3 -m pytest tests/
```

~4000 tests, 86% minimum coverage (enforced by pytest-cov). Current: 88%.

**The suite runs in parallel by default** — `addopts` carries `-n auto`, which
takes it from ~4m22s to ~1m08s on an 11-core machine. Two consequences worth
knowing before you read a red run:

- **Output from different tests interleaves.** A `print` or a captured traceback
  is still attributed correctly, but the lines around it may belong to another
  worker. `--tb=long` on a single failing test id is the reliable read.
- **`--pdb`, `-s` and breakpoints do not work under xdist.** Pass `-n0` to run
  serially:

  ```bash
  python3 -m pytest tests/test_foo.py -n0 --pdb
  ```

If a test passes with `-n0` and fails under `-n auto`, that is a real bug in the
test, not a parallelism problem — it means the test depends on state some other
test leaves behind, or on something about the environment that xdist happens to
change (worker id in the `tmp_path`, for instance — see
[#437](https://github.com/Digital-Process-Tools/claude-supertool/issues/437)).
Fix the dependency. Pinning the test to a worker, adding an `xdist_group`, or
forcing `--dist loadfile` hides it and leaves a test that is not testing what
its name claims.

**No install step is required to get a fully green suite**, including in a
fresh `git worktree add` checkout. `TestDispatchEndToEnd` in `tests/test_xml.py`
exercises real subprocess dispatch against `supertool.py` directly rather than
against the `supertool` file — that name is `.gitignore`d (it's either a local
symlink or an install-time artifact) and is absent from every worktree, since
`git worktree add` does not materialize ignored files. If you see those three
tests fail, you're on a stale checkout of this doc's advice, not a real
regression — check out master and re-run before assuming your change broke it.

Enable the pre-push hook (runs the full suite — slow tests included, coverage
not gated — exactly as CI does, before every push):

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
