# Contributing to supertool

Three ways to extend supertool: add a custom op for this project, bundle ops into a preset, or add a validator that runs after writes.

---

## Quick start

**Custom op** — one-project command, lives in `.supertool.json`. Done in 5 lines. See [Custom ops](#custom-ops) below.

**Preset** — reusable bundle for a tool or platform (e.g. GitLab, Kubernetes). Shareable across projects. See [Presets](#presets) below, or the [preset catalog](presets/index.md) for shipped examples.

**Validator** — post-write hook that runs after a file is saved. Syntax check, lint, type check. See [Validators](validators.md) for the adapter contract and field reference.

---

## Checking syntax against the supported floor

supertool supports Python 3.9–3.12. The interpreter you develop on is almost certainly newer than the floor, and **you cannot verify floor compatibility from it.**

**Never use `ast.parse(src, feature_version=(3, 9))` for this.** `feature_version` gates *grammar productions* — walrus, `match`, `except*`. It does not touch the tokenizer, so PEP 701 nested same-type quotes inside an f-string replacement field:

```python
f"File should be empty, got: {f.read_text(encoding="utf-8")!r}"
```

parse clean under `feature_version=(3, 9)` on any 3.12+ host, and raise `SyntaxError: f-string: unmatched '('` on 3.9/3.10/3.11. This is not hypothetical: it shipped in #473 and took nine of twelve CI legs red after every local check called it fine (#478).

**Run an older interpreter instead.**

```bash
pytest tests/test_syntax_floor_478.py -m ''     # uses the ladder below
PYTHON39=/path/to/python3.9 pytest tests/test_syntax_floor_478.py -m ''
```

`supertool._syntax_floor_check(paths)` resolves an interpreter in this order:

1. `$PYTHON39` — explicit escape hatch, **verified rather than trusted**: if it reports a version no older than the interpreter running the suite, it is rejected outright rather than quietly ignored. A declaration that buys nothing is worse than none, because it restores the false clean.
2. The running interpreter, when it is itself at or below the floor — the CI floor leg, where the check runs for real with nothing extra installed.
3. The lowest `pythonX.Y` on `PATH` between the floor and the running version. The binary is asked for its version; the filename is not believed.

**What it does not cover.** With nothing older than the host, the check returns a `skipped` result naming the escape hatch and does **not** report a pass — read the reason, do not read the absence. With an interpreter above the floor (a 3.11 lying around), the result carries a `partial` note: it catches PEP 701 and anything else newer than that interpreter, but not syntax legal on 3.11 and illegal on 3.9. Full floor fidelity comes from the 3.9 CI leg, and `test_ci_matrix_covers_the_syntax_floor` fails if that leg ever leaves the matrix — otherwise the check would skip everywhere and render as a pass.

### What the repo-wide Python guards scan, and what they skip

Two guards walk the whole tree — the syntax floor above, and `tests/test_no_bare_python3_spawn.py`. Both ask one question through `tests/_repo_walk.py`: **is this path repository source, or machine state that happens to sit in the working tree?** There is no per-caller option, deliberately: a file being source does not depend on which rule is about to be applied to it.

The answer is decided in this order.

1. **Names, unconditionally.** `__pycache__`, `node_modules`, `venv`, `.venv`, `.git`, `.tox`, `.nox`, `.eggs`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.hypothesis`. Git cannot be relied on for these: `.git` is never reported by `git ls-files --ignored`, and no ignore file in this repo names a virtualenv at all ([#577](https://github.com/Digital-Process-Tools/claude-supertool/issues/577)) — an in-repo `.venv` is untracked, unignored, and thousands of third-party files deep, many legitimately un-compilable at the 3.9 floor.
2. **Then `git ls-files --others --ignored --exclude-standard --directory`.** Whatever git calls ignored is machine state, which is what makes `build/`, `dist/`, `htmlcov/` and the next packaging tool's output exempt before anyone files them ([#449](https://github.com/Digital-Process-Tools/claude-supertool/issues/449), [#575](https://github.com/Digital-Process-Tools/claude-supertool/issues/575)). Asked of the *ignored* set and never the *tracked* set — a file being written right now is untracked, and that is exactly when a guard earns its keep.
3. **When git cannot answer at all** — no repo, no git binary, a non-zero exit — the walk falls back to a small denylist of build-output names and keeps going. It never reads an unanswered question as a clean sheet, and it never narrows: with no git answer the walk scans *more*, not less. An empty scan is a hard `AssertionError`, never a pass.

**A leading dot is not one of the names, and that is the point** ([#593](https://github.com/Digital-Process-Tools/claude-supertool/issues/593)). The rule used to exclude every dot-prefixed path component, which quietly took `.github/` and `.githooks/` — version-controlled, shipped, and installed by every contributor — out of both guards. `.venv` is machine state that happens to be dot-prefixed; `.githooks` is source that happens to be dot-prefixed; the dot tells them apart in neither direction.

**If a directory of yours is not being scanned**, it is because a component of its path is in the name list above, or because git reports it as ignored. Both are visible: `git check-ignore -v <path>` answers the second, and the first is a literal in `tests/_repo_walk.py`. If you have added a tool whose cache directory is now being scanned and producing noise, the fix is to gitignore it — that is step 2 doing its job — and naming it in step 1 is reserved for state git provably cannot report. Adding a directory of *ours* requires nothing: it is scanned by default, which is the whole property #593 bought.

Shell files are a separate walk with a separate question ("what does this repository ship that a shell executes"), discovered from `git ls-files` plus a shebang read in `tests/test_ci_non_python_coverage_557.py`. See the CI table below.

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
| `syntax` | no | Usage pattern shown in help, e.g. `mypy:FILE`. **Parsed, not just displayed** — see the note below. |
| `example` | no | Concrete example, e.g. `mypy:src/app/module.py`. |
| `status` | no | `"experimental"` or `"stable"`. Informational only. |
| `safety` | yes, in shipped presets | `"read-only"`, `"writes"` or `"acts"`. Rendered as the class marker in `ops:roster`. |
| `paths` | yes, if any argument is a filesystem path | `{"args": [1], "root": "cwd"}` — which argument positions are paths, and the boundary they must stay inside. See below. |
| `replaces` | no | Raw shell invocations this op supersedes. The shipped `PreToolUse` hook refuses them. See below. |

**`safety` decides whether someone may learn your op by calling it.** The `ops:roster` listing is names plus one marker, because that is what fits the ~7KB SessionStart cap — and a bare name is only actionable for an op you can probe. Most ops teach their own signature on contact: `between:FILE:747:820` answers *"'820' was read as the path"* and names the op that does take a range. An op that reaches outside the tree cannot be learned that way — `oss_train` force-pushes a merge train, `gh-pr-merge` merges, `watch` spawns a poller.

| Value | Means | Marker |
|-------|-------|--------|
| `read-only` | Safe to invoke blind. Reads files, or makes a read-only network call. | *(none)* |
| `writes` | Changes files in this tree. | `*` |
| `acts` | Changes something outside this tree, or starts something that outlives the call. | `!` |

**Classify by consequence, not by mechanism.** Nearly every preset op runs a subprocess, so "spawns" is not the test. `bluesky_status_since` reads a feed — which sounds read-only — and writes `~/.config/bluesky/last_check` on success, so one probe to learn its signature advances the watermark and the *next* real briefing reports an empty window it silently consumed. That is `acts`. A session-token or username cache written on the way past is not: nothing downstream reads it as a fact about the world.

An op with no `safety` key, or an unrecognised value, renders `!`. The fallback is the loudest class on purpose: an `acts` op mis-rendered as probe-safe invites somebody to probe it, and the reverse costs one `help:OP` call. Every op in a shipped `presets/*.json` must declare one — `tests/test_ops_roster_1231.py` fails otherwise, so a new op cannot ship classed by accident. Built-in ops take their class from `_OP_SAFETY_BUILTIN` in `_supertool.py` instead: it is a fact about the binary, and a project's `.supertool.json` may be absent, stale, or somebody else's.

`syntax` reads like documentation because it's rendered in `ops` output — but for any op whose syntax uses `:::` (e.g. `git-commit:::MESSAGE[:::PATHS...]`), it is also parsed to derive that op's `@file`/`@payload` field registry: `MESSAGE[:::PATHS...]` becomes the fields `message`, `paths`. Edit it for readability — add a clarifying parenthetical, reword a field name into prose — and the parser can silently stop deriving clean field names, which silently deletes the op's whole payload route. No error, no warning: the op just stops accepting `op:@-`/`op:@payload`, while its docs (and the `ops` listing) still describe the route as if it existed. `tests/test_at_file_route.py::TestPayloadRoutePin` pins which real ops currently have a payload route specifically to catch this at test time — if you're touching a `:::`-bearing `syntax` string, expect that test to have an opinion. See [#770](https://github.com/Digital-Process-Tools/claude-supertool/issues/770).

### `replaces` — the raw command this op supersedes

If your op exists because a raw command was the wrong way to get the answer, say so **in the op**, and the shipped guard enforces it for every plugin user:

```json
"gh-pr": {
  "syntax": "gh-pr:NUMBER_OR_BRANCH[:status|:full|:diff[:PATH]|:threads]",
  "replaces": [
    { "argv": "gh pr view", "use": "gh-pr:NUMBER" },
    { "argv": "gh pr view", "flag": "--json", "value": "state", "use": "gh-pr:NUMBER:status" },
    { "argv": "gh pr view", "flag": "--json", "value": "files",  "use": "gh-pr:NUMBER:diff" }
  ]
}
```

| Key | Required | Means |
|-----|----------|-------|
| `argv` | yes | Command word and subcommands, space-separated. Matched **token-for-token** against the start of a simple command, never as a substring. |
| `flag` | no | The entry matches only if the command carries this flag. |
| `value` | no | ...and only if that flag's value is, or contains as a comma-list member, this. Requires `flag`. |
| `unless_flag` | no | Flag spellings whose presence means this entry does **not** claim the command. A string, or a list; the single item `"*"` is any flag at all. |
| `use` | no | The op invocation the refusal names. Defaults to the op's `syntax`. |

**The most specific matching entry wins**, so flags select *which* op is named rather than whether to block: `gh pr view 12` names `gh-pr:NUMBER`, `gh pr view 12 --json state` names `gh-pr:NUMBER:status`. Ties are listed together rather than resolved arbitrarily.

**`unless_flag` says *this shape of the command has no replacement*** ([#1394](https://github.com/Digital-Process-Tools/claude-supertool/issues/1394)). `flag` and `value` only ever add specificity, so before it an op could say *this argv is mine* and could not say *this argv is mine except when it carries these flags* — and the only escape hatch, declaring no entry, is all-or-nothing per op while the opt-out (`raw_command_guard: false`) is repo-global. So one over-broad entry took every other mapping in the repository down with it. `gl-api` is the shipped example:

```json
{ "argv": "glab api", "unless_flag": ["*"], "use": "gl-api:PATH" }
```

`glab api` is GET by default and a **write** under `-X`, `-F`, `-f` or `--input`, and supertool has no GitLab write route at any spelling. `"*"` rather than a list of those four because gl-api forwards no flags at all: a denylist of the write spellings would have left `-H`, `--hostname`, `-i`, `--output`, `--silent` — and `glab api -h` — blocked with no way past, which is a guard wedging a CLI's own help.

Four things it does *not* do, each a decision rather than an omission:

- **It un-claims the entry, it does not allow the command.** An exclusion loses to nothing: a second, broader entry that still matches still blocks. A veto that crossed entries would let an op in a repository's own `.supertool.json` un-block a command a shipped op legitimately claims.
- **It keys on the flag, never on its value.** `glab api -X GET` is a read and is excluded anyway. That costs a *missed block*, which is the direction this guard may be wrong in — there is no per-command way past a wrong one.
- **A help flag needs no `unless_flag` and cannot be overridden by one.** `--help` and `-h` un-claim every entry in every registry, because a command that only describes itself performs nothing an op could supersede. Measured on the v0.35.0 tree, all 28 shipped mappings blocked their own `--help`, and each refusal named the op that does the thing the flag declines. Whole tokens only — unlike `unless_flag`, a short cluster is **not** expanded, or `-xh` would un-claim the whole repository at once.
- **A bare `--` ends the option list**, here and for `flag` alike. `gh pr diff 1 -- --json` names a path called `--json`, and a flag inside an argument is not a flag. That covers the help flags too: `gh pr create -- --help` is a file called `--help`.
- **It matches `--method`, `--method=POST` and clustered short flags.** A single-dash token is read as a cluster of single-letter flags, so an entry excluding `-s` excludes `-sb` — the ordinary spelling of the intent `-s` was excluded for — and one excluding `-X` excludes `-XPOST`. That widens exclusions, which blocks *less*, so it is not extended to the `flag` matcher, where it would block more. Two consequences to know: a `--` token is never expanded (or `--foo` would match an excluded `-f`), and a short flag carrying a clustered **value** is expanded regardless of what that value spells, so an entry excluding `-f` also un-claims `-ofoo`. Telling those apart needs per-flag arity this schema does not carry.

An `unless_flag` that is neither a non-empty string nor a list of non-empty strings **drops the whole entry** and leaves a note, so the verdict is `undecided` rather than clean. Reading an unreadable exclusion as an absent one would turn one typo into exactly the over-broad block this key exists to prevent. The one list shape that is *not* malformed is the empty one: `[]` excludes nothing, exactly as omitting the key does, because that is what it literally says — the rule above is about values whose meaning cannot be recovered, not about values that mean nothing.

Five rules for writing one:

- **Declare only what the op genuinely replaces.** Absence *is* the escape hatch. There is no op for tagging, releasing, deleting a ref or re-running a workflow, so nothing maps `git tag`, `gh release create`, `gh api -X DELETE` or `gh run rerun` and they are never blocked. (One shape escapes that rule and is disclosed below: `git push origin <tagname>` is a tag operation the matcher cannot separate from an ordinary branch push.) An entry for something the op only half-answers turns a working command into a dead end.
- **Do not reach for `flag` to make a match narrower out of caution.** An entry with no `flag` matches the command word, which is usually what you mean; a flag makes the block conditional on a spelling the caller happens to use. The exception is a flag that names the *question* rather than a spelling: `gh-branch` claims `gh run list --branch` and `--commit` and not the command word, because a bare `gh run list` is an enumeration it does not produce.
- **Exclude the flag that means "not in a terminal".** `--web` / `-w` on `gh`, and any equivalent, ask for a browser, and no op opens one — so a mapping that claims them refuses a command and names an op that cannot do the job. Every `gh` entry that has the flag excludes it ([#1384](https://github.com/Digital-Process-Tools/claude-supertool/issues/1384)); the four entries shipped in v0.34.0 did not, and that was a dead end for a release.
- **Every op is either mapped or a recorded absence.** `tests/test_replaces_census_1384.py` partitions every op in every shipped preset into the two, each absence carrying its reason, and goes red when a new op belongs to neither. A silent 15-of-87 and a considered 15-of-87 render identically otherwise, which is this repository's own defect class pointed at its own guard.
- **`replaces` is metadata, not op configuration.** It is a reserved key, so unlike `per_page` or `error_patterns` it is *not* exported to your op's subprocess as `SUPERTOOL_REPLACES`.

**Two shapes an entry cannot see**, both worth knowing before you write one ([#1384](https://github.com/Digital-Process-Tools/claude-supertool/issues/1384)):

- **The value of a positional.** `git push origin master` and `git push origin v0.34.0` are the same argv shape, the same arity and the same token classes. `unless_flag` keys on flags and there is no `unless_positional` — telling those two apart means asking the repository whether a ref is a tag, at guard time, on every Bash call, before the command runs. So `presets/git.json` claims both and pushing a tag by name is blocked with `git-push` named, which does not do tags. That is a **wrong block, disclosed** in `docs/presets/git.md` rather than avoided, because the alternative was leaving the whole git family ungated.
- **A global option before the subcommand.** `argv` matches a contiguous token prefix, so `{"argv": "git status"}` does not match `git -C /tmp/x status`, `git -c core.pager=cat status` or `git --git-dir=/tmp/x/.git status`. The only spelling `replaces` offers instead is `{"argv": "git -C"}`, which claims *every* `git -C` subcommand including the ones no op answers, so `presets/git.json` declines it and says so.

**Builtin ops are outside the guard, deliberately** ([#1384](https://github.com/Digital-Process-Tools/claude-supertool/issues/1384)). `cat` is not refused in favour of `read`, `find` in favour of `glob`, `ls -R` in favour of `tree`. Four reasons, pinned in `tests/test_guard_builtin_ops_absent_1384.py`: the guard scores every *pipeline segment*, and those words are usually a stage of a larger computation rather than the command; the only way past a wrong block is `raw_command_guard: false`, which is repo-global, so one `grep` in a pipeline costs you all 22 forge mappings; every mapping that exists prevents a *wrong answer*, while `cat file` returns the file and its cost is round-trips; and mechanically builtins never enter `_op_registry`, carry no `description`, and would therefore deny with an empty refusal body. `find -name` → `glob` and `ls -R` → `tree` are the two whole-command cases where the first and third reasons are weaker, if anyone reopens it.

The matcher tokenises the command with `shlex` and matches argv — see `_guard_segments` in `_supertool.py` for exactly which shell constructs it models and which it does not. That is the design decision [#1347](https://github.com/Digital-Process-Tools/claude-supertool/issues/1347) stands on: the hand-written regexes it replaced failed by reading a command as a string, one firing on a *directory name* and another refusing commands that carried the very flag it required.

### An op that takes a path declares where paths may point

Builtin ops have been gated since [#146](https://github.com/Digital-Process-Tools/claude-supertool/issues/146): `_PATH_ARG_POSITIONS` in `_supertool.py` says which argument slot is a path, and dispatch refuses one that resolves outside the cwd. **No preset op was in that table**, so until [#1287](https://github.com/Digital-Process-Tools/claude-supertool/issues/1287) a preset op with a path argument enforced containment itself or not at all — and "not at all" was the default for anything newly written. [#1283](https://github.com/Digital-Process-Tools/claude-supertool/issues/1283) was one instance of that: `claims:/etc/hosts` read the file in the same call that `read:/etc/hosts` refused it.

Declare it:

```json
"paths": { "args": [1], "root": "repo" }
```

- **`args`** — argument positions, counting the op name as `0`, exactly like `_PATH_ARG_POSITIONS`. So `claims:PATH` is `[1]` and `xml_attr:PATH:XPATH:ATTR` is also `[1]`. Only the positions you name are checked: over-gating is its own defect ([#1164](https://github.com/Digital-Process-Tools/claude-supertool/issues/1164) refused a legitimate local slice by gating a slot that held a regex). Non-negative only — a negative index is refused rather than read Python-style, because `parts[-1]` on a bare call is the op name.
- **`root`** — `"cwd"` (the core's boundary, and the right answer for almost everything) or `"repo"` (the repository root). `claims` needs `"repo"` because it resolves a relative argument against the git toplevel: under a cwd boundary, `claims:docs/x.md` run from `docs/` would be refused, and that call works and should.
- **`"args": []`** — a declaration that no argument here is a filesystem path. `gl-api:PATH` means an API route, not a file. Written down rather than defaulted, because the two look identical from outside. This form carries no `root`: there is nothing for a boundary to bound, and inventing one would read as a choice somebody made.

**An op that names a path and that declares nothing is refused at dispatch.** Not `skipped`: the three-state rule this repo applies everywhere else has no third state here, because a path argument that reaches no check is not a check that could not run — it is an unchecked read, and it renders identically to a checked one.

**Two detectors, OR'd — neither supersedes the other** ([#1350](https://github.com/Digital-Process-Tools/claude-supertool/issues/1350)):

- **The `syntax` string**, matched per `_`-separated component, so `PATH`, `PATHS`, `FILE`, `@FILE`, `MD_FILE` and `TEXT_OR_FILE_OR_file://PATH` all count and `NUMBER_OR_BRANCH` does not.
- **The `cmd` template**, when it substitutes `{file}` or `{dir}` — the core's own path placeholders, so writing one has already told the core which argument is meant to be a path. Until #1350 this was not read at all, and an op with no `syntax` key took the "no path here" arm: no declaration demanded, no check run, and a verdict indistinguishable from declared-clean. That is the rule's own detector answering "no" where it meant "I could not tell".

They are OR'd rather than ranked because of a measurement: of the 24 shipped ops the `syntax` detector finds, **zero** carry `{file}` or `{dir}` in their `cmd`, so letting `cmd` supersede `syntax` would have disarmed the gate for every one of them. Going the other way, exactly one shipped op is found by `cmd` alone — `oss_train` in this repo's own `.supertool.json`.

**`{arg}` is deliberately not a signal**, even though it substitutes the same `parts[1]` that `{file}` does. Sixteen shipped ops pass a handle, a ref, a tag, an ID or a repo slug through `{arg}` and none takes a path; promoting it would refuse all sixteen and gate nothing. If your op means a path, write `{file}`.

Either detector only ever raises the *question* — a path hidden inside a `|`-separated blob cannot be located by reading the syntax, which is why the position is declared explicitly.

Nineteen shipped ops predate the rule and are grandfathered by name in `_UNDECLARED_PATH_OPS`. **That set only shrinks**, and `tests/test_preset_path_chokepoint_1287.py` fails if a name is added to it or if a new op with a path skips the declaration — so a newly written op cannot inherit the old default by being written after it. It has shrunk once: `gl-api` was the op the detector's own docstring held up as the worked example of a declared op while sitting in the register, and [#1351](https://github.com/Digital-Process-Tools/claude-supertool/issues/1351) declared it for real rather than re-citing a different one.

An op written as a bare command string — the `.supertool.json` shorthand, `"myop": "mytool {file}"` — is not gated, and since #1350 that is a decision rather than a side effect: its `cmd` now carries a signal the detector would read, but a string has no `paths` key and so no way to answer the demand, so the gate returns before either detector runs. That is deliberate and not a hole in the same sense: those ops are the caller's own config, at the same trust level as validators and presets, and they can declare a boundary by being written as an object instead.

The core's opt-outs (`SUPERTOOL_ALLOW_OUTSIDE_CWD=1`, or `"allow_outside_cwd": true` in `.supertool.json`) are honoured at every boundary, and the refusal names them. The gate is `_safe_path` with a different root, not a second copy of the rule — [#882](https://github.com/Digital-Process-Tools/claude-supertool/issues/882) and [#889](https://github.com/Digital-Process-Tools/claude-supertool/issues/889) are what a second copy costs.

A core check is defence in depth, never a replacement: `presets/claims/check.py` keeps its own boundary check, because the script is also runnable directly.

### Placeholders

| Placeholder | Expands to | Example |
|-------------|-----------|---------|
| `{file}` | First argument, shell-quoted, treated as file path | `cat {file}` |
| `{dir}` | Directory of `{file}` | `ls {dir}` |
| `{arg}` | First argument, shell-quoted, no path validation | `glab issue view {arg}` |
| `{args}` | All arguments, each shell-quoted | `python3 tool.py {args}` |
| `{path}` | Preset directory with trailing `/` (presets only) | `python3 {path}gitlab/issue.py {arg}` |
| `{python}` | The interpreter supertool is running under | `{python} scripts/oss_train.py {file}` |

Use `{file}`/`{dir}` for file operations, `{arg}`/`{args}` for non-file arguments (issue numbers, job IDs, etc.).

### Dispatch order

Built-in ops → custom ops (including preset ops) → aliases. Built-ins always win. Project ops override preset ops on name conflict.

### Extra config keys as environment variables

**`SUPERTOOL_ARG_SEP` is set on every preset subprocess, whatever the config
says.** It holds how *this call's* fields were separated — `:::`, `:`, or the
empty string when they arrived structured through an `@payload` and nothing
was tokenized at all. A preset that reconstructs the caller's input for an
error message needs it: `git-commit` rejoined a split-up message on `:` no
matter what had split it, so a `:::` inside a message came back as a `:` and
the suggested repair, pasted, committed bytes the caller never wrote (#946).
The three states are the point — a payload's fields were never split, and an
error that says they were is a claim about a parse that did not run.

Any key in an op config that isn't a reserved key (`cmd`, `timeout`, `description`, `syntax`, `example`, `status`, `restartMcp`) is passed to the subprocess as a `SUPERTOOL_`-prefixed environment variable:

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

### `scripts/` — this repo's own maintainer ops

`scripts/` holds project ops that drive *this* repository and are registered in this repository's `.supertool.json`. They are not part of the wheel (`py-modules` is `supertool` and `_supertool`), they are not in `.supertool.example.json`, and they are not in the README: an op that reads `~/Documents/st-wt` answers for one machine's layout, and advertising it to users of the released tool would be documenting something they cannot run.

`oss_train` is the one that exists. It rebases every open branch onto `master`, resolves the conflicts, and force-pushes — 55 of the last 60 merged PRs touched `CHANGELOG.md`, so with N open PRs each merge re-conflicts the other N-1 ([#906](https://github.com/Digital-Process-Tools/claude-supertool/issues/906)). Five things about it are load-bearing:

- **The flag is comma-separated.** `oss_train:all,dry`, never `all:dry` — only the first `:`-token reaches a project op's `{file}` and the rest is discarded silently, so a colon form runs the train it looked like it was previewing. A colon that *does* survive is still read as a separator, with one exception: a Windows drive prefix. The blanket rewrite cut `C:\Users\x\seed` in half and the refusal then named `\Users\x\seed`, a string nobody typed, in the one line whose job is saying which target was rejected ([#1247](https://github.com/Digital-Process-Tools/claude-supertool/issues/1247), four Windows legs). The rejected target is quoted rather than `repr()`'d for the same reason — `repr` doubles every backslash.
- **A bare `oss_train` is refused**, and the refusal names how many branches it would have touched. It used to mean `all`, and typing it to find out whether the op was registered force-pushed fourteen worktrees ([#993](https://github.com/Digital-Process-Tools/claude-supertool/issues/993)).
- **The explicit target list is worktree NAMES, not paths** ([#1246](https://github.com/Digital-Process-Tools/claude-supertool/issues/1246)). `oss_train:862,860` names two directory entries under `wt_root`; anything holding a path separator, anything absolute, and anything whose resolved join lands outside the root is refused by name, above the header, before a single `git fetch`. The `all` path always had this contract — `discover()` can only return names it read out of the root — and the explicit path had none, so `oss_train:/some/other/repo` rebased and force-pushed that repository. Both lists go through the one `check_target()` now. The containment half is the load-bearing one: a symlink inside the root is a plain name with no separator in it, and only resolving the join can see through it. Note the boundary is `wt_root()`, not the CWD — the core's `_containment_error` would have passed `$PWD/seed`.
- **One `realpath` per target, and the path that was checked is the path that is used.** `check_target()` returns `(verdict, path)` and `main()` acts on what it was handed. The first fix resolved twice — once to decide, once to act — and the round-2 audit walked through the gap: `st-wt/2 -> st-wt/benign` at launch, relinked to an outside repo while target `1`'s fetch was in flight, and that repo was rebased. For target k the window is every train before it. **The fragment, the docstring and the commit subject all claimed the property while the code re-derived**, which is the shape worth remembering: an artifact asserting a property is what stops the next reviewer looking for it. A test counts the resolutions now, so the claim and the code fail together.
- **`dry` stops above the rebase, not below it** ([#910](https://github.com/Digital-Process-Tools/claude-supertool/issues/910)). It used to perform the rebase and skip only the push while the header said `DRY RUN`; those worktrees hold live agents, so the safe-sounding flag moved `HEAD` underneath somebody's work. The `BUSY` guard sees *uncommitted* changes only, so an agent between two commits reads as idle — which is why the preview is not allowed to buy "would it conflict?" with someone else's `HEAD`.

Each branch is labelled by the branch `git symbolic-ref` reports, never by the worktree directory: `st-wt/749` holds `lane-watch`, and every follow-up command a reader would run takes a branch name.

`tests/test_oss_train_containment_1246.py` covers the target contract, and it is the one file here that does exercise a rebase — against a throwaway repo built inside `tmp_path`, so its red is the real destructive behaviour (`the local branch was rebased in a repository outside wt_root`, with the two shas) rather than an exit code standing in for it. `tests/test_oss_train_1216.py` covers the argument parsing, the refusals, the `BUSY` guard, the read-only `dry` path and the reading of `git-push`'s verdict, and names what it does not cover — the rebase, the `git-resolve` refusal and the push itself cannot be exercised without force-pushing real refs, so no fixture pretends to.

**The verdict is read as a prefix, and that is not a style choice.** `"PUSHED" in verdict` is true of `NOT PUSHED - REJECTED`, `- UNVERIFIED`, `- REBASE PAUSED`, `- TIMED OUT`, `- already up to date` and `- no push attempted` — every failure `git-push` emits. The op that exists to relay a verified verdict instead of assuming its own success ended on a membership test that tallied all six as `PUSHED`, with a detail line reading "NOT PUSHED - ..." next to the tick. `classify_push` is a separate function for the same reason: it is the one part of the push path that can be tested without a remote.

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
      "safety": "read-only",
      "description": "Check deployment status for a service.",
      "syntax": "deploy-status:SERVICE",
      "example": "deploy-status:api-gateway"
    },
    "deploy-log": {
      "cmd": "python3 {path}mytools/log.py {arg}",
      "safety": "read-only",
      "description": "Print a captured deploy log.",
      "syntax": "deploy-log:PATH",
      "paths": { "args": [1], "root": "cwd" },
      "example": "deploy-log:logs/api-gateway.log"
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

## Changelog fragments

**Do not edit `CHANGELOG.md` in a pull request.** Add a file to `changelog.d/` instead:

```
changelog.d/906.added.md
changelog.d/895.fixed.md
changelog.d/878.fixed.second-entry.md
```

`<issue>.<section>[.<slug>].md`. The section is one of the Keep a Changelog headings — `added`, `changed`, `deprecated`, `removed`, `fixed`, `security` — and the optional slug lets one issue file two entries in one section. The content is the entry exactly as it would appear under that heading: a `- **Bold summary** ([#906](link)). Prose.` bullet, plus as many indented paragraphs as the change earns. Nothing is reformatted at assembly.

**The entry must name its own issue, and the check refuses it if it does not** ([#1251](https://github.com/Digital-Process-Tools/claude-supertool/issues/1251)). `#1251` anywhere in the body satisfies it, as does a link whose URL ends in the number — the canonical `- **Bold summary** ([#1251](link)).` opening does both. This is the only content rule that is about *meaning* rather than about CommonMark, and it exists because the number lives in the filename and the release deletes the filename: assembly writes the body into `CHANGELOG.md` and nothing carries the name across. Measured on the fragments as they stood at each release commit, **8 of the 20 entries in v0.32.0 and 6 of the 28 in v0.33.0 named every issue but their own** — several citing three or four neighbours — and only two of the twenty had a `test_the_change_is_findable` to notice. The alternative considered was having the assembler append the reference itself; it was refused because it would reformat an authored entry, which is the one thing this directory promises not to do, and because an "is it already there?" test cannot tell a self-citation from a coincidence — v0.32.0's #1197 was findable only because a *different* fragment happened to mention it.

**A fragment is bullets and prose, and this is the one rule about fragment *content*.** Because nothing is reformatted, a fragment line that CommonMark would read as a heading or a link-reference definition becomes one in `CHANGELOG.md` itself: it reparents every entry below it, it is what the assembler's insertion point finds on the *next* cut, and a definition lands above the genuine link-ref block at the bottom, where the *first* definition of a label is the one that resolves ([#923](https://github.com/Digital-Process-Tools/claude-supertool/issues/923)). `--check` refuses such a fragment naming the file, the line number and the line, so it fails on the PR rather than in front of whoever is cutting the release.

**Writing our own Markdown scanner lost three times, so the guard is a CommonMark parser** ([#936](https://github.com/Digital-Process-Tools/claude-supertool/issues/936)). [#927](https://github.com/Digital-Process-Tools/claude-supertool/pull/927) anchored its patterns at column 0 and [#930](https://github.com/Digital-Process-Tools/claude-supertool/issues/930) found three bypasses; [#932](https://github.com/Digital-Process-Tools/claude-supertool/pull/932) widened them to 0-3 spaces and any label, and [#934](https://github.com/Digital-Process-Tools/claude-supertool/issues/934) found six more; [#935](https://github.com/Digital-Process-Tools/claude-supertool/pull/935) inverted to a whitelist with a positional guarantee and its own fence state machine, and #936 walked through the fence — a column-0 line inside an open fence was copied out verbatim, so `# INJECTED HEADING` and an `[Unreleased]:` definition both landed at column 0 of the released file under a receipt that said `ok`. Every one of those bypasses is the same shape: **our scanner disagreed with CommonMark**, about leading spaces, about setext, about fence state, about info strings. So `--check` now parses each fragment with `markdown-it-py` and refuses a heading, a link-reference definition or raw HTML **at any depth**, a fence that does not close inside the fragment, and a top level that is not a single `-` bullet list. The guard and the reader are one parser, and the receipt says which parser and which version made the claim.

**The release re-parses what it is about to write.** A second layer, deliberately independent of the guard above, because one guard over this file has now been wrong three times running: the assembled document's heading table must be the old one plus exactly the headings the assembler reports emitting, its link-reference table the old one plus exactly the definitions it reports writing, and it may gain no raw HTML. If not, it refuses and `CHANGELOG.md` is untouched. This also catches a corruption no fragment causes — an `[Unreleased]:` definition anywhere above the block at the bottom wins, first-definition-wins, so a release could rewrite the bottom block, report `links [Unreleased] → compare/vX...HEAD`, and ship a file where that link goes elsewhere.

**No parser means `skipped`, not `ok`.** `markdown-it-py` is a dev dependency and the assembler imports it. Without it the script has established nothing, so it says so, exits non-zero and writes nothing — there is no text-scanning fallback, because three of those shipped and all three were bypassed within one audit. `.github/workflows/changelog.yml` installs it; the job used to be a checkout and a bare `python3`, which is worth knowing before assuming a CI leg has the dev extras.

**The same guard runs when you *write* the fragment, not only in CI** ([#1132](https://github.com/Digital-Process-Tools/claude-supertool/issues/1132)). A fragment is written by an ordinary `paste` or `edit`, so for a long time nothing in the write path looked at its content: the receipt said `git-status : ok` and the first thing to disagree was a 20-leg matrix twenty minutes later — PR #1115 went red on 14 of those legs over one missing `- `. The `changelog-fragment` validator now fires on any mutating op under `changelog.d/`, and it does not restate the rule: it imports this script and republishes `parse_fragment_name` / `scan_fragment_body`'s own messages, so the write-time verdict and the CI verdict are the same sentence by construction. It is a validator like any other, so `supertool 'validate:changelog.d/<file>:verbose'` prints a truncated message in full, and no parser still means `skipped`.

**Put a quoted heading in a fenced code block at the bullet's own indent — not four spaces, and close the fence at that indent too.** Entries here quote headings constantly ([#839](https://github.com/Digital-Process-Tools/claude-supertool/issues/839) is one), and until #934 both this page and the refusal message said to indent by four. That advice was an injection: CommonMark's four-column code-block threshold is relative to the containing block's content column, a `- ` bullet's is 2, so four spaces inside a fragment is two relative columns — a live paragraph, where a heading is a heading and a definition resolves. Verified with a real parser inside a bullet rather than reasoned about at the top level of a document, which is where the mistake came from: a definition is live at 2, 4, 5 and tab indent, and the threshold is 6. The closing-fence half is #936: a code block takes no lazy continuation, so any line of the block that reaches column 0 — the closer included — ends the fence, the bullet and the list, and what you were quoting goes live at document level.

**A bullet may open with a link.** `- [#123](url) fixed the thing.` and a wrapped continuation line beginning with one are ordinary entries, refused between #932 and #936 because the guard treated a bare `[` as the start of a possible link-reference definition. An inline link can never be one. The 360-entry corpus pin did not catch it: the shipped file happens to contain no entry of that shape, so the corpus never exercised the rule it was taken to vindicate.

**Why, measured.** 55 of the last 60 merged PRs touched `CHANGELOG.md`, all of them at the top of the same 2,670-line file. With N open PRs each merge re-conflicts the other N-1, so an eight-PR release cycle pays closer to eighteen rebase-and-resolve rounds than eight ([#906](https://github.com/Digital-Process-Tools/claude-supertool/issues/906)). Two PRs never touch the same path in `changelog.d/`, so the conflict class disappears rather than being merged around.

**`merge=union` is not the shortcut it looks like.** A one-line `.gitattributes` entry would make git union the file automatically — and `tests/test_git_resolve_heading_dup_839.py` exists because that is wrong on this document. Whenever the union emits the same heading twice — both sides of one hunk (#839), or one side plus the surrounding context git had already merged ([#911](https://github.com/Digital-Process-Tools/claude-supertool/issues/911)) — every line between the two copies is reparented under the first, and unreleased work reads as shipped. A correctness bug in the changelog, reported as `markers: clean`.

**CI asks for a fragment, and says when it does not.** `.github/workflows/changelog.yml` requires one from any PR touching `supertool.py`, `_supertool.py`, `presets/`, `validators/`, `formatters/`, `notifiers/` or `.claude-plugin/`. Docs-only and tests-only PRs are exempt by design — requiring a fragment for a typo fix is how the discipline decays into a reflex-added empty file — and a `no-changelog` label is the stated way out for anything else. The job prints which state it landed in on every run.

**A fragment that is *deleted* does not satisfy the gate** ([#925](https://github.com/Digital-Process-Tools/claude-supertool/issues/925)). `git diff --name-only` lists a deletion identically to an addition, so a PR that changed the core and removed somebody else's pending fragment used to pass — announcing nothing, and dropping an already-approved entry from the next release. Additions and deletions are read apart now, by two diffs rather than by one `--diff-filter` flag: restricting the single diff would also stop the user-visible-paths read seeing a *deleted* preset, and a preset that goes away is as user-visible as one that arrives. A release cut deletes fragments and adds none, so it is a stated pass — `ok (release: N fragment(s) assembled into CHANGELOG.md)` — while a deletion with no `CHANGELOG.md` beside it is a finding that names what it lost. **The deletion check sits above the user-visible-paths gate, and the ordering is load-bearing:** losing an entry needs no code change to go with it, so `git rm changelog.d/906.added.md` on its own would otherwise leave the paths read empty and report `skipped (nothing to announce)`. That test is a claim about the diff, not a proof the entries survived; the assembler's own entry-balance refusal is what proves that, where the file is written.

**Nothing outside `changelog.d/` may name a pending fragment by path — and "assert it exists" is only the commonest spelling of that.** `assert (root / "changelog.d" / "1053.added.md").is_file()` looks completely reasonable and passes for as long as no release happens — then the release *consumes* the fragment and the test reddens every leg on the release commit, blocking the cut with a failure that says nothing about the cut. It shipped three times ([#941](https://github.com/Digital-Process-Tools/claude-supertool/issues/941) took five legs on v0.26.0, [#953](https://github.com/Digital-Process-Tools/claude-supertool/issues/953) thirteen of twenty on v0.27.0, [#1053](https://github.com/Digital-Process-Tools/claude-supertool/issues/1053) is the third). What such a test actually claims is that the change is *findable*, and a pending fragment and a released `CHANGELOG.md` entry both satisfy that — exactly one is true at any moment. Write it as one call:

```python
from _changelog_findable import assert_change_is_findable

def test_a_changelog_fragment_exists() -> None:
    assert_change_is_findable(1053)
```

`tests/test_changelog_findable_1053.py` additionally parses every module of the suite and refuses any `assert` that tests a `changelog.d` path's existence — directly, or through a local name bound to such a lookup — naming the file, the line and the accepted form. It reads the asserted expression and never the failure message, because the accepted form names `changelog.d/<n>.<section>.md` in its own message.

**The syntax was never the class, which a fourth instance proved** ([#1231](https://github.com/Digital-Process-Tools/claude-supertool/issues/1231), [#1293](https://github.com/Digital-Process-Tools/claude-supertool/issues/1293)). `tests/test_ops_roster_1231.py` held its fragment in a module-level tuple of swept paths and resolved it with a `read_text` in a loop — no `assert`, no existence call, so the AST detector was blind to it by construction, and the v0.33.0 release commit went red on 13 of 22 legs for a reason unrelated to what the test guarded. `tests/test_changelog_findable_1293.py` closes the general case from the other end: it asks which fragments are on disk *right now* and refuses any tracked text file that names one, in any language and by any syntax — a doc example, a workflow comment, a jit-context citation, a fixture. Naming an *already consumed* fragment stays fine and is common: 185 lines across 19 tracked files do it, every one correct, because nothing the next tag deletes is called that. The match carries a left digit boundary, because `1.added.md` is a substring of `21.added.md`. When there is no pending fragment, or when git cannot list the checkout, it **skips** rather than passing — a scan that looked at nothing must not read as a clean sheet.

### Cutting a release

The release edits **five** files, and `test_plugin_manifest_version_matches_code`, `test_pyproject_version_522`, `test_the_newest_release_section_is_the_version_that_ships` and `test_readme_version_badge_matches_code` guard all five against each other:

1. `.claude-plugin/plugin.json` — `version`
2. `_supertool.py` — `VERSION`. Not `supertool.py`: since [#931](https://github.com/Digital-Process-Tools/claude-supertool/issues/931) that is an 80-line entry-point shim and the constant lives with the code. Grep the constant rather than a line number.
3. `pyproject.toml` — `version`
4. `README.md` — the version badge. **This section said "four files" and omitted it until v0.34.0**, and the badge is the site that rotted: it read `0.14.1` while the tool shipped `0.29.0`, fifteen releases stale, hyperlinked to the very file it disagreed with. `test_readme_version_badge_matches_code` fails on an **unmatched pattern** rather than passing, because a regex that found nothing has not checked the badge.
5. `CHANGELOG.md` — **written by the assembler, not by hand:**

```bash
python3 .github/scripts/assemble_changelog.py --version 0.26.0 --dry-run   # read it first
python3 .github/scripts/assemble_changelog.py --version 0.26.0
```

It inserts `## [0.26.0] - <today>` above the newest existing release, folds every fragment in under its Keep a Changelog heading in spec order, rewrites the `[Unreleased]` compare link, adds the new tag's link ref, and deletes the fragments it consumed. `--keep` leaves them; `--date` overrides today.

**Where "above the newest existing release" is, and where the link refs are, are read off a parse** (#936). Both used to be line-prefix matches, and this page tells you to quote headings in fenced blocks, so `CHANGELOG.md` contains lines that look like release headings and like link-ref definitions and are neither. A cut against one inserted the release section between an opening fence and the heading it was quoting; the link-ref walk stopped on a closing fence, found nothing, and advanced no link while reporting that it had left them alone.

**It has three outcomes and states which one it took**, every run — `ok` naming every fragment it consumed, `skipped` when `changelog.d/` is empty (exit 1: nothing was assembled, and a release tool that exits 0 there has reported "released" for "nothing to release"), or `refused` (exit 2) naming the file it will not guess about. A filename that does not parse is refused rather than skipped, because a fragment the release quietly passed over is an entry that never ships and that nobody is told about.

**`## [Unreleased]` is `changelog.d/` now.** The heading stays in `CHANGELOG.md` — the `[Unreleased]:` compare link points at it — but nothing accumulates under it any more. Anything that *is* under it when a release is cut (the entries that predate this mechanism, or a hand-edit somebody made anyway) is **folded into the release being cut**, above the fragment-derived entries, with same-named `###` subsections merged rather than duplicated. `[Unreleased]` means "goes out in the next release", so it goes out in it: leaving it behind would ship a tag that silently omits real work while that work still reads as pending.

Nothing is trusted about that merge. The assembler counts the entries on both sides, counts the entries it produced, and **refuses to write** if the two do not balance — a merge that dropped a line would otherwise be indistinguishable from a clean run. The receipt says how many it folded, including when the answer is zero.

**A release section ends with a blank line, and the re-parse now refuses one that does not** ([#1113](https://github.com/Digital-Process-Tools/claude-supertool/issues/1113)). `render` builds its section ending in a blank line and the splice passed the joined text through `str.splitlines()`, which drops the empty field a terminal newline produces — so the last body line landed directly against the `## [x.y.z]` heading below, on 0.25.0 through 0.29.0. CommonMark lets an ATX heading interrupt a paragraph, so GitHub rendered it as a heading anyway and nothing looked wrong; a stricter or older parser folds it into the paragraph before, in the one artefact users read to decide whether to upgrade. Only *new* instances are refused: the ones already in the file shipped inside tags and GitHub release notes, and rewriting them would make `CHANGELOG.md` stop matching what was published.

**The link-ref table at the bottom is audited on every pull request**, not at release time ([#918](https://github.com/Digital-Process-Tools/claude-supertool/issues/918)):

```bash
python3 .github/scripts/assemble_changelog.py --check-links
```

The assembler writes one definition per cut, which keeps the *next* release honest and says nothing about the state it inherited — `[0.24.0]` and `[0.25.0]` shipped with no definition at all, so those headings rendered as literal bracketed text, and `[Unreleased]` sat comparing from `v0.23.0` while two tagged releases had shipped, which is a link that resolves, returns a real diff, and shows released work as pending. So the audit reads the whole table: every `## [x.y.z]` heading has a definition, `[Unreleased]` compares from the newest section, and no definition names a version the file does not document.

**A version that was never tagged is the third state, and it is declared rather than inferred.** `0.11.0` and `0.14.0`–`0.19.0` have sections here and no tag anywhere, so there is no release page to link to, and a `releases/tag/vX.Y.Z` invented for one is a 404 that renders as a working link. They are listed in `assemble_changelog.UNTAGGED_RELEASES`, the list is audited too — a declared version that *has* a definition, or that is not in the file, is a finding — and `tests/test_changelog_link_refs_918.py` refuses anything from 0.20.0 on being added to it, so the declaration cannot become somewhere a real regression gets filed away.

`test_the_newest_release_section_is_the_version_that_ships` closes the fourth file into the ring above: the newest `## [x.y.z]` section must be `supertool.VERSION`, so a bump that moves three files and not this one is red.

**Counting what is pending** is a file count, not a grep: `python3 .github/scripts/assemble_changelog.py --count` prints a bare integer, and refuses if any name would fail to assemble. Counting `- **` lines under a heading answered a question about line prefixes and was read as an answer about pending work.

---

## Helper script conventions

- **Python stdlib preferred.** No third-party dependencies. If an op needs `requests`, reconsider the design.
- **Exit 0 on graceful skip — for preset scripts, and only those.** If the required CLI tool is missing, print a friendly message and exit 0. Don't fail the whole supertool call because `kubectl` isn't installed. **A validator does the opposite of the friendly message**: it emits `refusal.absent(TOOL, file, reason, dur_ms)` and says nothing on stderr, because a stderr note beside an `ok: true` payload is invisible to the row, the before/after delta, `rollback_on_fail` and CI. This bullet read as license for the other thing in ten adapters at once ([#1202](https://github.com/Digital-Process-Tools/claude-supertool/issues/1202)); see [validators.md](validators.md), "The tool is not installed".
- **Validators output JSON.** See [validators.md](validators.md) for the exact schema. Other scripts can output anything — supertool passes it through as-is.
- **One file per op.** `gitlab/issue.py`, `gitlab/mr.py`, `gitlab/pipeline.py` — not one monolithic `gitlab.py` with a dispatch table.
- **Scripts are co-located with their preset.** `presets/mytools/status.py`, not `scripts/status.py`. The `{path}` placeholder makes this work without hardcoded paths.
- **Never call `urllib.request.urlopen` directly.** Use `urlopen()` from [`presets/_http.py`](../presets/_http.py). See below — this one is enforced by a test.
- **Never call `.read()` on a response.** Use `read_capped()` from the same module. Also enforced by a test.
- **Never call `urlretrieve`.** It is a third door onto the same default opener — no redirect guard, no cap, no deadline, no destination policy. Use `download()` from the same module. Enforced by a test, added after [#817](https://github.com/Digital-Process-Tools/claude-supertool/issues/817) came through that door.
- **Never fetch a URL a user, an issue or an API response chose without a destination policy.** See "Fetching a URL somebody else chose" below.

### HTTP requests go through `presets/_http.py`

`urllib.request.urlopen` uses the default global opener, whose `HTTPRedirectHandler` rebuilds a redirected request stripping exactly two headers — `content-length` and `content-type` — and carrying everything else, including `Authorization`, `api-key` and `Cookie`, to whatever host the `Location` names. `http_error_302` additionally permits an `https` -> `http` downgrade. A server answering `302 Location: http://attacker.example/` therefore receives the caller's live credential, and because the redirect is followed transparently, its response body comes back to the caller as though the real API had answered it ([#691](https://github.com/Digital-Process-Tools/claude-supertool/issues/691)).

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _http

from _http import (  # noqa: E402
    DeadlineExceeded, RedirectRefused, ResponseTooLarge, read_capped, urlopen,
)

try:
    with urlopen(req, timeout=timeout) as resp:
        body = read_capped(resp)
except RedirectRefused as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
except (ResponseTooLarge, DeadlineExceeded) as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
except http.client.HTTPException as e:          # IncompleteRead is NOT an OSError
    print(f"ERROR: incomplete response: {e}", file=sys.stderr)
    sys.exit(1)
```

`_http.urlopen` follows a redirect only when it stays on the same origin — `(scheme, host, port)` with default ports normalised and the host compared case-insensitively — plus the one asymmetry of an `http` -> `https` upgrade on the default ports, which moves the credential onto a *more* protected channel. A different host, a different port on the same host, a downgrade, a non-HTTP scheme or an unparseable port all raise `RedirectRefused`.

Three rules about the refusal:

1. **Catch it explicitly and print it.** `str(exc)` names the origin, the status code, the attempted destination and the reason. Returning the pre-redirect state quietly is the false-success defect wearing a new coat.
2. **It is deliberately not an `OSError`.** A blanket `except OSError` or `except urllib.error.URLError` must not absorb a credential-exfiltration attempt into a generic "network error", and a `..._safe()` helper whose contract is "returns `None` on any error" must not turn it into a silent `None`. Where such a helper exists (`hashnode._graphql.gql_safe`, `bluesky._atproto.refresh_session`), the refusal is a documented carve-out that exits instead.
3. **Ordering matters.** Put `except RedirectRefused` *before* the broad handlers.

**Testing one of these: the seam is `_http._OPEN`, and nothing else works.** `_OPEN` is bound to the opener once at import and never looked up on `urllib.request` again, so `monkeypatch.setattr(mod.urllib.request, "urlopen", ...)` replaces a name no preset calls. It raises nothing — the stub is simply ignored and the request goes to the live host, which is why two tests in `test_security_error_echo_691.py` spent months contacting `bsky.social` and `dev.to` on every CI leg and passing on whatever came back ([#1312](https://github.com/Digital-Process-Tools/claude-supertool/issues/1312)). The bluesky one asserts that a password *echoed by the remote* is redacted, and the echo it injects had never been delivered.

```python
monkeypatch.setattr(sys.modules["_http"], "_OPEN", lambda req, timeout=0: _Resp())
```

The same trap one layer up: an op with both a `gql` and a `gql_safe` needs both stubbed, or whichever one the code path under test actually calls stays live.

**Arm `block_outbound` so a missed stub fails at the socket.**

```python
from _netblock import block_outbound

@pytest.fixture(autouse=True)
def _no_outbound(monkeypatch: pytest.MonkeyPatch) -> None:
    block_outbound(monkeypatch)
```

`tests/_netblock.py` refuses any non-loopback `connect` or `getaddrinfo` and names the host and what to stub. Loopback and `AF_UNIX` stay open, so `test_http_bounds.py` and the `claude-channel` suites are unaffected — those bind their own servers and the process under test is the one that answered.

Why this is a rule and not a nicety: a live call makes the leg a statement about somebody else's DNS and redirect policy. #1312 was filed off a red on PR #1302, whose diff was one test file and one markdown table; Hashnode had started answering 301, `_http.urlopen` correctly refused the off-origin hop, and the test rendered that correct refusal as a product defect.

### Fetching a URL somebody else chose

Everything above governs a request to a URL *this repo* chose — the four API clients hold their base URLs as constants, and the only untrusted input is where a redirect points. `gh-issue` broke that assumption: it pulled `http(s)` URLs out of issue markdown and fetched them, so any comment containing

```
![x](http://169.254.169.254/latest/meta-data/iam/security-credentials/role)
```

made a developer's machine read its own cloud metadata service, write the body to `/tmp/supertool-images/gh/N/`, and print the path under `## Images` for an agent to open ([#817](https://github.com/Digital-Process-Tools/claude-supertool/issues/817)).

**Adopting `urlopen()` would not have fixed it.** The metadata URL is the *first* hop, and the only guard `_http.py` had was a same-origin rule on redirects. There was no destination policy to adopt. So there is now a second one, deliberately **not** applied to `urlopen()` — turning it on globally would break every loopback test in this repo and every self-hosted endpoint, and the constant-URL clients do not need it:

```python
from _http import DestinationRefused, download  # noqa: E402

try:
    download(
        url, local_path,
        allowed_hosts=("github.com", ".githubusercontent.com"),
        limit=8 * 1024 * 1024,
        content_types=("image/",),
    )
except DestinationRefused as e:
    ...   # a decision: report it, with the reason
except (OSError, http.client.HTTPException) as e:
    ...   # a failure: report it, as a different thing
```

`download()` checks scheme and host allowlist first — pure, no DNS, no packets, because a probe that *reaches* the metadata service has already told the attacker the machine can see it — then resolves and checks every returned address, then connects with a redirect handler that re-runs the allowlist per hop, then the `Content-Type`, then `read_capped`, and only then writes. A refusal leaves nothing on disk, not even the directory.

**Use an allowlist of hosts, not a denylist of address ranges.** A denylist has to be complete to be worth anything — `169.254.169.254` *and* `169.254.170.2` *and* `100.64.0.0/10` *and* `fd00::/8` and whatever IANA reserves next — and after all that it still permits every public URL on the internet, so the input is still a way to make somebody's machine make a request. The address check is the second layer, for an allowlisted *name* that resolves somewhere it should not.

**Three things it does not cover, and they are in the docstring rather than here so they cannot drift from the code:** DNS rebinding between the resolve and the connect; an `http_proxy` that makes the address checks decorative; and the bytes themselves, which are attacker-chosen content from an allowlisted host and are the caller's problem. A half-guard described as a guard is worse than none.

**Two ends of the same rule: say when you decline.** `DestinationRefused` is not an `OSError`, for the same reason as `RedirectRefused` — the call site it was written for read `except (URLError, OSError): continue`. And the caller must print all three outcomes. "Fetched", "refused, because —", and "tried and could not tell" are different facts with different next actions, and an image that vanishes silently reads as an issue with no image. See [validators.md](validators.md), "Declining instead of guessing".

### The body is bounded, in bytes and in wall clock

`resp.read()` has no cap, and urllib's `timeout` bounds each socket operation rather than the call, so a server that drips one byte at a time resets it forever — measured at 4.7s against `timeout=1` ([#766](https://github.com/Digital-Process-Tools/claude-supertool/issues/766)). Unbounded body plus unbounded wall clock is the whole slowloris shape, so `read_capped()` closes both.

1. **The cap is a refusal, not a truncation.** Over `_http.MAX_RESPONSE_BYTES` (10 MiB) raises `ResponseTooLarge` and returns nothing. Handing back the first N bytes of a JSON body produces a `JSONDecodeError` that reads as "bad JSON from the endpoint", which sends the reader to the wrong file. Pass `limit=` if you know your call site better than the default does.
2. **`ResponseTooLarge` is not an `OSError`, and `DeadlineExceeded` is.** A body past the cap is a statement about the endpoint and must not vanish into a `..._safe()` helper's `None`; a slow endpoint is exactly the failure those helpers exist to degrade past, so `DeadlineExceeded` subclasses `TimeoutError` and needs no handler of its own there.
3. **The deadline needs no new argument.** It defaults to `_http.DEADLINE_FACTOR` (4) × the `timeout` you already pass, so a new call site inherits a bound without asking for one.
4. **A short body raises.** A response ending before its declared `Content-Length` raises `http.client.IncompleteRead` with an **empty** partial. The bytes that arrived are not a smaller answer; they are the start of an answer that never came.
5. **Catch `http.client.HTTPException`.** `IncompleteRead` subclasses it, **not** `OSError`, so it walks past `except OSError` and `except urllib.error.URLError` alike. That is how it used to propagate out of `gql_safe`, whose docstring promised it never raised.

What the deadline does **not** cover: urllib gives no way to interrupt the request-line and header phase, so a server dripping *headers* forever is still bounded only by the per-socket-operation `timeout`. The deadline catches it at the first opportunity afterwards. That is a partial bound and is documented as one — closing it needs a socket layer `_http` does not have.

`tests/test_http_bounds.py::test_no_unbounded_response_reads_remain_under_presets` fails the build on any argument-less `.read()` under `presets/` on a name bound from `urlopen` or an `HTTPError`, for the same reason the bare-`urlopen` test exists: a guard that is written but not wired at every call site is this repo's most frequently repeated defect. Error bodies are the one deliberate exception — they are truncated rather than refused, with `e.read(ERROR_BODY_BYTES)`, because they are already cut to 200-500 characters for display and are never parsed, so a short read has nothing to misdiagnose.
4. **A permitted redirect is disclosed too, and you get that for free.** `_http.urlopen` prints a `NOTE: the request was redirected before it was answered: ... -> ...` line to stderr whenever the final URL differs from the one you asked for. Allowing a hop is not the same as saying nothing about it — the caller asked one URL a question and a different URL answered, and every value you return below that line describes the second one. dev.to answers `/settings` with a same-origin 302 to `/enter` as soon as the session cookie expires, which is exactly how `fetch_csrf_token` came to report a layout change when the real cause was a dead cookie. Both URLs are printed with `repr`, because the destination is remote-controlled text on its way to a terminal.

`tests/test_security_redirect.py::test_no_bare_urlopen_call_sites_remain_under_presets` fails the build on any `urllib.request.urlopen(` left under `presets/`. That test exists because a guard that is written but not wired at every call site is this repo's most frequently repeated defect — the protection has to be inherited, not re-earned per integration.

---

## Text encoding

Three rules, all enforced by `tests/test_encoding_seam.py`. They exist because four separate defects came out of this seam one at a time ([#400](https://github.com/Digital-Process-Tools/claude-supertool/issues/400), [#415](https://github.com/Digital-Process-Tools/claude-supertool/pull/415), [#418](https://github.com/Digital-Process-Tools/claude-supertool/issues/418), [#431](https://github.com/Digital-Process-Tools/claude-supertool/pull/431)) — each found only once the previous one was fixed, and every one of them on a platform the author was not sitting on.

**1. Every text read and write names its codec.**

```python
path.read_text(encoding="utf-8")          # yes
open(path, encoding="utf-8")              # yes
open(path, "rb")                          # yes — binary decodes nothing
path.read_text()                          # no  — decodes with the locale
```

Without `encoding=`, Python decodes with `locale.getpreferredencoding()`: **cp1252** on a Windows console, **ASCII** under the C/POSIX locale that a great many cron jobs, containers and CI runners default to. Any file holding a `—` or a `✓` — which includes `presets/git.json`, shipped in this repo — then raises `UnicodeDecodeError`. A static AST scan over `supertool.py`, `_supertool.py`, `presets/`, `hooks/`, `validators/`, `formatters/` and `notifiers/` fails the suite on a new one and names the file and line.

The scan **declines a call that names a keyword `open` and `Path.open` do not declare** ([#766](https://github.com/Digital-Process-Tools/claude-supertool/issues/766)) — `opener.open(req, timeout=30)` opens no file, so `timeout=` is proof rather than a hint. Nothing else about the receiver is inferred: every keyword those two signatures *do* declare still counts, and `p.open(**kw)` still counts, because the names are unknown at parse time and the unknown answer is the one that flags.

**The read half of the rule applies inside `tests/` too** ([#461](https://github.com/Digital-Process-Tools/claude-supertool/issues/461)). A test that reads a real repository file as data — source, docs, a manifest — decodes whatever non-ASCII the project has accumulated, so it passes on your machine and dies on the Windows leg the day that file acquires an em dash. That happened twice in one day ([#431](https://github.com/Digital-Process-Tools/claude-supertool/pull/431) scanning preset source, [#460](https://github.com/Digital-Process-Tools/claude-supertool/pull/460) reading `docs/presets/watch.md`), both in tests written after the rule existed.

The scan makes **no attempt to tell a repository path from a `tmp_path` fixture**, and that is deliberate: #431's read was `path.read_text()` on a *function parameter* fed from `PRESETS_DIR.rglob()` in another function, so any target-based narrowing is interprocedural or it is silently incomplete — and a guard with invisible false negatives is worse than a blunt one. So **every** read in `tests/` names its codec, including reads of files the test itself just wrote:

```python
assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "x"   # yes
assert (tmp_path / "out.txt").read_text() == "x"                   # no
```

Fixture **writes** stay out of scope. That is where the ~1670 bare calls live, and a `write_text()` of an ASCII fixture cannot fail — enforcing there is the noise that made the original `tests/` exclusion correct.

One thing this scan does not catch, worth knowing: **a read reached only on a platform you are not on.** The scan is static, so it enumerates every call site rather than every executed one — that is the half that keeps working for code added later, but it says nothing about which branch runs where.

**2. Every decoding `subprocess` call names its codec *and* its error handler.**

```python
subprocess.run(cmd, capture_output=True)                                        # yes — bytes, nothing decoded
subprocess.run(cmd, capture_output=True, text=True,
               encoding="utf-8", errors="replace")                              # yes
subprocess.run(cmd, capture_output=True, text=True)                             # no  — locale codec, strict handler
subprocess.run(cmd, capture_output=True, text=True, errors="replace")           # no  — codec still comes from the locale
subprocess.run(cmd, capture_output=True, encoding="utf-8")                      # no  — handler still strict
```

Both halves or neither, because they are two different bugs sharing one line ([#501](https://github.com/Digital-Process-Tools/claude-supertool/issues/501)). The **strict handler** kills the op outright on the first byte that is not valid UTF-8: `git merge-tree` writes conflicting blob content to stdout, so one conflicted PNG took `gl-mr` down mid-render ([#498](https://github.com/Digital-Process-Tools/claude-supertool/issues/498)). The **missing codec** is rule 1's bug at a seam rule 1 never covered — `subprocess._text_encoding()` reads the locale, so a `C`-locale runner mangles accented paths, branch names and commit messages without anything crashing at all.

There is no "this one only reads git porcelain, so it is ASCII" exemption. `status`, `diff --name-status`, `diff --stat` and `log --format` all embed paths, branch names, author names and commit messages, and latin-1 commit messages are ordinary in older repositories. Only hashes, counts and `--version` are genuinely safe, and pinning them costs nothing.

**`errors="replace"` is the default answer, not the universal one.** It makes a decode failure *silent* — mojibake rendered as though it were content. Where supertool merely displays the output that is the right trade, because the alternative is a traceback landing after half the answer is already on screen. Where the decoded text becomes **something else** — bytes written back into a user's file, a path handed to the filesystem — it is the wrong trade, because it converts a crash into a wrong answer. Those seams decode with `replace` and then check `_undecodable_at()` for U+FFFD, refusing and naming what happened rather than proceeding. The vim shell verbs (`:!`, `:%!`, `:r !`) and the `git diff --cached -z` path lists in `validate_staged` / `format_staged` are the current members of that set; if you add a call whose output ends up on disk or in an `os.path` call, it belongs there too.

**The scan declines rather than guessing.** `subprocess.run(cmd, **opts)` and `text=some_flag` cannot be judged syntactically, so they are reported as unreadable instead of being counted clean — see `test_the_subprocess_scan_declines_rather_than_guessing`. Spell the kwargs out literally at the call site. Known blind spots, stated so the green reads correctly: an aliased import (`from subprocess import run as _r`), a call built through `getattr`, and `tests/` — which holds 125 further violations and is out of scope, because a test that mis-decodes its own fixture output fails loudly on a runner rather than silently in a user's hands.

**3. A preset that prints non-ASCII must not depend on the console encoding.**

Presets run as separate processes and inherit none of supertool's stream setup. Supertool pins `PYTHONIOENCODING=utf-8` for every child it spawns, so an op invoked through supertool is covered without the script doing anything. A `presets/git/*.py` script is additionally required to call `use_utf8_stdout()` as the first statement of `main()`, because those are the ones run straight from a shell during conflict work, with no supertool in front of them. It lives in `presets/git/_git_common.py` — one definition, never a copy.

The failure this prevents is worth naming: printing a `✓` on a cp1252 console raises `UnicodeEncodeError` and kills the process *after* the work is done, so a commit that succeeded reports as a crash — and the operator runs it again.

**Testing this without a Windows runner.** Both halves reproduce on Linux and macOS, which is the whole point:

```bash
# read half — a bare open() becomes a hard error (3.10+)
python3 -X warn_default_encoding -W error::EncodingWarning supertool.py read:file.py

# note: this does NOT work as a whole-suite mode. `PYTHONWARNDEFAULTENCODING=1
# pytest -W error::EncodingWarning` errors 4192 of 4224 tests, almost all of
# them from `subprocess.run(text=True)` in the autouse conftest fixture rather
# than from any read a test wrote. See #461.

# stdout half — the Windows console default, anywhere
PYTHONIOENCODING=cp1252 python3 supertool.py git-status

# config decode — a C locale, with PEP 538/540 coercion disabled so it stays ASCII
LC_ALL=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 python3 supertool.py git-status
```

**4. CI's own emitters are pinned per process, never through the environment** ([#546](https://github.com/Digital-Process-Tools/claude-supertool/issues/546)).

Rules 1–3 are about supertool. This one is about the workflow that tests it, and it exists because a *readable* Windows failure is the only thing that makes the Windows legs worth having. Pytest's terminal writer renders assertion messages onto a stream whose codec is the runner's console codepage, and an em dash — ordinary in this repo's assertion text — is not in cp437, so it left the process as U+FFFD. Destroyed at emit time; nothing downstream recovers it. Measured on `windows-latest` in runs [30485881190](https://github.com/Digital-Process-Tools/claude-supertool/actions/runs/30485881190) and [30500828589](https://github.com/Digital-Process-Tools/claude-supertool/actions/runs/30500828589), both of which also carry intact em dashes echoed from the workflow's own step names — so the log transport was never the problem.

Two mechanisms, and the choice between them is the point:

```yaml
run: python -X utf8 -m pytest ...        # yes — one process
env:
  PYTHONUTF8: "1"                        # no  — every child, for ever
```

`-X utf8` is a flag on that interpreter and is **not inherited** by subprocesses. `PYTHONUTF8`/`PYTHONIOENCODING` in a workflow `env:` are inherited by every process the suite spawns — including `tests/test_encoding_seam.py` and `tests/test_git_commit_payload_route.py`, which reproduce Windows encoding defects precisely by *leaving the environment alone*. A fix whose blast radius covers the tests that would catch its own regression is not a fix. `tests/test_ci_encoding_546.py` pins both halves, and it will fail on either variable appearing anywhere in the workflow outside a comment.

The always-run failure summary (`.github/scripts/junit_summary.py`) reconfigures its own stdout in-process for the same reason, and reads the messages out of `junit.xml` rather than off the terminal: pytest writes that file as UTF-8 whatever the console can encode, and it carries the **untruncated** message. `--tb=no` means the one-line summary is all a reader gets, and pytest elides the middle of a long one to fit the terminal width — on one real Windows failure the elided part held the word `timeout`, which was the diagnosis. `errors="backslashreplace"`, never `"replace"`: on a diagnostic the handler must disclose, and `replace` is what produced the U+FFFD in the first place.

---

## Anchored regexes

**A pattern that asks "is this whole value acceptable?" ends with `\Z`, never `$`** ([#1188](https://github.com/Digital-Process-Tools/claude-supertool/issues/1188)). Python's `$` matches at the end of the string *and* immediately before a final newline, so `re.compile(r"^[0-9]+$").match("5\n")` is a match. Every guard written that way says yes to a value nobody meant to allow, and says it silently — a trailing newline is invisible in every render of the value that follows. It reached a printed shell command once: `_refname.ordinary()` called a ref ending in a newline ordinary, and `shell_ref()` therefore returned it unquoted.

`tests/test_anchored_guards_1188.py` walks `_supertool.py`, `presets/` and `.github/scripts/` with `ast` and fails the suite on a new one, naming the file, the line and the pattern. Those are the trees that read a value from a caller, a forge or a filename; `validators/` and `tests/` are out, because those parse the stdout of an external tool line by line and `$` meaning "end of this line" is the intent there rather than the bug.

Two things it skips by construction: a pattern compiled with `re.MULTILINE`, where `$` means the end of a line and `\Z` would be wrong, and a pattern that ends in `$` without starting at `^`, which is a suffix test (`\.pem$`) where matching before a trailing newline changes nothing a caller can act on.

**A line-parser inside those trees says so on its own call** rather than in a table someone has to go and find:

```python
_SEQ_ITEM = re.compile(r"^(\s+)-\s*(\w+)\s*$")  # anchored-ok: matched per line of a workflow file
```

The reason after the colon is required — a second test fails a bare `# anchored-ok`, because a waiver with no reason reads as a decision and is not one. Put it on the line the `re.` call *starts* on; that is the span the scan reads, so a multi-line `re.compile(` takes it on the opening line.

The scan reads the literal first argument of a call spelled `re.<something>`. Four shapes are invisible to it — a pattern built by an f-string or `+`, one assembled from a variable, one held in a dict and compiled elsewhere, and one reached through an aliased import — and none of the four exists in the tree today. It narrows the class; it does not close it. `re.fullmatch` is deliberately outside the scan, because it requires the whole string and so never had the bug.

## What CI runs, and what it does not

The pytest matrix is {ubuntu, macos, windows} × py3.9–3.12. For a long time that was the whole workflow, which meant `notifiers/claude-channel/channel.ts` — TypeScript, started in every radar session — shipped on a 12/12 green that had never executed a line of it ([#557](https://github.com/Digital-Process-Tools/claude-supertool/issues/557)). Its tests existed and were real, but they `skipif` on `shutil.which("bun")`, so they were collected, skipped, and counted as neither a pass nor a failure.

| Code | Where it runs | What is checked |
| --- | --- | --- |
| Python | all 12 pytest legs | the suite — and **no coverage floor**: every leg passes `--no-cov` |
| Python | the `coverage` job, ubuntu + py3.12 | `.github/scripts/coverage_gate.py` — floors on `_supertool.py`, `presets/`, the gate script itself and the rest of `.github/scripts/`, plus the printed inventory of what it did *not* measure |
| `notifiers/claude-channel/channel.ts` | `notifiers` job, ubuntu + macOS | `bunx tsc --noEmit` under the channel's own strict tsconfig, plus the two socket-level integration test files, run for real |
| Shell (`*.sh`, `.githooks/*`) | all 12 pytest legs | `bash -n` — **syntax only**, via `tests/test_ci_non_python_coverage_557.py` |
| `notifiers/cursor-witness/extension/src/extension.ts` | nowhere | **uncovered, knowingly** — a VS Code extension needs `npm install` of the editor's type packages to compile and an editor host to exercise |

Four things about that table are load-bearing:

- **The coverage floor is a job, not a flag on the matrix, and until [#861](https://github.com/Digital-Process-Tools/claude-supertool/issues/861) it was neither.** It lived in `addopts` as `--cov=supertool --cov-fail-under=86`, which measured one file: `presets/` — ~14k statements, the whole op surface — had no floor at all, so a preset could ship with zero tests against a green number. And because the twelve legs pass `--no-cov` (and `.githooks/pre-push` does too, deliberately), the 86% was enforced only against whoever happened to run a bare local `pytest`, while the row above this one said it ran on all twelve legs. The gate now has one owner, one number, and one place it runs.

- **The `notifiers` job does not run on Windows, and that is a decision.** `channel.ts` binds an AF_UNIX socket, so those tests skip there by platform whatever toolchain is installed. A Windows leg would install bun to report a green it had not earned — [#557](https://github.com/Digital-Process-Tools/claude-supertool/issues/557)'s defect wearing [#557](https://github.com/Digital-Process-Tools/claude-supertool/issues/557)'s fix. macOS is in because this code has already produced a macOS-specific defect: the ~104-byte `sun_path` cap, which is why the #554 harness pins `/tmp` rather than `tmp_path`.
- **`SUPERTOOL_REQUIRE_JS=1`, set by that job and nowhere else, converts a missing prerequisite into a collection error.** Without it a half-failed `bun install` would skip the tests and leave the job green, which is the whole defect reproduced inside its own fix. See `tests/_toolchain_gate.py`; the promise is deliberately *not* inferred from `CI`, because eleven of the twelve pytest legs are on CI and install no JS runtime.
- **Shell coverage is syntax, not behaviour.** `bash -n` parses `install.sh`; nothing executes it. Running an installer in CI means writing into `~/.claude`, cloning an MCP SDK and starting a server, for a script whose failure is loud and immediate on the one machine that runs it. Stated here rather than left to be inferred from a green tick.

The `.ts` inventory in `tests/test_ci_non_python_coverage_557.py` is asserted against `git ls-files`, so a new TypeScript file fails the suite until somebody classifies it as executed, uncovered or a fixture. That is the durable half of "accept the gap explicitly": a gap nobody can add to silently.

Both jobs also carry a `timeout-minutes` — 30 for `pytest`, 10 for `notifiers` — and the channel integration step runs under a `--timeout=30` per-test budget. See §"Never leave a CI job without a wall-clock budget" for the sizing and for why the two numbers differ.

---

## Running tests

```bash
python3 -m pytest tests/
```

~4000 tests. The `--cov` in `addopts` prints a term report and **enforces
nothing** — see below for why that is deliberate.

### Coverage

```bash
python3 .github/scripts/coverage_gate.py
```

One script, run by the `coverage` job in CI and by you locally, producing the
same number both places. It runs the suite with child-process attribution on,
then prints three sections and gates on the first:

| State | What it means |
| --- | --- |
| **measured, enforced** | `_supertool.py`, `presets/`, `.github/scripts/coverage_gate.py`, and the rest of `.github/scripts/` — a floor apiece that reds the build |
| **measured, not enforced** | `validators/`, `formatters/`, `notifiers/*.py` — real numbers, printed, no floor, **each with its own reason printed beside it**. `validators/` and `formatters/` are thin `subprocess` wrappers around external binaries (phpstan, prettier, hadolint) that are absent on the runner, so the figure reports the toolchain rather than the code; `notifiers/`'s Python half is two small files and the part that matters is the TypeScript, listed in the row below. One sentence covering all three would be true of two of them |
| **not measured** | `tests/` itself, the TypeScript, the shell — each named with its reason and with whatever *does* check it |

**The floors are per bucket, and a bucket is not always a directory** ([#991](https://github.com/Digital-Process-Tools/claude-supertool/issues/991)). `.github/scripts/coverage_gate.py` has a floor of its own, separate from the directory holding it, because the directory's 82.65% was `assemble_changelog.py` at 93.95% and `junit_summary.py` at 94.12% averaged with the gate itself at **46.43%** — and what the 46% covered for was `report()`, the function that decides pass from fail, which had no test at all. Matching is longest-prefix, so the file entry wins over the directory regardless of the order they are declared in.

**Nothing here is exempt for being CI plumbing.** `.github/scripts/` sat in the middle row until #991 on the grounds that flooring the script that computes the floor is a circular, and that its failure mode is a red step rather than a silent one. Neither held: the gate's coverage is produced by the test suite rather than by the gate, and the failure that actually happens is not a crash, it is the tests going away.

All three print on every run, pass or fail. A report that quietly omits a
directory is exactly the defect [#861](https://github.com/Digital-Process-Tools/claude-supertool/issues/861)
filed, and it must not come back in a new shape. `tests/test_coverage_scope_861.py`
asserts every tracked `.py` file lands in one of those buckets, discovered from
`git ls-files`, so a new preset directory reds the suite until somebody
classifies it.

**Why `addopts` no longer carries `--cov-fail-under`.** That measurement
understates this repo, badly and unevenly. 122 test modules drive a preset by
spawning it — the only honest way to test a script whose contract is its argv
and its exit status — and coverage does not follow a child process without
`parallel = true` and `COVERAGE_PROCESS_START`. In-process only,
`presets/git/diff.py` reads **9%** while its 600-line dedicated test module sits
right there; the true figure is **83.7%**. `trail.py` moves 12% → 75.6%,
`commit.py` 80% → 94.2%. A floor on the unattributed number sends contributors
to write tests that already exist, and leaves the genuinely thin files
(`git/blame.py` 14%, `git/diverge.py` 24%, `mcp/daemon.py` 46%) looking no
different from the well-tested ones. The gate script pays ~3% wall-clock to
tell those two cases apart.

**The floors are a measurement, not a target.** They sit a point or so under
what a real run produces, and they ratchet: raise them when the number rises,
never lower them to make a red go away. A gate nobody can pass gets deleted or
bypassed inside a week, and a bypassed gate is worse than an absent one — it
still prints green. The gate says so itself when a floor drifts more than three
points below the measurement.

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

Enable the pre-push hook (mirrors CI: excludes `slow` and `benchmark`,
coverage not gated):

```bash
git config core.hooksPath .githooks
```

**It does not run on every push, and which pushes it covers is the point**
([#893](https://github.com/Digital-Process-Tools/claude-supertool/issues/893)).
The hook reads the refs git hands it on stdin and gates on the *destination*:
a push to `master`/`main` pays for the suite, where a red default branch is a
shared cost nobody opted into; a push to a feature branch does not, because a
red PR branch is what PRs are for and the PR's checks run in parallel on
somebody else's machine. The measured cost it removes from a feature push is
**176.66s**, serial and blocking.

Three states, not two, and each announces itself: destination is master → run;
destination is a feature branch → skip, and say so; **stdin unreadable → run
the suite**, because "the question was never answered" must not render as
"feature branch, skip it". Being wrong that way costs three minutes; being
wrong the other way costs master.

- `PREPUSH_FULL=1 git push` — force the suite on a feature branch.
- `git push --no-verify` — the blunt instrument: skips this hook and every
  other one. Discouraged, and not the right tool for "I want the suite here".

The hook is in `.githooks/pre-push`, committed to the repo.

**Which interpreter the hook runs (#572).** Never the bare name `python3`: on Windows that name can resolve to the App Execution Alias stub, which *blocks* instead of erroring, and inside `git push` a block reads as a slow remote rather than as a broken hook. Resolution order:

1. `$PYTHON` — explicit escape hatch, used **verbatim and never verified**. `PYTHON=/path/to/venv/bin/python3 git push` is how you point the hook at an interpreter it cannot find on its own. A resolution step that could override it, or replace it after a failed probe, would hide which interpreter actually ran the suite.
2. An activated venv — `$VIRTUAL_ENV/bin/python3` (or `Scripts/python.exe`). Preferred over anything on `PATH`, because a system `python3.13` next to a 3.11 venv is a different set of installed packages, which is the POSIX half of the bug being fixed.
3. The newest `pythonX.Y` on `PATH`, from `python3.14` down to `python3.9`. Versioned names are not aliased on Windows, which is what makes them safe; the floor is `requires-python`.

Candidates 2 and 3 are **executed** before being committed to (`-c 'import sys'`), not merely looked up: `command -v` is answered by a stale symlink into a deleted venv, and believing the name there swaps a broken interpreter for a working one and reports it as a test failure. Same rule as the syntax-floor ladder above — the binary is asked, the filename is not believed.

If nothing resolves and no `$PYTHON` was set, the hook **refuses the push** and lists every name it tried. It does not fall back to the bare name, since that would restore the hang for exactly the people who have no versioned interpreter.

### Never assume the checkout has history

`actions/checkout` clones at **depth 1**, and the workflows do not set
`fetch-depth`. On CI this repo has exactly **one commit**. Any test that reads
supertool's own git history — commit counts, `git log` ranges, blame, anything a
pickaxe search walks back through — passes on a developer's full clone and fails
on eight of fourteen CI legs, with a failure that says nothing about the code it
was meant to be testing.

Build the history the test needs, in a `tmp_path` repo the test owns:

```python
@pytest.fixture(scope="session")
def history_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("history")
    ...  # git init, then commit as many times as the assertion requires
    return repo
```

Twenty-five commits cost about a second, once per session. Pair it with a guard
test asserting the fixture is actually bigger than whatever limit is under test —
otherwise the day it shrinks, the assertions it feeds go quietly green instead of
red. See `tests/test_env_knob_parsing_654.py`.

**Raising `fetch-depth` is not the fix.** It changes CI for every job in the repo
to suit one assertion, and it leaves the next such test just as free to make the
same assumption.

If a test genuinely cannot work without real history, skip it with a stated
reason — a reported skip, never a silent pass. See
[Declining instead of guessing](validators.md#declining-instead-of-guessing) for
why the distinction is load-bearing here too.

### Never assume which worker a test lands in

The suite runs under `pytest-xdist`, so the unit of process isolation is the
**worker**, not the test file. Two test files can share one interpreter, and
which two is decided by the runner's core count — a property of the machine,
never of the code.

That matters wherever the product deliberately keeps *per-process* state.
`env_int` in `presets/_env.py` says each distinct notice at most once per
process (`_ANNOUNCED`), because a knob read once per file would otherwise print
the same line ten times over the output it is warning about. In production that
is exactly right: a preset is a subprocess that reads its knobs and exits. In a
worker it means the second test to provoke a given message reads an **empty**
`capsys` and fails an assertion that describes the code correctly.

`test_github_prs.py` and `test_gitlab_mrs.py` both set
`SUPERTOOL_ENRICH_WORKERS=0` against a default of `8`, producing a byte-identical
notice. The pair shared a worker only on the macOS legs, so [#689] read as a
platform bug for a day; `-n0` reproduces it on any platform in under a second.

**Registering the state is the fix, not relaxing the assertion.** Every
process-lifetime ledger or cache belongs in `conftest.RESET_GLOBALS` (for
`supertool`) or `conftest.PRESET_ENV_RESET_GLOBALS` (for the `_env` module the
presets share), so it is restored between tests and the assertion keeps meaning
what it says. Loosening the assertion, or asserting only when the output is
non-empty, converts "this broke" into "this silently gave you something else".

### Never reach a preset module by bare import

`presets/*/` basenames are **op names**, not module names. Twenty of them are
already claimed by more than one preset directory — `status`, `list`, `read`,
`publish`, `issue`, `job`, `search`, `_auth`, `_common`, `_sanitize`, ... — and
`sys.modules` has one slot for each. Every preset script also puts its own
directory on `sys.path` at import and never takes it off, so by the time a
fixture runs there are ~180 `presets`-flavoured entries on the path, ordered by
whichever suite imported its preset first.

So `import status` inside a test does not name a file. It names whichever of
`presets/mcp/status.py` and `presets/git/status.py` xdist's work split put on
the path first, and that is a property of the runner's core count. [#693] moved
it by *adding an unrelated test file*: the split changed, `presets/git` was
scheduled beside four `presets/mcp` suites for the first time, and 18 of them
went red on a module they do not cover.

Load it by absolute path under a preset-qualified name instead:

```python
from _preset_loader import load_preset_module

status = load_preset_module("mcp", "status", prefix="mcp_")
```

`tests/_preset_loader.py` evicts the sibling shims, scopes the path edit to the
`exec_module` call, and restores `sys.path` exactly as it found it — nothing is
registered in `sys.modules`, so two presets' same-named modules can coexist in
one worker. `test_git_status.py` and `test_status_swallowed_705.py` show the
raw `spec_from_file_location` form for cases that need it.

Binding the contested name once, early, in `conftest.py` is not the fix. It
wins the race rather than removing it: the slot is still one slot two files
claim, the next conftest edit or earlier-importing plugin flips it back, and it
flips back silently — the same `AttributeError`, or the same `git-status`
render inside an mcp assertion. `test_preset_basename_collision_726.py`
AST-scans `tests/*.py` and fails on the shape, naming the file, the line, and
the rival paths.

### Comparing rendered output

If your test compares two rendered blocks to each other, **the thing that
differs may be the clock, not the code.**

Supertool prints durations it measured itself: the `[validators]` per-tool time
column, and the `PASS (0.02s)` header on a custom op. Two runs of the same op
therefore render two different strings. An assertion that two outputs are
*indistinguishable* then passes on that jitter alone — and it fails in the
direction that hurts, going green while the bug it targets is fully present.
That is not hypothetical: it happened in [#621]'s own RED run, and it happened
non-deterministically, passing under xdist and failing serially on the same
code ([#643]).

You do not have to remember any of this. `tests/conftest.py` sets
`SUPERTOOL_DETERMINISTIC_TIME=1` for the whole suite, and every duration
supertool measured then renders as a frozen `0.0s`, so a comparison can only
ever see a real difference. Two rules follow:

- **Do not set that variable in shipped code, a preset, or a config.** It is
  test-only. A real run must report the real time — that number is how a human
  sees which validator is slow. `test_switch_is_not_enabled_outside_the_test_suite`
  fails if it ever leaks.
- **Where the switch cannot reach, normalise instead.** Recorded fixtures, a
  subprocess with its own environment, or a test that deliberately
  `monkeypatch.delenv`s the switch to exercise the real formatting path: use
  `stable_render` from `tests/_render.py`, which rewrites duration-shaped
  tokens and nothing else.

A normaliser that strips more than the varying field is a worse version of the
bug it fixes — its tests would pass on anything. The bar is that a test using
one **still goes red when the behaviour breaks**;
`TestNormaliserIsNotTooBroad` and
`test_normalised_comparison_still_catches_a_real_regression` in
`tests/test_render_determinism_643.py` are what keep `stable_render` honest.

Durations supertool was *given* rather than measured — an adapter's own
`duration_ms`, which tests supply as a constant — are deliberately **not**
frozen, so `assert "(12ms)" in row` keeps working.

One measured duration renders as words rather than as `0.0s`: the elapsed on a
**timeout verdict** ([#727]). Everywhere else freezing a duration removes noise;
there it removes the evidence, because the elapsed is the only number the
message exists to carry, and `FAIL (timeout 0.0s > 10s)` states a verdict its
own figures contradict. Under the switch that line reads `FAIL (timeout after
its 10s budget - elapsed frozen, deterministic-time mode)` — still constant
across runs, so the property above is unaffected. It was **not** exempted from
the freeze: an exemption is a call site to remember, and this fix lives at the
renderer for the same reason [#643]'s did. If you need to assert on a real
elapsed there, `monkeypatch.delenv` the switch as
`test_real_durations_render_when_the_switch_is_off` does.

`_timeout_verdict_line` also refuses to print an elapsed *below* its budget as
a result: `subprocess.run(timeout=T)` cannot raise before T has passed, so that
combination is a bug in the reporting path and says so.

### `slow` vs `benchmark`

Two markers, and the difference is not how long the test takes.

| Marker | Default run | Pre-push hook | CI |
| --- | --- | --- | --- |
| *(none)* | yes | yes | yes |
| `slow` | no | **yes** | yes |
| `benchmark` | no | **no** | no |

A `slow` test is heavy and still deterministic: it costs time and buys a real
answer, so the hook runs it. A `benchmark` asserts on **elapsed wall-clock**,
which on a parallel or loaded machine measures the box at least as much as the
code — `assert 5.02 < 5.0` is a busy scheduler, not a regression ([#485]).
Landing that as a push refusal costs ~110s and a diagnostic detour on a diff
that could not have caused it, and it spends the credibility of every failure
that *does* mean "this diff is wrong".

So put a wall-clock assertion behind `@pytest.mark.benchmark` and run it when
you want the number, on a machine whose load you control:

```bash
pytest -m benchmark -n0     # -n0: serially, so the timings mean something
```

If you want a performance property *gated* rather than reported, assert on
work done instead of on time — peak allocation, node counts, or a ratio
between two input sizes measured in the same process, where machine speed
cancels out. `TestParseScaling` in `tests/test_xml.py` is the worked example,
including what each metric does and does not catch.

### A fake binary on `PATH` cannot intercept an adapter's spawn on Windows

Every adapter under `validators/` spawns a **list** with an extensionless
program name — `["php", "-l", f]`, `["gofmt", "-l", f]`, `["cargo", "check"]`.
Python hands that to `CreateProcess`, which appends `.exe` and **does not
consult `PATHEXT`**. A `php.bat` or `gofmt.bat` shim in the first `PATH` entry
is therefore invisible: the search walks straight past it and the real binary
the `windows-latest` image ships answers instead. (`.bat` files *are* runnable
through `subprocess` — the CVE-2024-1874 surface — but only when the extension
is explicit.)

**The failure is silent, and it wears two different faces.** Neither is an
error, which is what makes this worth a rule:

- where the real tool accepts the subject file, you get a **clean verdict** —
  `assert True is False`, indistinguishable from a pass;
- where it rejects the subject file, you get a **real finding** — #753's
  JSON-contract case handed a `subject.txt` of `noise` to a real `xmllint`,
  which correctly reported `parser error : Start tag expected`, so the leg
  failed as `assert 'xml' == 'adapter'` and looked like a classification bug.

So: **split the test into two layers.**

1. **The rule, in process, on every platform.** Classification over a tool's
   output is platform-independent by construction. Import the adapter module
   (`importlib.util.spec_from_file_location` — most adapter filenames have
   hyphens) and call its classifier directly against **real captured
   transcripts**. This is the layer that has to hold everywhere. Keep the
   classifier at module level rather than inline in `main()` so it can be
   called; `xmllint.parse_diagnostics`, `node_check.diagnostic_line` and
   `terraform_check.is_fmt_verdict` exist in that shape for this reason.
2. **The whole adapter, spawned, POSIX-only.** `pytest.mark.skipif(os.name ==
   "nt", ...)` with the `CreateProcess` reason written into the marker, not a
   bare platform exclusion. What Windows loses is the spawn-and-decode path,
   which `test_validators.py` already covers there against the real binaries.

**And assert the interception itself.** A fixture that is never used cannot
fail, so one case per faked binary must prove the fake answered, using an exit
code and a marker no real tool emits:

```python
bindir = _fake_tool(tmp_path, "gofmt", exit_code=42, stdout="I-AM-THE-FAKE\n")
data = _run("gofmt-check", bindir, target)
assert "I-AM-THE-FAKE" in json.dumps(data), describe(data)
```

Worked examples: `tests/test_phplint_tool_vs_file_745.py` (one adapter) and
`tests/test_adapter_tool_vs_file_753.py` (seven, parametrised).

### Never write a formatter's fixture with `write_text`

A fixture for a formatting check goes to disk as bytes:

```python
path.write_bytes(text.encode("utf-8"))
```

`Path.write_text` translates every newline to `os.linesep`, so on Windows the
fixture arrives at the tool as CRLF — and a formatter rewrites CRLF. `gofmt -l`
prints the filename of a CRLF file whatever else is in it, so the *correctly
formatted* fixture is reported as needing formatting, on Windows only.

This is worth a rule because of how it was read the first time. The failure was
recorded as `reason="gofmt adapter has encoding issues on Windows"` and the test
was skipped there — so the adapter carried the blame for three occurrences, the
phrase in that skip reason was the entire diagnosis on file, and Windows lost
its only check that a clean Go file is reported clean
([#777](https://github.com/Digital-Process-Tools/claude-supertool/issues/777)).
The mechanism is pinned on every platform by
`test_gofmt_reads_a_crlf_file_as_needing_formatting`, because the platform a
newline defect appears on is the one nobody here can run a local check against.

The terraform and cargo fixtures in `tests/test_validators_tier2.py` still use
`write_text`; they are guarded by a `which()` skip that keeps them off the
Windows legs today, which is luck rather than design.

### Never bracket a timestamp with zero tolerance

A test that reads a clock before and after a call and demands the emitted value
land between them is asserting that the host does not adjust its wall clock. It
does: `test_the_snapshot_says_when_it_was_read` reported a value a full second
*earlier* than a bound read before it, which program order cannot produce, and
reddened two legs of a pull request whose diff was one character of regex
([#909](https://github.com/Digital-Process-Tools/claude-supertool/issues/909)).

Reading the same clock API the product reads does not fix it — `datetime.now(
timezone.utc)` and `time.gmtime()` are both `CLOCK_REALTIME`, and a step moves
them together. Two things do, and the split is the point:

1. **Slack on the bracket**, sized so that everything the assertion exists to
   catch is still caught. An age, a hardcoded string, a wrong epoch and
   milliseconds-read-as-seconds are all wrong by an epoch, not by a minute, so
   two minutes of tolerance costs the test nothing.
2. **A second test with the clock frozen**, for the part slack would blur.
   Patch both `time.gmtime` and `time.localtime` to distinguishable values and
   assert the exact string: that is what pins a `Z` suffix as a claim about
   *which* clock was read, and a real-clock bracket cannot see a local-time
   swap at all on a runner whose zone is already UTC.

### Never write a subprocess timeout by hand

If a test spawns a validator adapter, its budget comes from
`tests/_adapter_budget.py` and nowhere else:

```python
from _adapter_budget import adapter_budget

r = subprocess.run([sys.executable, str(PHPLINT), str(f)],
                   capture_output=True, text=True,
                   timeout=adapter_budget(PHPLINT))
```

A test guard fails the suite if you write the integer instead
(`test_no_test_spawns_a_validator_adapter_on_a_hardcoded_budget`).

**Why there is a rule rather than a habit.** Three separate reds have now been
one hand-written number each, all Windows-only, all under load: `gofmt-check`
at 15s ([#702]), `phplint` twice at 10s ([#658]), `git rev-list` at 5s
([#650]). The value is not what was wrong with them. What was wrong is that
nothing related the number to what the adapter is *allowed* to take.

**The rule, in one line: an outer budget is a hang-guard on the adapter, so it
must exceed the adapter's own budget.** Every adapter under `validators/`
already wraps the real tool in its own `timeout=` and already declines when it
blows it. So a test spawning the adapter is not waiting on the tool — it is
waiting on the adapter, which owes an answer within its own budget plus the
cost of starting Python twice.

Set the outer budget *below* that and it can never fire for a hang: the
adapter declines first and the test gets JSON, not a `TimeoutExpired`. The
only thing left that can trip it is a slow machine. That is the previous
section's benchmark, arrived at by accident — multiply it by ten and the
assertion catches nothing new, so the number *was* the assertion. Every
reported site was on the wrong side of the line: phplint 10 < 30,
gofmt-check 15 < 30, cargo-check 120 == 120 (a tie is a race).

`adapter_budget()` reads the adapter's internal budget out of its source, adds
spawn headroom, and multiplies on Windows — where process spawn is materially
slower, antivirus interposes on every temp file, and the runner is shared, and
where all three incidents happened. Raising an adapter's internal timeout
therefore raises every test budget over it with no second number to update.
`SUPERTOOL_TEST_ADAPTER_TIMEOUT` overrides it for a run, the way
`SUPERTOOL_LINT_TIMEOUT` ([#553]) and `SUPERTOOL_GIT_TIMEOUT` ([#650]) do.

**These tests fail rather than skip when the budget blows, deliberately.** A
skip is right for a check that cannot tell "the tool hung" from "the runner
was busy" — which is precisely what a 10s budget over a 30s adapter could not
do. Deriving the outer budget from the inner one removes that ambiguity: a
blown budget now means the adapter did not honour its own timeout, which is a
real hang in the code under test. Skipping it is how a genuine hang becomes
invisible, and this repo files that trade against itself every time.

**But the adapter answering "I timed out" is a different event, and it
declines** ([#794]). The paragraph above is about the *outer* budget — the
`TimeoutExpired` this test never gets to see, because the adapter is
contractually obliged to answer first. When it does answer, and the answer is
its own `code: "adapter"` wall, the test asked for a lint verdict and did not
receive one. #794 failed exactly there: phplint returned `[adapter] timeout`
after 30000ms — its own 30s budget — while the outer budget was `(30 + 10) × 3
= 120s` and never came near firing. Every remedy aimed at the outer number
would have changed nothing about that run.

So a test that spawns a real adapter to assert a real verdict uses
`_adapter_verdict.assert_adapter_ok_or_skip_if_stalled(r, adapter=…,
inner_s=adapter_budget's inner_budget(ADAPTER))`, which declines on a stall
and reports the measured duration in the skip reason. **The predicate is
deliberately narrow, and each clause keeps a real defect loud:** every error
must carry `code: "adapter"` (a `parse` finding beside a stall is still a
broken file), the message must name a timeout (a missing binary or an
unreadable argv is something someone has to fix, per `validators/SCHEMA.md`),
and `duration_ms` must actually reach the adapter's internal budget (an
adapter reporting `timeout` in 12ms has broken error routing, which is a
defect in the thing the suite tests). Nothing else in this rule moves: the
adapter still publishes the stall as `ok: false`, never as `skipped`.

**A test asserting a file is *broken* needs the same gate, and cannot use that
wrapper** ([#1296]). A stall is `ok: false` with one `adapter` error, so
`assert_declined` passes on it and whatever comes next — a pinned source line,
an `errors[0]["code"]` — is what fails, pointing at the product. Use
`_adapter_verdict.skip_if_stalled(payload, inner_s=inner_budget(ADAPTER))`,
which runs the identical predicate and hands back any non-stall payload
unchanged. Put it in the file's own spawn helper, not at each call site: one
gate is one thing to keep correct, and every assertion in the file is equally
exposed. `tests/test_html_check.py` does exactly that, pinned by
`tests/test_html_check_stall_1296.py` — which also pins the bar such a gate has
to clear, that the test still fails if the adapter reports the broken file
clean.

**The adapter side of the same rule.** An adapter that grants itself a budget
must survive blowing it. Letting `TimeoutExpired` escape kills the process on
a traceback with **empty stdout**, and every caller `json.loads()` that — so a
slow linter surfaces as a `JSONDecodeError` naming neither the tool nor the
timeout. Emit the decline instead (`code: "adapter"`, message naming the
budget); `test_every_adapter_that_grants_itself_a_budget_survives_blowing_it`
enforces it across `validators/`.

### Never assert an adapter's verdict as a bare boolean

`assert out["ok"] is True` fires as `assert False is True`. Every adapter under
`validators/` answers with the same object — `tool`, `ok`, `count`, `errors`,
`duration_ms` ([`validators/SCHEMA.md`](validators.md)) — so at the moment that
assertion fails the test is holding a full statement of *why*, and throws it
away. An adapter has roughly a dozen routes to `ok=False`: tool absent, tool
present but not executable, its own internal budget expired, the file genuinely
did not parse, no file argument. The bare boolean separates none of them.

That has now cost two whole occurrences, both Windows-only, neither
reproducible on demand:
[#658](https://github.com/Digital-Process-Tools/claude-supertool/issues/658)/[#717](https://github.com/Digital-Process-Tools/claude-supertool/issues/717)
(`test_valid_ruby`) and
[#725](https://github.com/Digital-Process-Tools/claude-supertool/issues/725)
(the phplint spawn test that
[#716](https://github.com/Digital-Process-Tools/claude-supertool/issues/716)
added *in the same PR where it was fixing this exact opacity elsewhere*). For a
red that appears once a quarter on a runner you do not have, that one occurrence
is the entire diagnostic budget.

Use `tests/_adapter_verdict.py`:

```python
from _adapter_verdict import assert_ok, assert_declined, verdict, assert_adapter_ok

out = verdict(result, adapter="ruby-check")   # instead of json.loads(r.stdout.strip())
assert_ok(out, context="a Ruby file with nothing wrong with it")
assert_declined(out, context="a file with a deliberate syntax error")
assert_adapter_ok(result, adapter=name, context="…")   # both, for one-spawn tests
```

`verdict()` replaces `json.loads(r.stdout.strip())`, whose failure is a
`JSONDecodeError` naming neither the adapter, the exit code, nor the stderr
holding the traceback. `assert_ok`/`assert_declined` render the payload.

**The formatter is defensive on purpose, and that is the part to preserve.** It
formats a structure it does not own: adapters are separate programs, and a
payload can arrive in a shape nobody anticipated — `errors` as a string, an
entry that is not an object, no `errors` key at all, a crash before any JSON.
A diagnostic that renders blank on those reproduces the defect inside its own
fix, which is this repo's house failure exactly. So every branch of
`describe()` ends in text naming what it could not read, none of them can
raise, and `describe()` returning `""` is a bug. Output is bounded — three
errors, 200 characters a field — because an unbounded dump buries the first
error it exists to show ([#719](https://github.com/Digital-Process-Tools/claude-supertool/issues/719)'s
rule: a capped list says how many it hid).

**The guard is scoped to files that have adopted it**, deliberately.
`test_a_file_that_adopts_the_convention_adopts_it_everywhere` walks the AST of
every test file importing `_adapter_verdict` and fails on any remaining
message-less `["ok"] is <bool>`. It does not police files that have not adopted
it yet: a guard that fails on work nobody has done is a guard that gets deleted.
What it does prevent is #725's actual complaint — a new bare assertion landing
in a file that already knows better.

### Pin third-party actions to a sha, never to a major tag

Every `uses:` in `.github/workflows/` names a 40-character commit sha with a
trailing `# vX.Y.Z` comment. `tests/test_ci_action_pinning_925.py` fails the
suite on a tag pin, on a sha with no version comment, and on its own discovery
finding fewer than three workflow files.

**Why it is a rule and not a nicety** ([#925](https://github.com/Digital-Process-Tools/claude-supertool/issues/925)). `@v7` is
branch-like: the publisher moves it on every patch release. A compromised or
merely careless upstream therefore executes with this workflow's `GITHUB_TOKEN`
on the very next run of a commit that has not changed — which is exactly how
the tj-actions/changed-files compromise reached its downstreams.

**Dependabot is not the mitigation, and reads like one.** It will never bump
`v7` to `v7`, so under a tag pin a retag is live before any PR exists. Under a
sha pin it *is* the mitigation: it bumps the sha, rewrites the version comment,
and the upgrade becomes something a human approves. So the pin costs a weekly
review, not a standing manual chore — that trade is the reason this is a rule
rather than a preference.

**The version comment is load-bearing.** A bare 40-hex ref is unreadable, and
without the comment nobody can tell a current pin from a three-majors-stale one
by looking. `changelog.yml` sits on `checkout` v4 while the other two workflows
sit on v7; that is now visible in the file rather than only in the tag.

### Never leave a CI job without a wall-clock budget

Every job in `.github/workflows/tests.yml` declares `timeout-minutes`.
`tests/test_ci_job_timeouts_722.py` fails the suite if one does not, so a
fifteenth job cannot arrive without a budget.

**Why it is a rule and not a nicety.** GitHub's default is **six hours**, and
until it expires a hung leg renders as `pending` — byte-identical to a leg that
is about to finish. `notifiers (bun + TypeScript) (ubuntu-latest)` sat at 26
minutes on [#715] against 24-37s on every recent master run, its macOS twin
green on the same commit, on a PR that touched no TypeScript ([#722]). The board
read `13 passed, 0 failed, 1 pending`, the states still summed to the leg count
so [#454]'s arithmetic check passed, and the merge gate quietly became a six-hour
block. There is also nothing to read while it hangs: `gh api .../jobs/<id>/logs`
answers `BlobNotFound` for an in-progress job, and a job cancelled by hand never
writes a log at all. A job killed by `timeout-minutes` **fails, with its log
written** — that is the half of this worth having.

**Size per job class, from that class's own measurements.** Copying one number
across classes is [#702] in CI config. The current two came from 70 job
observations across five master runs:

| job | observed | budget |
| --- | --- | --- |
| `pytest` (windows 483-574s, macos 128-197s, ubuntu 93-125s) | worst 574s | 30 min — 3.1x |
| `notifiers` | worst 37s | 10 min — 16x |

The multiple on `notifiers` is large because its base is 37s and three of its
steps are package-manager installs (npm, pip, bun), whose tail latency against a
degraded registry is minutes. A tight multiple there would fire on a bad network
day, and a guard that reds a genuine green teaches everyone to press re-run —
worse than the disease. The floors and ceilings are asserted at both ends: a
budget tightened into a benchmark fails the suite, and so does one loosened far
enough to be the six-hour default with extra steps.

**Bound the step too, but with the inner tool rather than a second wall clock.**
The step that hung now runs pytest under `--timeout=30` (pytest-timeout, which
both jobs had installed and neither used). A job ceiling can only report "this
leg did not finish"; the per-test budget names the test and dumps the stack of
the thread stuck in it, which is the artefact [#554] needs. A step-level
`timeout-minutes` was the alternative and buys nothing the other two do not: it
is a third number to keep ordered and it names no test. The ordering is the
[#702] rule one layer out — the job ceiling must exceed the per-test budget, or
the inner guard can never fire — and it is asserted by reading both numbers out
of the workflow rather than tabulating them beside it.

**The `pytest` job deliberately gets no per-test budget.** ~4000 tests under
`-n auto`, `slow` included in CI, and its windows legs are the ones this repo has
three times measured blowing a hand-written number under contention ([#702],
[#658], [#650]). There is no per-test timing from a windows leg to size one
from, and guessing it is precisely those three incidents one layer up. The job
ceiling bounds it; the finer guard waits for evidence.

[#454]: https://github.com/Digital-Process-Tools/claude-supertool/issues/454
[#485]: https://github.com/Digital-Process-Tools/claude-supertool/issues/485
[#553]: https://github.com/Digital-Process-Tools/claude-supertool/issues/553
[#621]: https://github.com/Digital-Process-Tools/claude-supertool/issues/621
[#643]: https://github.com/Digital-Process-Tools/claude-supertool/issues/643
[#650]: https://github.com/Digital-Process-Tools/claude-supertool/issues/650
[#658]: https://github.com/Digital-Process-Tools/claude-supertool/issues/658
[#689]: https://github.com/Digital-Process-Tools/claude-supertool/pull/689
[#702]: https://github.com/Digital-Process-Tools/claude-supertool/issues/702
[#554]: https://github.com/Digital-Process-Tools/claude-supertool/issues/554
[#715]: https://github.com/Digital-Process-Tools/claude-supertool/pull/715
[#722]: https://github.com/Digital-Process-Tools/claude-supertool/issues/722
[#727]: https://github.com/Digital-Process-Tools/claude-supertool/issues/727
[#794]: https://github.com/Digital-Process-Tools/claude-supertool/issues/794
[#1296]: https://github.com/Digital-Process-Tools/claude-supertool/issues/1296

### Never assert a property of a workflow by grepping the workflow

`.github/workflows/tests.yml` is 183 lines of which roughly two thirds are
comments explaining why each decision was made. A comment is prose about the
code; a substring match cannot tell the two apart. So

```python
assert "oven-sh/setup-bun" in workflow_text, "nothing installs bun any more"
```

went on passing after the action was dropped for `npm i -g bun@1.3.14` —
because the string survived inside the comment recording the switch. The test
was kept green by the prose documenting the change it existed to notice
([#730]). The same file did it a second time with `--no-cov`, and
`test_ci_job_timeouts_722.py` — which was the *structural* alternative — did
it a third time with `--timeout=30`, whose justifying comment quotes the flag
twelve lines above the flag ([#731]).

**Read the structure instead.** `tests/_workflow_parse.py` gives you the jobs
(`job_blocks`), their steps (`job_steps`), and each step's `uses:`, `env:` and
`run:`. Assert against those. A comment can then say anything at all and no
assertion moves.

```python
steps = job_steps(job_blocks()["notifiers"])
assert any(_BUN_INSTALL_RE.search(s.run) for s in steps)   # yes
assert "npm i -g bun" in workflow_text                     # no — re-arms on the next rename
```

Two rules follow from the three instances:

* **Swapping the needle is not the fix.** `"npm i -g bun"` is the same defect
  with a fresher string: the next rename re-arms it, and a comment mentioning
  the old command re-arms it immediately.
* **Compare sets, do not list names.** The same guard named two of the five
  channel test files the job runs, so three could have been dropped without it
  noticing — while two others had already arrived without being added. Both
  directions close if you assert the set CI runs equals the set the repo has.

PyYAML is deliberately not used: CI installs pytest, pytest-cov, pytest-xdist
and pytest-timeout and nothing else, so importing `yaml` would make every
guard built on it skip on all fourteen legs. The parser reads indentation, and
it is fixture-tested — a parser that silently finds nothing renders its callers
green while checking no job at all, which is the defect one layer up.

The general form, worth asking of any guard: **what would have to be true for
this test to fail?** If you cannot name a realistic change to the product that
turns it red, it is not a guard.

[#730]: https://github.com/Digital-Process-Tools/claude-supertool/issues/730
[#731]: https://github.com/Digital-Process-Tools/claude-supertool/issues/731

## Submitting upstream

Want to add a preset, op, or validator to the shipped supertool?

- **Branch naming:** `feat/short-description` for features, `fix/short-description` for bugs, `docs/short-description` for documentation.
- **One feature per PR.** A new preset is one PR. Adding a validator adapter is one PR. Bundling both makes review harder.
- **Tests in `tests/`.** New ops and validators need test coverage. Check existing tests for the pattern.
- **README update if introducing new shape.** If your PR adds a new top-level config key or changes op schema, update the README config reference section.
- **Commit messages:** `feat: add kubectl preset` / `fix: {path} placeholder on Windows` / `docs: add contributing guide`. Present tense, imperative mood, lowercase.

The "one feature per PR" bullet above is written for an external contributor
submitting a single, self-contained change — the other bullets (tests, README update,
commit messages) apply regardless of who is submitting or how many issues a PR closes.
"One feature per PR" itself is a coordination cost, not a correctness property, and its
cost structure inverts for maintainer-side work.

**Maintainer-side convention: one lane per PR, not one issue per PR.** Issues here are
frequently worked in lanes — a set of issues that share files or a subsystem, so loading
that context once serves several fixes instead of paying to reload it per issue. Branches
follow the issue numbers directly (`fix/1094`, or `fix/1067-1071` for a bundled lane),
skipping the `feat/short-description` scheme above, which exists for a contributor who
has one thing to submit. Commit titles follow the same pattern rather than the
`type: description` form above — a descriptive sentence with the issue number in
parens, e.g. "grep says which pattern it ran, and delegation stops answering a BRE
(#1098)" — because a lane title has to name more than one change. 15+ of the last 20
merged PRs closed more than one issue this way, and that is by design, not drift.

**The boundary that keeps this legitimate:** bundling is a lane call, not a batching
convenience.

- Legitimate: the issues genuinely touch the same files or subsystem, and the PR would
  have opened the same files even for just one of them (e.g. two fixes to `presets/gh/`,
  two docs issues about the same section).
- Scope creep: a change riding along because "it's the same lane" when it touches files,
  behavior, or docs the bundled issues did not require. If a reviewer can't point at the
  shared file or subsystem an issue's fix needed, it does not belong in the PR — file it
  separately, even if it is small.

An external contributor should still open one PR per change; the maintainer-side lane
convention is not license to bundle unrelated fixes just because they landed the same
day.
