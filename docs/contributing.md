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

Three rules, all enforced by `tests/test_encoding_seam.py`. They exist because four separate defects came out of this seam one at a time ([#400](https://github.com/Digital-Process-Tools/claude-supertool/issues/400), [#415](https://github.com/Digital-Process-Tools/claude-supertool/pull/415), [#418](https://github.com/Digital-Process-Tools/claude-supertool/issues/418), [#431](https://github.com/Digital-Process-Tools/claude-supertool/pull/431)) — each found only once the previous one was fixed, and every one of them on a platform the author was not sitting on.

**1. Every text read and write names its codec.**

```python
path.read_text(encoding="utf-8")          # yes
open(path, encoding="utf-8")              # yes
open(path, "rb")                          # yes — binary decodes nothing
path.read_text()                          # no  — decodes with the locale
```

Without `encoding=`, Python decodes with `locale.getpreferredencoding()`: **cp1252** on a Windows console, **ASCII** under the C/POSIX locale that a great many cron jobs, containers and CI runners default to. Any file holding a `—` or a `✓` — which includes `presets/git.json`, shipped in this repo — then raises `UnicodeDecodeError`. A static AST scan over `supertool.py`, `presets/`, `hooks/`, `validators/`, `formatters/` and `notifiers/` fails the suite on a new one and names the file and line.

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

## What CI runs, and what it does not

The pytest matrix is {ubuntu, macos, windows} × py3.9–3.12. For a long time that was the whole workflow, which meant `notifiers/claude-channel/channel.ts` — TypeScript, started in every radar session — shipped on a 12/12 green that had never executed a line of it ([#557](https://github.com/Digital-Process-Tools/claude-supertool/issues/557)). Its tests existed and were real, but they `skipif` on `shutil.which("bun")`, so they were collected, skipped, and counted as neither a pass nor a failure.

| Code | Where it runs | What is checked |
| --- | --- | --- |
| Python | all 12 pytest legs | the suite, 86% coverage floor on `supertool.py` |
| `notifiers/claude-channel/channel.ts` | `notifiers` job, ubuntu + macOS | `bunx tsc --noEmit` under the channel's own strict tsconfig, plus the two socket-level integration test files, run for real |
| Shell (`*.sh`, `.githooks/*`) | all 12 pytest legs | `bash -n` — **syntax only**, via `tests/test_ci_non_python_coverage_557.py` |
| `notifiers/cursor-witness/extension/src/extension.ts` | nowhere | **uncovered, knowingly** — a VS Code extension needs `npm install` of the editor's type packages to compile and an editor host to exercise |

Three things about that table are load-bearing:

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
not gated — as CI does, before every push):

```bash
git config core.hooksPath .githooks
```

The hook is in `.githooks/pre-push`, committed to the repo. Bypass with `git push --no-verify` (discouraged).

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
