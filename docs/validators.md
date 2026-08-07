# Validators — full reference

## What validators are

Validators are squiggle-on-save for the LLM. After every mutating op — `edit`, `replace`, `replace_lines`, `paste`, `append`, `vim` — supertool runs the matching validators against the result file. If one fails and `rollback_on_fail: true` is set, the file reverts atomically to its pre-edit state and the model gets an immediate error receipt with the parse error, line number, and column. No broken files sitting around. No "edit succeeded, discovered it three turns later."

The model retries with real information instead of hallucinating a fix.

## The built-in syntax backstop

That paragraph used to be true only of repos that had configured the right validator, and the gap was invisible: this repository itself wired `lsp-diag` — a *semantic* diagnostics pass served by a warm language-server daemon, with `rollback_on_fail: false` — as its only Python validator. Nothing in the chain ever asked whether the file parsed, so an edit that wrote an unterminated string literal into a `.py` file was reported as `lsp-diag : ok (no new errors)` and left on disk. Twice in one evening, both times on payloads with tricky escaping, which is exactly the case the check exists for ([#477](https://github.com/Digital-Process-Tools/claude-supertool/issues/477)).

So `py-syntax` is not a configured validator — it is built into supertool and it always applies:

| | |
|---|---|
| Runs on | `*.py`, for `edit`, `replace`, `replace_lines`, `paste`, `append`, `vim` |
| How | `compile()` in supertool's own process — no subprocess, no daemon, no cache, microseconds |
| Rolls back | Yes, when the file parsed *before* the op and does not parse after |
| Config needed | None. It runs in a repo with no `.supertool.json` at all |
| Deferred to | A configured validator matching the same file that declares `"syntax": true` |

It reverts **regressions only**. A file that was already unparseable can still be edited, a broken file can be repaired, and a new file created broken is reported red but not deleted — there is no pre-edit state to restore. Anything it cannot answer (unreadable file, unknown builtin) comes back as `skipped`, never as ok.

**It is Python-only, and that is a real limit, not an oversight.** The interpreter running supertool can parse Python for free; it cannot parse PHP, TypeScript or Go without a toolchain that may not be installed. For those, declare a parse check in `.supertool.json` with `rollback_on_fail: true` (see the bundled list below) — and add `"syntax": true` to it so the backstop stands down where you have your own.

## Why an LSP validator cannot be that backstop

`lsp-diag` looks like it covers the same ground for every language it matches. It does not, and the reason is structural rather than a bug in the adapter.

It queries a **warm** language-server daemon, and a warm daemon answers about the document it holds, not about the bytes on disk. Measured against cclsp/Pyright: `get_diagnostics` is served from a per-URI cache that `publishDiagnostics` fills, and `ensureFileOpen` returns early once a file is open — so nothing re-reads the file for the rest of the daemon's life. A `.py` file opened clean and then broken on disk still reported *"No diagnostics found"* after a 20-second wait and an `mtime` bump, while a cold daemon reported the unterminated literal instantly.

The sharp part is that **supertool causes this itself, by doing the right thing.** Every mutating op takes a pre-edit baseline so the receipt can report a *delta* rather than a total — and that baseline pass is exactly what opens the document in the daemon. The check is therefore strongest on files the daemon has never seen and blind on the one under edit. Skipping the baseline would not fix it either: it would only move the first-open to the after-pass, so the *second* edit of the same file goes stale instead of the first, and every pre-existing error would render as a regression.

Since supertool speaks MCP tool calls rather than LSP, it cannot send a `didChange` to fix this: cclsp's document sync is internal and reachable from no tool it exposes. The remaining lever — restarting the server per edit — trades a stale answer for a full re-index on every edit, which is the cost #488 exists to avoid.

So `lsp-diag` **declines instead of guessing**. On the post-edit pass it reports:

```
lsp-diag    : skipped     (stale document — warm LSP daemon answers from its pre-edit copy, not from disk)   0.3s
```

Read that as *"nobody checked the semantics of this edit"*, because nobody did. Everything supertool's own messages produce — no LSP configured, `MCP server 'X' unavailable` (routine since #488 stopped short-budget validators spawning daemons), transport errors, empty results — reports the same way, naming the reason. None of them is a pass.

Two consequences worth stating plainly:

- **For `.py`, the syntax guarantee is unaffected** — `py-syntax` above is in-process, reads the file itself, and cannot go stale.
- **For `.php`, `.ts`, `.js`, `.jsx`, `.tsx`, `lsp-diag` was never a post-edit syntax check** and now stops looking like one. If you want a real guarantee for those, configure a parse check with `rollback_on_fail: true` from the table below. The green row that used to be there was not covering you.

If your LSP genuinely re-reads the file on every query, opt back in through the validator's `env` block:

```json
"lsp-diag": {
  "cmd": "{python} {supertool_dir}/validators/lsp-diag/lsp-diag.py {file}",
  "match": "*.{py,php,js,ts,jsx,tsx}",
  "env": { "SUPERTOOL_LSP_RESYNC_ON_QUERY": "1" }
}
```

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

A validator that declined to run is a **third state**, distinct from both "clean" and "has errors": it produced no information about the file. An adapter reports it by adding a `skipped` key to its SCHEMA.md JSON — the `skipped` key alone is the marker, and an adapter that has no verdict must omit `ok`/`count`/`errors` entirely rather than pad them with `ok: true` (a receipt carrying both reads as a pass to anything keying off `ok`). The row renders as:

```
phpstan-mcp : skipped    (path outside --paths allowlist)     0.1s
```

Skips never enter the before/after delta, never render a `✗`, are never cached (a skip is decided by config, and the cache key is a content hash), and — most importantly — **never roll back an edit**, even for a validator with `rollback_on_fail: true`. Counting a refusal-to-run as one error would let a scope-config mismatch revert perfectly good code.

Build the dict with `validators/common/refusal.py:skipped()` rather than by hand — that helper and this paragraph disagreed for a year (#515), and the shape here is the one that won. Consumers branch on the presence of `skipped` before reading any verdict key; they cannot do otherwise, since the reason string exists only on a skip, which is why the "uniform shape saves consumers a branch" argument for padding does not hold here. `tests/test_skipped_shape_contract_515.py` pins this prose, the helper and the core's own built-in skips against each other, so the three cannot drift apart again.

`phpstan-mcp`, `phpmd-mcp` and the cold `phpstan` adapter recognise their tool's own refusal messages (`--paths` allowlist, "no files found to analyse"). An exit they cannot explain still reports as an error — swallowing an unknown failure is the same category mistake pointing the other way. Teach an adapter a house-specific refusal with `PHPSTAN_MCP_SKIP_PATTERNS` / `PHPMD_MCP_SKIP_PATTERNS` / `PHPSTAN_SKIP_PATTERNS`: extra comma-separated, case-insensitive substrings.

**`PHPSTAN_MCP_PATHS` — decide the refusal locally instead of paying for it.** Recognising the refusal still costs the full daemon round trip that produced it: ~9.2s per edited file, spent to be told phpstan would not look at it (#412). Set `PHPSTAN_MCP_PATHS` to the analysis roots — `os.pathsep`- or comma-separated, absolute or relative to the working directory — and `phpstan-mcp` answers `skipped (path outside PHPSTAN_MCP_PATHS allowlist)` without opening the socket:

```json
"phpstan": {
  "cmd": "PHPSTAN_MCP_PATHS=src:lib MCP_PHPSTAN_BIN=… {python} {supertool_dir}/validators/phpstan-mcp/phpstan-mcp.py {file}"
}
```

It is opt-in, and it must stay that way, because the two ways it can be wrong are not equally bad. **Skipping a file that IS in scope loses the analysis silently** — the file looks handled and is not. **Analysing a file that is NOT in scope** wastes a round trip and still returns the right answer. So the adapter never infers the scope, never reads it out of `phpstan.neon`, and never defaults it on: unset (or blank) means "no local knowledge, the daemon decides", exactly as before. Setting the var is the repo asserting it knows the scope, and the skip reason names the var rather than `--paths` so a wrong skip points at this line of config instead of at a tool that never saw the file. Matching is on `os.path.abspath` with an `os.sep` boundary, so `/srcbad/Foo.php` is not inside `/src`.

Two things to keep in mind when setting it. It belongs on the **validator entry**, not the shell — each `.supertool.json` entry pins its own daemon and its own scope, so `phpstan` and `phpstan-fwk` need different values. And do not set it on an entry that uses `resolve` to hand the adapter a **temporary** file (the `phpstan-component` pattern): the resolved path lives outside the repo, would fall outside every root, and every such edit would skip.

**Silence is the refusal that hides best.** PHPStan does not announce a declined file on stdout at all — it writes `[ERROR] No files found to analyse.` to stderr, exits non-zero, and leaves stdout empty. Any adapter that treats "no output" as "no findings" turns that into `ok: true, count: 0` (#263), and a green meaning *"I analysed nothing"* is byte-identical to one meaning *"I analysed it and it is fine"*. An adapter with no parseable result must therefore decide between three outcomes and never default into the fourth: a recognised refusal is `skipped`, an unexplained non-zero exit is an error naming the exit code, and only a genuinely quiet success — empty output, exit 0 — keeps the clean verdict. When quoting the tool's words back as the skip reason, quote **the line that refused**, not the first line printed: analysers open with preamble (`Note: Using configuration file ...`) that names the config and explains nothing.

### The adapter's own reply is never a finding

The mirror image of that, and it shipped for as long: `validate:presets/gitlab.json` printed

```
jsonlint    : 1 err       (0ms)
     orchestrator  adapter bad json: Expecting value: line 1 column 1 (char 0)
```

against a file `json.load()` reads without complaint (#634). `Expecting value: line 1 column 1 (char 0)` is what `json.loads("")` raises — it was never about the file. The **adapter's** reply could not be parsed, and the orchestrator rendered its own confusion as an error attributed to the user's code, in the position and colour a real syntax error prints in. The `0ms` was the second tell: nothing had been measured.

Where #263 turned a silence into a false clean, this turned one into a false *finding*, which is the worse trade. A missed error costs one bug. An invented error costs the credibility of every error the validator prints — and this one fired on every `.json` edit, which is exactly how the first genuinely malformed file gets read as the usual noise and waved through.

So the orchestrator now takes the third state whenever the adapter gave it nothing it can read:

```
jsonlint    : skipped — jsonlint adapter replied with something that is not JSON — Expecting value: line 1 column 1 (char 0) — this file was not checked
```

Four exits route here, all of them "no verdict was produced": empty stdout, stdout that is not JSON, a reply that parses but carries neither `ok` nor `skipped`, and a `cmd` that could not be spawned at all (the missing-binary case — `1 err` there claimed the *file* was broken because a tool was not installed).

The skip reason names the program from the **spec**, never from the exception text: POSIX puts the missing binary in the `OSError` (`No such file or directory: 'jsonlint'`) and Windows does not (`[WinError 2] The system cannot find the file specified`), so reading it from the exception told Windows users a checker could not run without telling them which one — the platform-shaped hole #627 already paid for once. Only the platform's *reason* (`e.strerror`) is quoted, so the message reads the same shape everywhere: `jsonlint adapter could not be run: jsonlint — No such file or directory — this file was not checked`.

A **timeout is deliberately excluded** and stays a loud error. The binary exists and was invoked; a tool that hangs is a validator failure, and guessing towards silence there is how a broken validator starts looking clean.

This is not suppression, and the distinction is the whole point. Muting the row would have traded a loud false positive for a quiet absence of coverage — the failure nobody notices. The row still prints on every run; it now names the adapter, says it was the adapter's own reply that failed, and states plainly that the file was not checked. What it no longer does is claim a fact about the file.

**A `cmd` must point at a SCHEMA.md adapter, not at the raw tool.** That was the root cause: this repo's own `.supertool.json` had `"cmd": "{python} -m json.tool {file}"` while the bundled `validators/jsonlint/jsonlint.py` sat unused beside it. `json.tool` is a pretty-printer — it exits 0 and echoes the reformatted file, so the last stdout line is `}`, and `json.loads("}")` raises exactly the error above. Any raw linter wired this way reproduces it, which is why the two `_example-*` entries in `.supertool.example.json` (`phpcs …`, `pytest -q …`) illustrate `opt_in` and `resolve` but are **not** usable as adapter commands. Wrap the tool; do not invoke it directly.

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
| Python (types)      | `pyright`        | `pyright` (`npm install -g pyright`)| Real type-check via `pyright --outputjson` |
| Python (lint)       | `ruff`           | `ruff` (`pip install ruff`)        | `ruff check --output-format json`. Ruleset comes from the **project's** config, not the adapter — see below |
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
| PHP — static  | `phpstan`        | `phpstan` binary (PATH or via `PHPSTAN_BIN` env)  | Env-configured. See [README](../validators/phpstan/README.md) |
| PHP — mess    | `phpmd`          | `phpmd` binary (PATH or via `PHPMD_BIN` env)      | Env-configured. Auto-detects the project's `gitlab-ci/md/*.xml` CI rulesets so local findings match CI (`PHPMD_NO_AUTODETECT=1` to opt out; `ruleset_source` in the output records which path was used). See [README](../validators/phpmd/README.md) |
| PHP — style   | `psr`            | `phpcs` binary (PATH or via `PSR_BIN` env)        | Env-configured. See [README](../validators/psr/README.md)  |
| Git           | `git-status`     | `git` binary                                      | Reports working-tree delta (`+N -N <state>`) post-edit |
| Prettier — check | `prettier-check` | `prettier` binary (PATH or via `PRETTIER_BIN` env) | Env-configured read-only formatting check. See [README](../validators/prettier-check/README.md) |
| Shell — semantics | `shellcheck`    | `shellcheck` on PATH               | `shellcheck -f json`. Ruleset from the project's `.shellcheckrc`. Complements `bash-check`, which answers a different question. See below |
| JavaScript — lint | `eslint`        | `eslint` on PATH or resolvable via `npx --no-install` | `eslint -f json`. **Declines when no `eslint.config.js` resolves** — no fallback ruleset is invented. See below |
| Secrets           | `gitleaks`      | `gitleaks` on PATH                 | `gitleaks detect --no-git --redact`. Matches every path, not one language. Never prints the matched value. See [README](../validators/gitleaks/README.md) |


### The tool is not installed — `skipped` locally, loud in CI on request

`shellcheck`, `eslint` and `gitleaks` are external binaries most machines do
not have, which makes the same question three times: what does a validator
report when its tool is absent?

Reporting `ok` is the defect this repository has filed more than any other, so
it is `skipped`, with the reason and the install command on the row. That is
the honest local answer — a laptop that never installed shellcheck genuinely
holds no information about the file, and reddening every unrelated edit over it
would get the validator switched off within a day.

But a `skipped` that appears on every run of every job is how a validator
becomes decorative, and in CI "not installed" does not mean "no information",
it means **the gate is not running**. So the escalation is opt-in, and only
ever turns quiet into loud:

```bash
SUPERTOOL_REQUIRE_VALIDATORS=shellcheck,gitleaks
```

Comma- or `os.pathsep`-separated, case-insensitive, `*` for all. Named there, a
tool that could not run becomes an `adapter` error whose message names the
variable — so the reader is sent to the CI image rather than to the file. There
is deliberately **no** value that turns a finding into a skip: that would be a
mute button, and this repository declines those. `refusal.required()` is the
one implementation, shared by all three adapters.

The recommendation is to leave it unset locally and set it in CI for whichever
of the three that pipeline installs. Setting it for a tool the image does not
install produces a red on every edit, which is the same noise problem from the
other end.

**An escalated absence is not diffed, is named on `[result]`, and exits 1.**
The `adapter` error above is not a finding about the file — nothing opened it —
so it never enters the before/after subtraction that the rest of the block is.
It shipped inside that subtraction once, and the result read as a pass in three
places at once: the tool is absent for the pre-edit snapshot as well, the counts
cancel, and the row rendered

```text
shellcheck  : 1 err       (pre-existing — not from this edit)
gitleaks    : 1 err       (pre-existing — not from this edit)
[result] 1 op run, 1 write
```

with exit 0 — a claim that a real finding predated the edit, about a file no
checker read, under the one line documented to survive `| tail -1`, in the one
mechanism that exists to stop "the gate is not running" reading as a pass. It
now renders as its own state and says so where a script looks:

```text
shellcheck  : NOT CHECKED  (no verdict about this file)   0.2s
     adapter  shellcheck is named in $SUPERTOOL_REQUIRE_VALIDATORS but could not run, so this file was NOT checked: shellcheck not found on PATH — `brew install shellcheck` / `apt install shellcheck`
[result] 1 op run, 1 write, 1 validator NOT RUN (shellcheck) — those validators returned no verdict, so the file was NOT checked
```

Exit code 1. **Naming a validator in the variable is a statement that it must
be present here**, so one that is not is a configuration fault to fix, in the
image or in the variable — not a local inconvenience to absorb. Leave it unset
and none of this is reachable: an absent tool is the honest `skipped` it always
was, and exit stays 0. That is why the escalation is opt-in and why there is no
CI auto-detection; a tool that decided for you which absences are fatal would be
guessing at the thing only the operator knows.

It is an exit code, **not a refusal**. The edit still lands and still rolls back
on exactly the conditions it did before. Converting "we could not check" into
"we will not work" would trade the quiet failure for a louder one rather than
removing it.

### shellcheck — and the bug in the issue that asked for it

`bash-check` runs `bash -n` and answers "does this parse". `shellcheck`
answers the next question, and both stay: `bash -n` is in-process-cheap and is
what still runs where shellcheck is absent.

**The receipt in [#665](https://github.com/Digital-Process-Tools/claude-supertool/issues/665)
does not hold, and it is worth stating here so it is not repeated.** The issue
justifies the validator with `claude-remember#251` —

```sh
[ -n "$LAST_LINE" ] && tail -n +"$LAST_LINE" "$MEMORY_FILE" > "$TMP" || echo "(no previous entry)" > "$TMP"
```

— and calls it "SC2015 verbatim". Measured against ShellCheck 0.11.0, **SC2015
does not fire on that line**, at any severity, with `-o all -S style` (which
returns only SC2292/SC2250 notes about brackets and braces). SC2015 carries a
deliberate carve-out: it stays silent when the `|| C` branch is an `echo` or a
`printf`, because `cmd && x || echo failed` is an idiom people mean. The
redirect on that `echo` — the part that made #251 destructive, since `C`'s `>`
truncated what `B` had written — is not something the check looks at.

So this validator would **not** have caught the bug it was filed for. It is
still worth having, on rules that do fire and that these repos hit: SC2164
(`cd` without `|| exit`), SC2086 (unquoted expansion), SC2181 (`$?` instead of
testing directly) and SC2155 (`local x=$(cmd)` masking an exit code). Each is
exercised on a real file in `tests/test_validators_shellcheck_665.py`, and so
is the gap — a red on `test_the_issues_own_receipt_is_not_caught` means a
future ShellCheck closed the carve-out and this paragraph needs deleting.

No `--severity` floor and no `-o all` are passed: the ruleset walks up from the
file into the project's own `.shellcheckrc`, for the reason spelled out under
ruff below. `rollback_on_fail` is `false` — SC2086 is a style finding, and
reverting a good edit over one destroys work to fix nothing.

The `match` glob is `*.{sh,bash,ksh}`. It deliberately misses the
extensionless, shebang-carrying hooks that make up most of `claude-remember` —
a `match` that fired on every extensionless file would run shellcheck on
binaries and READMEs. The adapter itself has no such limit: point it at such a
file (`validate:hooks.d/after_save/50-git-backup`) and shellcheck reads the
shebang. A repo of extensionless hooks should add a second entry globbing the
directory.

### eslint — declining beats inventing a config

JS/TS coverage was `node-check` (syntax), `tsc-check` (types, TS only),
`prettier-check` (formatting) and `stylelint` (CSS). No linter. `eslint` fills
that, and it has **three** ways of saying nothing, each of which arrives
looking like a clean file:

| what happened | what eslint does | what the adapter reports |
|---|---|---|
| eslint not installed | nothing runs | `skipped` |
| installed, no resolvable config | exit **2**, stdout empty, message on stderr | `skipped` |
| the file matched an ignore pattern | exit **0**, one `ruleId: null` message, no findings | `skipped` |

The second is the trap [#667](https://github.com/Digital-Process-Tools/claude-supertool/issues/667)
names, and it is #263's shape exactly: an adapter that only counts findings
reads an empty stdout as `count: 0`. The third is not in the issue and is
worse, because the exit code is **zero** — verified against eslint 10.8.0,
where a file under `node_modules/` returns one `File ignored by default …`
message and nothing else.

**No fallback config is shipped, and that is the judgment call.** Inventing one
makes the validator enforce opinions the repo never chose, and the first thing
anyone does about findings they did not opt into is switch the validator off.
It would also be actively misleading for the case that motivated the issue:
DVSI's "never `var`" rule mostly governs JS inside XML templates, `js-onMe`,
`js-onInit` and inline handlers, none of which eslint can reach — so a fallback
config would produce a green that says nothing about the rule it was configured
for. A repo that wants JS linted adds `eslint.config.js`; until then the row
says nobody checked, on every edit.

Resolution order is a global `eslint`, then `npx --no-install eslint` for a
project-local one. `--no-install` is not optional: without it a post-edit
validator can reach the network mid-edit.

**The fallback is also how row one of that table used to be unreachable.** On
any laptop with node, `eslint` is absent from `PATH` and `npx` is not — so the
fallback resolves, the install-hint branch is never taken, and npx exits 1 with
empty stdout and `Unknown command: "eslint"` (npm 11) or `could not determine
executable to run` (npm 8-10) on stderr. Neither matches the no-config
signature, so it landed on an `adapter` error: the reader was told eslint
*failed* and sent to debug a linter that was never installed. Those two
messages, and only those two, and only when the npx route was the one taken,
are now the same `skipped` with the same `npm install --save-dev eslint` hint.
Any other npx failure stays loud — swallowing an unknown failure is the same
category mistake pointing the other way.

The adapter passes no `--select`. `ruff check` resolves its configuration by
walking up from the file it was handed, so **your** `pyproject.toml` or
`ruff.toml` decides what is reported — exactly what you would get running ruff
by hand. An adapter that hard-coded a ruleset would report findings the project
never adopted, and the first thing anyone does about that is switch the
validator off.

This repo's own choice, in `pyproject.toml`, is `select = ["E9", "F", "B",
"PLE"]` — syntax and IO errors, pyflakes, bugbear, and pylint's error tier. The
numbers behind it, measured on ~150k lines across 496 files:

| selection | findings |
|---|---|
| `--select ALL` | 40,514 |
| ruff's own defaults (0.16) | 2,213 |
| `E9,F,B,PLE` as shipped | 20, then 0 |

The middle row is the trap. A post-edit validator that prints a wall of
pre-existing style on an unrelated edit gets switched off, and a validator that
is off protects nothing — so the selection is the half that can be brought to
zero and held there. Three rules inside it are switched off with a reason
rather than left on and half-suppressed: `F401`, `F841` and `F541` (263
standing occurrences, whose fix is a mechanical `ruff check --fix` across half
the repo and belongs in its own PR), and `B023`, scoped off for `supertool.py`
alone, where all 34 occurrences are closures invoked inside the iteration that
defines them.

`rollback_on_fail` is **false**, and should stay false wherever you register
this. A lint finding is not a broken file; reverting a good edit because it
landed next to an unused import destroys work to fix nothing. Compare
`py-compile`, where a failure means the file no longer parses and rollback is
the whole point.

If ruff is not installed the validator reports `skipped` with that reason — not
`ok`. See "Graceful skip" above.

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
| `exclude`          | Glob (or list of globs) to skip even when `match` hits — e.g. `"*tests/*"`. Per-validator on purpose: `phpunit` must still scan tests. |
| `hooks_into`       | Op names to wrap (subset of `edit`, `replace`, `replace_lines`, `paste`, `vim`).       |
| `rollback_on_fail` | Restore pre-edit file content if the validator's count went up or ok flipped to false. |
| `resolve`          | Shell cmd returning an alternate target path (e.g. source-file → test-file).           |
| `timeout`          | Seconds. Default 60.                                                                    |
| `opt_in`           | If true, validator only runs on explicit request via the `validate` op.                |
| `tier`             | `"fast"` (default) or `"slow"`. `slow` defers the validator to end-of-call, deduped by `(validator, path)` — runs once per unique pair regardless of how many ops touched the file. See [Slow tier](#slow-tier--defer-to-end-of-call). |
| `env`              | Optional `{KEY: VAL}` block merged into the subprocess environment. Values are coerced to strings. Useful for pointing wrappers at a project-local binary or config without touching the system environment. |
| `mcp_autospawn`    | `true` lets this validator **create** a warm MCP daemon. Default `false`: a validator may use a daemon that is already warm but may not start one, because a cold MCP server indexes for 30-60s (longer on big repos) while the validator's own budget is measured in seconds — the validator is killed, the orphaned daemon keeps its index resident for the full `idle_timeout`. Set `true` only when the validator's `timeout` genuinely covers a cold start. See [MCP integration → daemon lifetime](mcp-integration.md). |

### env — usage

Pass tool-specific config without shell exports:

```json
"phpstan": {
  "cmd": "python3 {supertool_dir}/validators/phpstan/phpstan.py {file}",
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

## Slow tier — defer to end of call

Heavy validators (phpstan, phpunit, rector, tsc, cargo-check) often take 5–30s per run. In a multi-op batch — three edits on the same file — running them after each op is pure waste: same file content gets re-analyzed N times.

Set `"tier": "slow"` on the validator and supertool defers it: queued during the batch, drained once at end-of-call, deduped by `(validator, path)` so each pair runs exactly once regardless of how many ops touched it.

```json
"phpstan": {
  "cmd": "python3 {supertool_dir}/validators/phpstan/phpstan.py {file}",
  "match": "*.php",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "tier": "slow",
  "rollback_on_fail": false,
  "timeout": 60
}
```

Behavior:

- **Single-op calls always run inline**, regardless of tier — defer only kicks in for multi-op batches where dedup pays off.
- **Failures don't roll back prior ops.** Slow-tier results arrive after every op has already committed; mirrors the deferred-formatter contract. Keep `rollback_on_fail: false` on slow validators.
- **Output appears under a `[validators-deferred]` header**, separate from the per-op `[validators]` block, so it's clear which results came from the dedup pass.
- **Default is `"fast"`** — omitting the field preserves the original per-op behavior. Zero change for users who don't opt in.

Use `slow` for anything where a single analysis pass on the final file content is enough. Use `fast` (default) when you want per-op rollback semantics (syntax linters, json/yaml/xml parsers — these are cheap anyway).

## Caching

Results are auto-cached at `~/.cache/supertool/validators/`, keyed on `sha256(file_content) + name + cmd + tool fingerprint`. Validators skip re-running when neither the file nor the tools behind them have changed.

Disable caching per-call with the `SUPERTOOL_NO_VALIDATOR_CACHE=1` env var, or per-project with `"validator_cache": false` in `.supertool.json`.

**Tool fingerprint.** The key describes what did the analysing, not only what was analysed — otherwise a result computed by a buggy analyser keeps being replayed after the analyser is fixed. (Found the hard way: `mcp-phpstan-warm` 0.6.0 → 0.7.0 fixed two staleness bugs, and the very next run still served 0.6.0's wrong answers, in 0.2s, doing no work at all.) The fingerprint is a hash of `stat` signatures — `size` + `mtime_ns` — from two sources:

| source | catches |
|--------|---------|
| every token of the resolved `cmd` that is an existing file | adapter script edits, interpreter and binary swaps |
| `fingerprint_paths` on the validator spec | a specific config or binary that validator depends on |
| `validator_fingerprint_paths` at config top level | project-wide: the lockfile |

A lockfile is the high-value entry. Many analysers launch through a wrapper script whose own bytes never change between versions — composer bin proxies are exactly that — so stat-ing the launcher proves nothing. `composer.lock` (or `package-lock.json`) changes on **any** dependency upgrade:

```json
// .supertool.json
{
  "validator_fingerprint_paths": ["composer.lock"],
  "validators": {
    "phpstan": {
      "cmd": "...",
      "fingerprint_paths": ["phpstan.neon"]
    }
  }
}
```

Paths that don't exist contribute nothing rather than failing the lookup — a missing lockfile makes the fingerprint weaker, never disables caching. Fingerprints are computed once per process, so this costs a handful of `stat` calls per run, not per file.

**TTL.** Entries expire `validator_cache_ttl_hours` after they're written (default `24`; set `0` to disable expiry). This is the backstop for staleness the key still can't see: tool upgrades and adapter edits are now keyed directly by the fingerprint above, but a transient engine failure a clean re-run would pass, or a config file nobody listed in `fingerprint_paths`, still slips through. Expiry is on access: a stale entry is treated as a miss, re-runs, and is rewritten with a fresh timestamp. No cron needed.

**Engine failures are never cached.** The core cache filter is generic: it excludes a result whose error *code* is non-deterministic (MCP transport errors, non-zero adapter exits) and never inspects tool-specific message text (per `SCHEMA.md`: "Validator core never parses tool-specific output"). Real findings (PHPStan types, `rector.refactor` suggestions) are deterministic and stay cached. Message-level engine glitches — rector's `System error: ...` reflection failures and `Call to a member function toMutatingScope() on null` — are dropped at the source by the adapter, configured per-mcp via the `validators.rector.engine_glitches` prop in `.supertool.json` (a JSON list of case-sensitive substrings the adapter reads straight from `.supertool.json`; built-in defaults apply when the prop is absent). Add a new signature there — no code change. This prevents a transient warm-daemon hiccup from freezing a failure that replays on every later run (the June-2026 2100-poisoned-entries incident). See `validators/rector-mcp/rector-mcp.py`.

## Declining instead of guessing

A validator has three states, not two: `ok`, a finding, and **`skipped`** — an absence of information. A skip never renders a ✗, never triggers `rollback_on_fail`, and is never written to the cache. It exists so that a checker which *cannot* answer says so, rather than emitting a verdict that looks exactly like one it worked out.

**`NOT CHECKED` is the same absence arriving through the error channel.** A `skipped` is a checker that declined *before* running and had a field to say so with. An adapter that was asked to run, could not, and holds only an `adapter`-coded error has produced exactly as little information — and every consumer downstream of a result treats an error as a measurement of the file. So a result whose errors are *all* `code: "adapter"` is rendered as `NOT CHECKED`, is never subtracted from a baseline, and is named on the `[result]` line. All of them, not the first: an adapter reporting four real findings alongside one adapter row has still measured the file, and hiding that would be this defect pointing the other way.

Whether it exits non-zero is decided by `$SUPERTOOL_REQUIRE_VALIDATORS`, one section up — that variable is the only place an operator states which absences are unacceptable *here*.

**Daemon not installed — the checker that cannot run declines** ([#531](https://github.com/Digital-Process-Tools/claude-supertool/issues/531)). The four warm-process adapters (`phpstan-mcp`, `rector-mcp`, `phpmd-mcp`, `phpunit-mcp`) resolve their daemon binary before spawning it. When it is absent — the normal case for any `cwd:` pointed at a git worktree, where `composer install` never ran — that used to surface as a finding:

```
phpstan-mcp : 1 err  (pre-existing — not from this edit)
     adapter  RuntimeError: mcp-phpstan-warm not found at: /tmp/.../wt-foo
```

A checker reporting on a file it never opened. In a `batch:` of six edits that is eighteen lines of false failure competing with the real validator output, which is the thing the block is being read for. It now reads:

```
phpstan-mcp : skipped     (mcp-phpstan-warm not found at: /tmp/.../wt-foo)
```

**The distinction is made at the raise site, not by matching the message.** `refusal.DaemonUnavailable` is raised only where the binary is looked for and found absent — a missing file at a configured path, or a bare name absent from `$PATH`. Everything that happens *after* a real binary is found — a spawn that dies, a handshake that times out, a daemon that answers badly — remains a `1 err` with an `adapter` code. Guessing towards silence there is how a genuinely broken validator starts looking clean, which is a worse failure than the noise this removes. `DaemonUnavailable` subclasses `RuntimeError`, so existing handling around `resolve_bin` is unchanged.

The skip reason keeps the install command (`composer require --dev dpt/mcp-phpstan-warm`, …) on one line, so a validator that is off because nothing is installed is still discoverable from the row that says so. `phplint` is unaffected and still runs in worktrees — it is what actually catches syntax problems there.

**No transport is the same absence, one layer down — and warm validators do not run on Windows.** The warm analysers are spoken to over a Unix domain socket, and GH-hosted Windows Python builds do not expose `socket.AF_UNIX` even though the OS supports it. They never worked there. What they did instead was walk in anyway and publish the crash as a finding: `_spawn.ensure_daemon` resolves the socket path before running its preflight, that reaches `_paths.runtime_dir()`, and `runtime_dir()` calls `os.geteuid()` — which Windows also lacks — so every `edit:` and every `batch:`, per file, per validator, produced `adapter  AttributeError: module 'os' has no attribute 'geteuid'` (#544). All four adapters now decline through the same `DaemonUnavailable` marker:

```
phpstan-mcp : skipped     (warm daemon needs socket.AF_UNIX, which this Python build does not expose (Windows) …)
```

**Say it plainly: this disables warm validation on Windows.** That is not a regression — there was never a working warm validator there to lose — but it is a stated absence now rather than a fabricated finding, and `phplint`, `psr`, `xmllint` and the other cold validators are untouched and still run.

**The binary lookup goes first, and the platform check goes second.** Both are skips, so neither invents anything, and the ordering is chosen on which sentence a reader can act on: if the daemon is not installed, "install it" is the next step whatever the platform; once it *is* installed, the transport is what they hit. The reverse order was tried and it made "missing binary" an unreachable outcome on Windows, which forced two tests to be skipped there.

**Where the check sits is a contract, not an implementation detail.** It lives in the body of each adapter's `ensure_daemon`, never at the top of `main`. Several suites stub the daemon layer by replacing that whole function, which is why they never needed a real socket; a platform check placed ahead of that assignment fires before the stub takes effect, and those suites go green while exercising nothing. The first attempt at this fix did exactly that and silenced the guard asserting *a real daemon failure stays loud* — reproducing the bug that guard exists to prevent. `tests/test_windows_warm_validators_544.py` pins the placement by stripping `AF_UNIX` *and* stubbing the daemon, and asserting the stub was reached.

**The ownership check that crashed now refuses rather than passing.** `runtime_dir()` verifies the per-user runtime directory belongs to us (#148). Where `os.geteuid` does not exist that comparison is not merely unavailable, it is unanswerable — `st_uid` is a constant `0` on Windows and carries no ownership information at all. Defaulting it to "ours" would trade a loud failure for a quiet one on the single check whose whole job is to be suspicious, so it exits with a stated reason instead. No adapter reaches it on such a platform any more, but `stop.py`, `status.py` and `supertool.py`'s own MCP client call it too, and the next caller meets a sentence rather than an `AttributeError`.

**A loose runtime-dir mode is a finding, not a skip — so it refuses** ([#568](https://github.com/Digital-Process-Tools/claude-supertool/issues/568)). `runtime_dir()` also creates the directory the warm daemons live in, and it must be mode `0700` because on Linux that is what gates a co-tenant's `connect()` to the socket (#148). It used to `chmod` towards `0700` inside `except OSError: pass` and continue regardless, with the requirement stated in the comment above and enforced by nothing. It now creates the dir `0700` in the first place and `os.stat`s the result. The three states decide the handling and they decide it the other way from #544: `st_uid` on Windows is *unanswerable*, so that one declines; a mode read back as `0o755` is an answer, so this one is a finding and a finding on this check means refuse. Warning and proceeding was the alternative and it is the failure mode both #544 and #551 are about — a security check whose output nobody has to act on is indistinguishable from one that keeps passing. The cost is stated in `docs/mcp-integration.md`: an `exFAT`/`FAT32`/SMB `SUPERTOOL_RUNTIME_DIR`, where a `chmod` is expected to be a no-op, is now refused rather than silently unprotected, and the message names the mount option that fixes it.

**A refusal a validator cannot act on is a skip, not a death.** `runtime_dir()` refuses with `sys.exit("<reason>")`, and `SystemExit` derives from `BaseException` — so `_mcp_ensure_server`'s `except (OSError, MCPServerError, MCPTimeout, KeyError)` does not catch it, and neither would a bare `except Exception`. That handler returning `None` is the whole mechanism behind every warm-validator skip and every heuristic fallback, so an escaping refusal would not decline the check, it would kill the invocation. #544 hoisted the `AF_UNIX` case one step earlier in `MCPClient.__init__` for exactly this reason; a mode can only be learned by looking at the directory, so it cannot be hoisted, and it is translated to `MCPServerError` at the same boundary instead. The adapters then decline the way they already do for a daemon that is not installed, and the cold validators (`phplint`, `psr`, `xmllint`) are untouched.

**The post-edit lint on a `vim` receipt declines the same way.** It is not a validator — it is the extension-matched syntax check (`php -l`, `xmllint`, `py_compile`, JSON parse) printed at the foot of the receipt — but it has the same three states. A check that does not return inside `SUPERTOOL_LINT_TIMEOUT` (default **5 seconds**, raise it by exporting a larger integer) prints `POST-EDIT LINT TIMED OUT — <tool> (<N>s)` and says the file was **not** checked, rather than falling silent. Silence in that section means "clean" and must keep meaning only that. The 5s budget is deliberately loose — `xmllint --noout` on a small file costs single-digit milliseconds — so exceeding it points at the machine, not the file; on a loaded CI runner it is worth raising for the run rather than reading the decline as a failure ([#553](https://github.com/Digital-Process-Tools/claude-supertool/issues/553)).

**A git call that does not answer is a third state too — not an absence in the repo** ([#650](https://github.com/Digital-Process-Tools/claude-supertool/issues/650)). Two places read git to describe the repo, and both used to collapse "git did not answer" into an existing answer. `presets/git/status.py` let the `TimeoutExpired` escape, so one stalled `rev-list` took the entire report with it. `supertool._current_branch()` folded a timeout into `""` — the same value that means "there is no branch here" — and `_branch_line()` renders `""` as silence, so a mutating op's receipt silently dropped its branch on exactly the run where the caller was least sure what state the repo was in. That is the wrong direction to be wrong in: the branch line exists to catch right-file-wrong-branch. Now `_branch_reading()` returns `(branch, why)` and the receipt prints `[branch: UNKNOWN — <what stalled>]`, `git-status` skips the affected section and names it in a trailing `git-status INCOMPLETE` line, and each abandoned call carries exit code `124` so no call site mistakes it for a git that succeeded and printed nothing. The budget is `SUPERTOOL_GIT_TIMEOUT` (default **5 seconds**). **A missing `git` binary deliberately stays in the middle state**, silent rather than declining: nothing on that machine was ever going to name a branch, which is the same honest silence the post-edit lint draws for an uninstalled checker — a decline that can never resolve is noise on every receipt of every op.

**The same file, twice more — and a third state one token wide** ([#705](https://github.com/Digital-Process-Tools/claude-supertool/issues/705)). #650 fixed the class in `presets/git/status.py`; an audit of the file it had supposedly finished found two call sites that never reached the footer it added. The `glab`/`gh` MR lookup ran its own `subprocess.run` and swallowed every failure into `pass`, so a network stall, an expired token and an unauthenticated CLI produced byte-for-byte the report of a branch that simply has no MR yet. And `supertool._path_meta_suffix` — the `m`/`?`/`!` marker on every `read` receipt, the most-used op in the tool — dropped the marker when its `git status` failed, so a modified file rendered as clean. That one inverts the marker's whole job: it exists to say the file on disk differs from the index.

The second is the interesting one, because the field looked like it had no room for a third state. It does: the suffix is a space-separated token list whose members already run to `non-utf8`, `crlf` and `->target broken`, so the one-character appearance came from its busiest members, not from a constraint. The decline is `git?` — it names the check that declined and cannot be read as any of the three answers it replaces, none of which mention git. A free punctuation mark was the alternative and was rejected on the grounds that made it free: a character nobody has a meaning for yet is the wrong carrier for the token that has to say the most. **Absence still means clean, and now means only that.** The noise cases stay silent by the same reading that keeps a missing `git` binary quiet — a file outside any repository (`not a git repository` is an answer, and the common one), and a machine with no `git` at all.

**An adapter that blows its own budget is a finding, and it has to reach stdout to be one** ([#702](https://github.com/Digital-Process-Tools/claude-supertool/issues/702)). Every adapter wraps its tool in a `timeout=`, and three of them — `hadolint`, `markdownlint`, `tsc-check` — had no handler for it expiring and no top-level `except` either. The process died on a traceback with **empty stdout**, and the caller `json.loads()` that: a slow linter surfaced as a `JSONDecodeError` naming neither the tool nor the timeout, which is the same three-state collapse as #650 one layer over — "could not answer" arriving as a crash instead of as an answer. All three now emit `ok: false`, `code: "adapter"`, and a message naming the budget and stating the file was **NOT** checked. It is a finding rather than a skip by the rule two paragraphs above: the binary was found and the process started, so everything after that is a `1 err`, and guessing towards silence there is how a genuinely broken validator starts looking clean. **The test-side mirror of the same rule** — a test's timeout on an adapter must exceed the adapter's own, or it is measuring the machine rather than guarding a hang — is in `docs/contributing.md` §"Never write a subprocess timeout by hand", with the reason those tests fail rather than decline.

**And the third state reaches the test that reads that finding** ([#794](https://github.com/Digital-Process-Tools/claude-supertool/issues/794)). The paragraph above is right that an adapter blowing its own budget is a finding and not a skip — the process ran, and `ok: false` with `code: "adapter"` is what reaches the caller. But a *test* whose question is "does this adapter report a clean two-line PHP file as clean" has, on receiving that finding, obtained no answer to its question. It asserted the finding was a verdict, and failed with `expected a clean verdict on a two-line PHP file with nothing wrong with it`, on a Windows leg of a pull request about a GraphQL query builder. The file was fine, the adapter was fine, and the runner was 8.5 minutes into a suite.

The three states apply one layer out: `tests/_adapter_verdict.py` grows `stalled_at_its_own_wall()`, and the one end-to-end spawn test declines through `assert_adapter_ok_or_skip_if_stalled()` rather than failing. The skip reason carries the rendered verdict and the measured duration, because a 30s spawn is still worth knowing about — it is just not worth publishing as a defect in someone else's diff.

**Where this stops.** The adapter's own behaviour is unchanged: routing a fault to `skipped` would still be a validator quietly reporting clean, and SCHEMA.md's rule above stands. A blown *outer* budget still raises `TimeoutExpired` and still fails loudly, because that means an adapter ignored its own timeout — a genuine hang. And the predicate requires every error to be an `adapter`-coded timeout whose `duration_ms` reaches the adapter's internal budget, so a missing binary, an unreadable argv, a mislabelled instant fault, and a real `parse` finding all still redden the board. A skip that fires on anything other than a wall the adapter demonstrably spent is a mute button, which is the trade this repo declines every time.

**A checker that could not run is not a checker with nothing to say — and `git-resolve`'s digest was reading the exit code of neither** ([#883](https://github.com/Digital-Process-Tools/claude-supertool/issues/883)). The post-resolve syntax digest shells into the `validate` op and folds its `validate: PATH` blocks back onto files. `None` is its "the validators ran and none of them handles this file type" — a real answer, which the receipt deliberately prints as nothing, so `markers: clean` stands alone. Four other outcomes were reaching the reader as that same nothing, or worse:

| what happened | what the receipt said, before | after |
|---|---|---|
| child exited non-zero (a containment refusal, a crash) | `not checked (validator output had 0 block(s) for 1 file(s))` — the fold blamed for the child's death, and no stderr | `not checked (validator exited 1: <first stderr line>)` |
| child killed **after** a complete reply | `validate: ok` | `not checked (validator killed by signal 9)` |
| `no validators configured` | nothing — reads as clean | `not checked (no validators configured)` |
| `@syntax` and the fallback name list both selected nothing | nothing — reads as clean | `not checked (no syntax validator selected)` |

**The second row is the one worth the change.** The block-count guard from #881 caught the *common* shape of a dead child by accident — zero blocks for N files — so the first row was already visible, if misattributed. A child killed after flushing its last block passes that guard with the right block count and every row reading `ok`, and the digest then affirms a clean syntax check about a process that did not survive its own run. `returncode` was the fact that separates them and the function had it in hand.

**The last two rows are not errors, and they are still not silence.** The child ran fine and reported honestly that it had nothing configured to run. That is a different fact from "none of these validators handles `.txt`", and rendering the two identically is what let a config with no validators report every resolved conflict as clean — with, in the test that used to assert this behaviour, a file that does not compile. Two tests asserted `None` here before #883 and both fed `no validators configured` under a docstring describing the per-file answer; they now assert the per-file answer against the shape that actually means it (a block with no rows), which stays `None` and stays quiet. That quiet is the point of the distinction, not a casualty of it: an ordinary `.txt` or `.md` in an ordinary config still prints `markers: clean` and nothing more.

**The reason line is bounded and flattened, because a failed child's stderr is untrusted text on a line this tool owns.** It is interpolated into `markers: clean | {digest}` at column 0, so it goes through `presets/_untrusted.flat` — the call, not a second copy of the rule — and is cut to 120 characters. The first non-empty line only: the wording has to distinguish *could not run* from *nothing to say*, and it does not have to diagnose.

**A guard refuses when every intent behind it has another spelling; otherwise it warns** ([#834](https://github.com/Digital-Process-Tools/claude-supertool/issues/834)). The three states above are about a checker that cannot answer. This is the neighbouring question — a checker that *can* answer, and has to choose how loud to be — and the payload route is where it was settled. A `'''` block whose content ends with a backslash is a caller escaping the delimiter out of reflex; in a literal block the backslash is inert, so the op wrote it, `py-syntax` agreed the file parsed, and the caller met the damage in a Python traceback two layers away. It is now refused at parse time, before anything is written.

**The reason it is a refusal and not a warning is not that it is certain — it is that nothing becomes unwritable.** Both readings of that backslash have another spelling: drop it and let the closing run carry the quote (`'''kind = 'mr''''`, legal TOML), or move to a `"""basic"""` block where a genuinely wanted backslash doubles. The message prints both, built from the caller's own line rather than from an example. Where a refusal *would* strand a legitimate intent, the honest severity is a warning — which is why the sibling guard on `\\` at end-of-line in a shell payload ([#835](https://github.com/Digital-Process-Tools/claude-supertool/issues/835)) is a harder call than this one and is not decided here: a doubled backslash in bash is sometimes exactly what was meant, and refusing it needs an opt-out to exist first.

**The issue as filed proposed the opposite guard, and it would have refused its own fix.** #834 reads "a TOML literal string cannot end with the character that delimits it" and asks for content ending in `'` to be refused. A multi-line literal ends with one or two apostrophes perfectly well — the closing run absorbs them — and that is precisely the spelling the refusal message recommends. A guard fired on the symptom would have made the correct payload unwritable while leaving the backslash that actually broke the write untouched. Checking the premise is part of the fix; `tests/test_payload_literal_backslash_834.py` pins both halves, and the surplus-quote table is held to `tomllib` the way #684's escape table is.

**And the sibling guard the paragraph above left undecided — the rule splits it rather than promoting it** ([#835](https://github.com/Digital-Process-Tools/claude-supertool/issues/835)). `_sh_backslash_warning` (#380) flags a `.sh` line ending in an *even* run of backslashes: bash consumes them pairwise, so the line genuinely ends and the continuation the caller thought they wrote is an escaped backslash followed by a new command. It parses, `bash -n` and `bash-check` agree, and the embedded Python dies a language away. The diagnosis was right and the write went through, which is the issue: *a guard that is certain should stop the write; a guard that is heuristic should warn. Mixing the two in one channel costs the certain ones their authority.*

The severity is still not decided by certainty — it is decided by the test above, and the same bytes get different answers depending on the route they arrive by. **Out of a `'''literal'''` payload block they are refused**, at parse time, before any op of a batch has run: both readings have another spelling (write one backslash, since a literal block eats nothing; or move to a `"""basic"""` block, where a wanted pair is spelled with four), so refusing leaves nothing unwritable. **Everywhere else they stay a warning**, because at the write chokepoint there is no second spelling at all — the check reads whole-file content, so one deliberate `echo \\` on line 400 would make every later edit to that script impossible, including the one that would remove it, and the colon CLI has no fields with which to say otherwise. The backslash-then-whitespace half stays a warning even from a literal block: a basic block writes an escaped space exactly as a literal one does, so that reading has nowhere to be sent.

**The opt-out the issue proposed is the tell that it would have been a warning.** `allow_literal_backslash = true` is a new public field on the payload format, unrenameable once shipped, bought to re-enable what the format already expresses — and it answers "stop asking" where the basic block answers "I meant two". Where a guard needs a flag invented for it, the honest severity was a warning; where the alternative spelling already exists, the message names it and no flag is needed. That is the same check that made #834's guard correct, and it is what decided which half of this one refuses.

**And a tool that exited non-zero has not necessarily said anything about the file** ([#745](https://github.com/Digital-Process-Tools/claude-supertool/issues/745)). `php -l` exits non-zero for a file that does not parse and for a PHP that could not start — a fatally failing extension, a path it could not open (`Could not open input file`, exit `1`). `phplint` mapped every remaining non-zero exit to `code: "parse"`, so "your toolchain is broken" and "your file is broken" arrived as the same sentence, and the `line` came from an `on line (\d+)` search across the whole output. That search is where it got concrete: PHP prints startup warnings *before* any parse error and terminates them with `in Unknown on line 0`, so a broken extension plus a genuinely broken file reported the syntax error at **line 0**, with source context rendered for a line that does not exist.

The rule: php is credited with having spoken about the file when its output carries a lint diagnostic — the linter's own `Errors parsing <file>` verdict line, or a `Parse error:` / `Fatal error:` banner (`php -l` reports compile-time fatals as the latter). Otherwise the exit is `code: "adapter"` with a message naming the exit code and the raw output. The line number is read from the diagnostic line itself rather than from the output at large, so a startup warning cannot donate its line 0 to a real finding.

**Ambiguity falls towards the file, and this is a finding rather than a skip.** A located `in <file> on line N` with no banner the classifier recognises still counts as a finding: a PHP whose message shape nobody anticipated must not have its findings relabelled out of the reader's list. The asymmetry is what makes that cheap — an `adapter` result is still `ok: false`, still one error, still rollback-triggering, and its message carries the exit code and the raw output, so a fault misread as a parse error stays fully legible, whereas a real syntax error relabelled `adapter` sends someone to audit a toolchain that is fine. Nothing is dropped in either direction; only the label and the invented line move. It is not `skipped` for the reason two paragraphs above: the binary was found and the process ran, and a fault routed to the third state is a validator quietly reporting clean.

**Naming it correctly is also what stops it being cached.** `code: "adapter"` joined `_NONDETERMINISTIC_ERROR_CODES`, because a verdict the adapter never obtained is by construction not a function of the file's content — and the cache key is a content hash. A toolchain broken for ten minutes would otherwise freeze a red that replays until someone edits the file, which is the 2100-poisoned-entries incident under §Caching. Before this the exits arrived wearing a finding's code and were cached on purpose. `validators/common/refusal.py:tool_fault()` builds the message; it is the sibling of `skipped()` on the other side of the same distinction — one analyser declined before running, the other ran and fell over.

**The same rule, six adapters over — each with its own vocabulary** ([#753](https://github.com/Digital-Process-Tools/claude-supertool/issues/753)). #745's sweep found the defect in six siblings and filed rather than half-fixed them, because gofmt does not talk like node does not talk like cargo and a regex written against an imagined error message is worse than no change. The marker in each case is a **located diagnostic in that tool's own format**:

| adapter | credited with having spoken about the file when | what it used to publish instead |
|---|---|---|
| `xmllint` | stderr carries `file:LINE: parser error : …` | a path typo, an I/O error and a usage dump as `code: "xml"` |
| `bash-check` | stderr carries `: line N:` | exit `126`/`127` — the shell's own "could not execute" codes — as `code: "syntax"` |
| `node-check` | a location line whose path resolves to **this file**, or a `SyntaxError` banner | `Cannot find module` as a syntax error at **line 1386**, node's loader frame, with source context |
| `gofmt-check` | stderr carries `path:line:col: message` | every non-zero exit as one hardcoded `code: "syntax"`, `stat …: no such file` included |
| `terraform-check` | a diff body on stdout (formatting), or `on <file> line N` in an Error block (syntax) | all four exit-2 failures as "file needs terraform fmt formatting" |
| `cargo-check` | output carries a short-format `file:line:col: error[…]` | an unparseable `Cargo.toml` as a Rust `code: "compile"` in a `.rs` file the compiler never reached |

`ruby-check`, the model #745 held up, turned out to carry the same defect in the branch its own comment did not cover: it routed an *empty* stderr to `adapter` and left non-empty-but-unlocated output as `code: "syntax"`, which is what `ruby -c` on a missing path produces. It is fixed here too.

**Two things the vocabulary work made concrete.** Exit codes are evidence where a tool documents them — `terraform fmt -check` returns `3` for "needs formatting" and `2` for "I failed", a distinction the adapter had never read — and a classifier that has to find a location can hand one back: `gofmt` prints `line:col` that the old branch discarded, so the same change that stopped inventing a location for `stat` failures started reporting the real one for parse errors. **Ambiguity keeps falling towards the file:** a `SyntaxError` node's report shape cannot place stays a finding, and is reported with `line: null` — reclassifying is one fix and inventing a line is another, and a finding that cannot be placed is better placed nowhere than at a number borrowed from the tool's internals.

**A guard with no rules is not a guard that found nothing** ([#693](https://github.com/Digital-Process-Tools/claude-supertool/issues/693)). The three states are usually reached from a call that failed. This one is reached from a check that ran perfectly over an empty rule set: `git-diff`'s forbidden-path guard read `forbidden_paths` from project config, most repos have never written one, and zero rules produce zero hits, which printed `✓ No red flags, forbidden paths, or missing tests.` The shipped default *was* the always-passing state.

Which of the three states applies is a judgement, and here it went against a refusal. `radar` refuses to run when unconfigured and that is the nearest precedent in this repo — but `radar` is opt-in machinery, and `git-diff` runs before every commit. A refusal on every unconfigured repo is a checker people stop running, and an abandoned checker reports nothing at all; the cure would be worse than the defect. Disclosing "guard not configured" resolves nothing either — a permanent disclaimer that still flags no file. **Shipping a default rule set is the option that changes the state nearly every user is actually in**, so `DEFAULT_FORBIDDEN_PATHS` now covers secret-shaped filenames and the guard has something to check on a repo that has configured nothing. The defaults are tuned to stay quiet on files projects commit deliberately (`.env.example`, `id_rsa.pub`), because a default that cries wolf gets configured away and returns the user to the always-passing state by a longer route.

Two smaller rules fall out of the same call site. **A config value that could not be parsed is a finding, not an absence** — absent and malformed both returned `[]`, so a typo silently disabled a guard and the run still printed the clean verdict; a malformed value is now reported as `⚠ Policy not loaded`, naming the key, and the shipped defaults keep running underneath it. And **an affirmative verdict may only name the checks that ran**: with no `test_pairing` rules the line reads `✓ No red flags or forbidden paths.` and the unchecked leg is simply not mentioned. Not disclaimed — mentioned nowhere. A "not checked" line on every run of every unconfigured repo is the permanent disclaimer this section keeps declining; the fix is for the affirmative claim to shrink to what was established, not for a caveat to be appended to it.

**The same shape, one layer below a security boundary.** `_sanitize.detect()` returning `[]` printed nothing, so a scan that found nothing and content that was never scanned produced identical output; `wrap()` now leads with `[scan] no known injection patterns matched (heuristic — not a guarantee)`, and the parenthesis is load-bearing — a heuristic reported as a guarantee is this same defect one level up. `safe_short()` does not carry it, because it renders once per row of every list. And `transport.claim_pidfile` returned `0`, meaning "you own this slot", from paths where the `os.open` had failed and no file existed: an unknown that renders as the *strongest* available answer, on a check whose whole job is exclusivity. It has a `CLAIM_UNKNOWN` third value now, and callers spawn nothing on it.

**A checker that applies and cannot be started declines too — it does not fall silent** ([#559](https://github.com/Digital-Process-Tools/claude-supertool/issues/559)). `POST-EDIT LINT DECLINED — <tool>` names the tool, the reason, and says the file was **NOT** checked. The empty section is reserved for the one absence it can honestly describe: no checker applies to this file, because the extension is unknown or the binary is not on `$PATH`. A `.py` file has a checker, so failing to source an interpreter for it is a decline, not silence. The interpreter is `sys.executable` and never a `$PATH` lookup of `python3` — on Windows that name resolves to the App Execution Alias stub, which blocks rather than errors, or to nothing at all ([#529](https://github.com/Digital-Process-Tools/claude-supertool/issues/529)), and either way a valid file collects a verdict nobody computed.

**Every decline carries the "file modified and NOT checked" note in the `vim` receipt** ([#560](https://github.com/Digital-Process-Tools/claude-supertool/issues/560)). The post-edit lint is informational and never rolls back, so on a decline the file has been written and nothing has read it. The note that normally means "there is follow-up work here" used to appear only on a syntax failure, which left the least-verified state as the quietest one. It is worded for that state rather than reusing the failure sentence: nothing failed, nothing ran.

**`warm_unsafe` — targets a warm process cannot judge.** Warm-process validators (`phpstan-mcp`, `rector-mcp`, `phpunit-mcp`) keep a long-lived daemon so each edit does not re-pay a cold boot. That daemon carries state, and for some targets the state is the answer. The case that produced this field (#345): `mcp-phpunit-warm` runs the project's `phpunit.xml` bootstrap in its long-lived **parent** and forks a child per test call, so every resource that bootstrap opened — a DB handle, a session, a framework singleton — is shared across the parent and all children. A test that touches one of them can fail under the daemon and pass under a cold `phpunit` run of the same file at the same commit. That red is not a fact about the file, and there is no way to tell it apart from a real one by reading the output.

So the project declares which targets are out of the warm runner's reach:

```json
// .supertool.json
{
  "validators": {
    "phpunit": {
      "cmd": "... phpunit-mcp.py {file}",
      "resolve": "bash .claude/scripts/validators/resolve_test.sh {file}",
      "warm_unsafe": ["extends\\s+SiControllerTestCase"]
    }
  }
}
```

Each entry is a Python regex matched against the content of the **resolved** target — the file the adapter would actually have been given, not the file you edited. A hit short-circuits before the adapter is spawned at all:

```
phpunit     : skipped     (warm-unsafe: target matches /extends\s+SiControllerTestCase/ — this va)    -
```

**What this costs.** A warm-unsafe target is not validated inline *at all* — its greens are declined along with its reds, because a green from a runner you have just proved unreliable is worth no more than its red. Run those files through the cold op (`phpunit:PATH`, `phpstan:PATH`) or leave them to CI. The trade is deliberate and is the same one #482 made: a visible gap beats an invisible wrong answer.

**What this is not.** It is not suppression of "pre-existing" failures. A pre-existing failure is a real failure, and hiding it is how a broken file starts looking clean; regression-only rollback (below) already handles those correctly by comparing against a pre-edit baseline. `warm_unsafe` addresses a different problem one layer up — a result that is not about the file at all.

**Failure modes bias towards running.** An unreadable target, a pattern that is not a string, and a pattern that does not compile are all ignored, and one bad pattern does not disarm the good ones beside it. The failure mode of a muting feature is a validator that has silently stopped working, so a config typo leaves the validator running rather than quietly turning it off. Target reads are capped at 256 KB.

## Manual run

Run validators explicitly against any file without an edit op:

```bash
./supertool 'validate:src/Foo.php'                    # all matching validators
./supertool 'validate:src/Foo.php:phplint,phpstan'    # filtered to named validators
./supertool 'validate:a.php,b.php,c.php'              # list form — many files, config loaded once
./supertool 'validate:a.php,b.php:@syntax'            # only parser/compiler validators
```

Useful for a pre-commit sweep or spot-checking a file you didn't edit this session.

**List form** — pass a comma-separated path list (`validate:f1,f2,…[:tool_filter]`) to validate several files in one invocation; the config is loaded once for the whole batch and each path is independently security-checked at dispatch. Single-file `validate:PATH` is unchanged.

**`@syntax` filter** — the special filter `@syntax` selects only validators that declare `"syntax": true` in their spec (the parser/compiler tier), keeping that scope in config rather than a hardcoded caller list. Falls back to the bundled name list for older configs that predate the flag.

### What `validate:` output guarantees

The guarantee is stated over the **block** — the header *and* the rows under
it. It was written for the header first, and for one release it held only
there; the row half is [#895](https://github.com/Digital-Process-Tools/claude-supertool/issues/895)
below.

**One `validate:` line per file, at column 0, whatever the files are called** — this is a contract a parser may rely on, and since [#881](https://github.com/Digital-Process-Tools/claude-supertool/issues/881) it is one. It was not before: the header echoed the path verbatim, a filename may contain newlines on Linux and macOS, and a file named `evil␊validate: forged.py␊ok          : ok␊.py` emitted **three** headers for one file. `presets/git/resolve.py` folds blocks back to files positionally, so the extra headers shifted every subsequent file onto somebody else's rows and a file with a real syntax error digested to `validate: ok`.

The path in the header is now flattened through `_untrusted.flat` — the same call the `git-worktrees` board uses, one guarantee with one implementation. An ordinary path is echoed exactly as typed; a control character is shown as itself rather than acted on, so nothing is censored and no character is lost. **Validators still run against the real, unflattened path** — the flattening is on the echo only.

**The set that guarantee is measured against is `str.splitlines()`, not the newline** ([#886](https://github.com/Digital-Process-Tools/claude-supertool/issues/886)). #881 shipped covering C0, DEL and C1 — eight of the ten separators Python splits a line on. U+2028 LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR are neither, and passed through, so the forgery above still worked with those two characters in place of the newlines and the contract stated here was false for a release. The rule the flattener now follows, and the question to ask of any future consumer of this format: **whatever the reader splits on is what the emitter must neutralise, exactly** — not a subset chosen from the characters somebody thought of.

The counterpart at the reading end: a fold must not `zip`-truncate. `_validate_paths` refuses a block count that does not match its file count and renders `validate: ⚠ not checked (validator output had N block(s) for M file(s))` for the whole batch, rather than attributing rows to files it cannot account for. **It is not unreachable, and #884 was wrong to call it so** — the U+2028 gap above reached it, and that guard is the only reason that gap was a denial rather than a second forged clean.

**And the reader must not take a filename for the emitter's own words** ([#888](https://github.com/Digital-Process-Tools/claude-supertool/issues/888)). `op_validate_multi` answers "no validators configured" / "no validators matched filter" *instead of* every block, never beside one, so a status message is an output that carries no `validate:` header at all. `_validate_paths` reads those two strings only when it has parsed zero blocks. It used to substring-test the combined stdout, which carries one header per file — so a single file named `no validators.py` returned `None` for every file in the batch, `None` renders as `markers: clean` with no digest line, and a file with a real syntax error was staged and reported clean. Flattening a header stops a filename adding a line; it never stopped one containing a string. A longer or more specific substring would have been the same defect with a smaller target.

**The rows carry the same guarantee as the header, and did not for a release** ([#895](https://github.com/Digital-Process-Tools/claude-supertool/issues/895)). #886 fixed the header and stopped there. `_validator_render_row` was still sanitising an adapter's `msg` with `.replace("\n", " ")` — **one separator out of the ten**, the exact defect #886 had just fixed one layer up — and appended `resolved_to`, which is a path, with no flattening at all. That matters because the shipped subprocess adapters echo their input: `xmllint` reports xmllint's stderr, `tsc-check` reports `output[:300]` raw, `phpstan` reports `m["message"]`, `ruff` and `yaml-check` likewise. A file named `a<U+2028>validate: forged.q` therefore got a correctly flattened header and then wrote a **second, forged one out of the row below it**, naming a file nobody checked.

Every adapter-supplied string these renderers put on a line of their own now goes through `_flat_cell`, which is `_flat_field` plus the strip and the width bound a row adds — `msg`, `resolved_to`, `source_context`, and also `tool` and `skipped`, which the report did not name. `tool` is the leftmost field on the row and `skipped` had no sanitising whatsoever; fixing three fields and leaving those two would be this defect one field over, which is the shape of the whole #876 → #878 → #881 → #886 chain. `_validator_render_diff` — the `[validators]` block after an edit — is included for the same reason: a different renderer, not a different guarantee.

`raw_stdout`, `raw_stderr` and `diff` in verbose mode are deliberately **not** flattened. Those are blocks, not fields: the reader asked for the tool's output verbatim, every line of them is emitted indented, and so none can produce a column-0 header. That is the same line `presets/_untrusted.py` draws between `scrub()` and `flat()`.

Severity was medium rather than high for one reason, and it is worth stating because it is the guard doing its job twice: in-tree, `presets/git/resolve.py`'s block-count check catches the extra header and renders `⚠ not checked` for the batch — loud, never a silent clean. The exposure is a human or an agent reading `validate:` / `op_validate_multi` output **directly** and taking a forged row for a pass.

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

## resolve — map a source file to its real target

By default a validator runs against the file the op touched. The optional `resolve` key lets it run against a *different* file derived from that one — the canonical case is "edited a source file, run its test." `resolve` is a shell cmd that takes the edited file (`{file}`) and prints the path to run instead:

```json
"phpunit": {
  "cmd": "... {file}",
  "match": "*.php",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "resolve": "bash .claude/scripts/resolve_test.sh {file}",
  "tier": "slow"
}
```

**Contract — supertool reads stdout only:**

- Non-empty stdout → that path becomes the validator's target.
- Empty stdout → the validator is **skipped** for this op (nothing sensible to run).

The exit code is **ignored** by the validator path. That is deliberate, and it's what makes the advisory below possible.

### advice — config-driven post-op hints

Autonomous flows that write only through supertool bypass the editor/git-hook reminders to do follow-up work (write a test, regenerate the XSD, …). The top-level `advice` block emits non-blocking `[advice]` lines after a mutating op when a rule matches:

```json
// .supertool.json
{
  "advice": {
    "newTest": {
      "hooks_into": ["paste"],
      "match": "*.php",
      "when": "new-file",
      "resolveFromValidator": true,
      "message": "new class without test"
    },
    "newComponent": {
      "hooks_into": ["edit", "paste"],
      "match": "*.php",
      "contains": "extends \\w*ComponentBase|implements \\w*IComponent",
      "message": "XSD/cache regen likely (dvsi_xsd + dvsi_clearcache)"
    }
  }
}
```

Each rule is gated by (all optional):

| field | meaning | default |
|-------|---------|---------|
| `hooks_into` | ops that trigger the rule | all mutating (`edit`, `paste`, `append`, `replace`, `replace_lines`, `vim`) |
| `match` | path glob | `*` |
| `when` | `new-file` \\| `existing-file` \\| `always` — gated on whether the file existed before the op | `always` |
| `contains` | regex tested against the **content the op added** (lines present after but not before) — fires only when the op *introduces* the pattern, not when the file already held it | — (no content gate) |
| `resolve` | a subprocess (a source→target resolver) emitting a would-be target | — |
| `resolveFromValidator` | reuse the first `resolve` cmd declared on a validator instead of duplicating it | `false` |
| `message` | the line shown; `{target}`/`{path}`/`{op}` interpolate. A message with no `{target}` gets ` — consider <target>` appended when a resolver produced one | `""` |

A `paste` that **creates a new `*.php` file** with no resolvable sibling test (the `newTest` rule above) appends:

```
[advice]
ℹ new class without test — consider tests/unit/SiX/FooTest.php
```

#### The resolve contract

A `resolve`/`resolveFromValidator` rule reuses the same `resolve` cmd declared on a validator, so the source→target path logic lives in exactly one place. Because the advisory needs the *would-be* target (which doesn't exist yet) while the validator path needs stdout to stay empty on a miss, the resolver signals the two cases on different channels:

| case | stdout | stderr | exit |
|------|--------|--------|------|
| target exists | target path | — | 0 |
| no target | *(empty)* | would-be target | 3 |

stdout stays empty on a miss → the validator still skips, unchanged. The miss target rides on **stderr**, flagged by **exit 3** → only the advisory reads it. Advisory only: it never blocks the write.

## Format-on-save

See [formatters.md](formatters.md) — formatters run after every edit, before validators, normalizing whitespace and style before the safety check runs.
