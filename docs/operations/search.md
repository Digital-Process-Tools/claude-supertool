# Search

Pattern-based ops for finding content across files or zooming into a known location. Reach for these when you don't know the exact file yet, or when you want context around a match.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `grep` | `grep:PATTERN:PATH` or `grep:PATTERN:PATH:LIMIT` | 10 results default, code + doc extensions only. Each output line is capped at 500 chars (`grep.max_line_chars`) with a `… (+N chars)` marker — see [per-line cap](#per-line-cap). **Auto-reads** full file if PATH is a concrete file under **both** the byte cap (< 20KB) **and** the line cap (≤ 60 lines, `MAX_AUTOREAD_LINES`) with a match; over the line cap it prints a `[auto-read skipped: > N lines — read:PATH:full to see it]` note instead of dumping the file. Append `:no-auto-read` to suppress entirely (parity with `glob`). |
| `grep` (path list) | `grep:PATTERN:A.py,B.py` | **Refused.** PATH is one path — a comma list is joined into a single filename. `git-resolve` accepts `PATH[,PATH...]`; `grep` does not, and now says so instead of blaming the CWD. Pass a directory, or one `grep` op per file (ops batch into one call). |
| `grep` (LIMIT 0) | `grep:PATTERN:PATH:0` | **Refused.** `0` = unlimited is the usual convention and supertool declines to guess between that and the default it used to substitute silently. Both readings have a spelling and the refusal now names each: `:all` for every match, or omit LIMIT for the default — printed as a number, resolved from `grep.max_results` at call time so a project that raised the cap is told its own. |
| `grep` (LIMIT `all`) | `grep:PATTERN:PATH:all` | Every match, no cap — see [`all` says which question you are asking](#all-says-which-question-you-are-asking). Works in the CONTEXT form (`grep:PATTERN:PATH:all:2`), in `grep_around:PATTERN:PATH:N:all`, and as `limit = "all"` in a payload. Refused in the CONTEXT slot, where it has no meaning. |
| `grep` (context) | `grep:PATTERN:PATH:LIMIT:CONTEXT` | Show CONTEXT lines before/after each match (like `grep -C`). Match lines: `path:lineno:content`. Context lines: `path-lineno-content`. Non-adjacent groups separated by `--`. |
| `grep` (count) | `grep:PATTERN:PATH:LIMIT:CONTEXT:count` | Return match counts per file instead of content. Output: `filepath: N matches` per line. |
| `grep_around` | `grep_around:PATTERN:PATH` or `grep_around:PATTERN:PATH:N:LIMIT` | Every match across files with N lines context (default N=3, LIMIT=10). Alias for `grep:PATTERN:PATH:LIMIT:CONTEXT` with sane defaults — useful for "show me how everyone uses this". Output capped at ~16KB (see below). **Fixed slots** — unlike `grep` and `around` it does not rejoin a `:` back into the PATTERN; see the note below. |
| `around` | `around:PATTERN:PATH` or `around:PATTERN:PATH:N` | Show N lines (default 10) before and after the **first** match of PATTERN in a single file. Uses line-numbered output like `read`. Output capped at ~16KB (see below). |
| `around` (line form) | `around:PATH:LINE` or `around:PATH:LINE:N` | Answered by `around_line`, with a receipt naming the call that ran. `around` is PATTERN:PATH and `around_line` is PATH:LINE — opposite argument order, same output — so this shape used to fail with `file not found: 1160`. Only applies where the literal reading is already an error: LINE must not name a real file and PATH must resolve. A genuinely numeric pattern with a real path still greps. |
| `grep` / `grep_around` / `around` / `read:PATH:::grep=` (saturating pattern) | `grep:\| \{:PATH`, `grep:^\|def op_:PATH`, `read:PATH:::grep=^\|x` | **Refused, on every route that takes a pattern** — one gate, so a new route cannot inherit a different answer (#1344). A top-level alternation branch that matches the empty string matches at position 0 of every line, so the whole pattern does — and `1000+ matches` reads exactly like a search that found a lot. Supertool rewrites bash-grep BRE alternation, so an escaped literal `\|` becomes a top-level `|`: with nothing to its left that branch is empty, and with `^` to its left it is `^`. Both saturate; the refusal names the offending branch. Use a character class for a literal pipe: `[|] \{`. Untouched: a branch inside a group (`colo(u|)r`, `(^|,)alpha`), a `|` inside `[...]`, and `^$|alpha` — `^$` matches a blank line and not `abc`, so that is a real search. |
| `around_line` | `around_line:PATH:LINE` or `around_line:PATH:LINE:N` | Show N lines (default 10) of context around a specific line number. Target line marked with `→`. |
| `between` | `between:SYMBOL:PATH` or `between:re:START:END:PATH` | Return a chunk of a file. **Symbol mode (default):** full body of a named function/method/class via tree-sitter (PHP, Python, JS, TS, Go, Rust, Java, Ruby — a `::`-qualified query stays one symbol rather than re-reading the call as `re:` mode, but it is matched literally against the definition's own name node — PHP `Foo::bar` does not resolve, pass the bare `bar`). SYMBOL may be written the way it reads in source — leading modifiers (`async`, `function`, `def`, `class`, `public static function`, …) and a trailing `(params)` are stripped and retried after the exact match fails, so `between:async function fillAndSubmit:helpers.js` resolves. **Pattern mode (`re:` prefix):** inclusive line slice from first line matching START regex to first line after matching END regex (language-agnostic). |

## Output cap

A large `:N` context on a file of long (e.g. minified) lines can over-fetch — one op can dump hundreds of KB and blow your context budget. `around:` and `grep_around:` cap their output at **~16KB**, truncating at a line boundary with a footer that points at the narrower tools (smaller `:N`, or `between:` for a whole symbol). Tune via `builtin-ops.around.max_bytes` / `builtin-ops.grep_around.max_bytes` in `.supertool.json` (or `SUPERTOOL_AROUND_MAX_BYTES` / `SUPERTOOL_GREP_AROUND_MAX_BYTES`). See [configuration.md](../configuration.md#builtin-ops).

## Per-line cap

A single pathological line — a minified bundle, a 7KB one-line `@extends Foo<array{…}>` PHPDoc — used to turn one match into a screenful. Every `grep` output line (match **and** context) is truncated at `grep.max_line_chars` (default 500, or `SUPERTOOL_GREP_MAX_LINE_CHARS`) with a `… (+N chars)` marker naming what was dropped, so widening is a deliberate choice. Normal-width lines are untouched. This is orthogonal to the byte cap below: the byte cap bounds the whole window, the per-line cap bounds one line.

**A cut line names the way back to it** ([#1489](https://github.com/Digital-Process-Tools/claude-supertool/issues/1489)). When any line was cut, a `note:` above the results gives the recovery call — `read:PATH:LINE-LINE` returns the whole line byte-exactly — with the first cut line's own coordinates as the example:

```
note: 2 lines cut at 500 chars — read:PATH:LINE-LINE returns a cut line byte-exactly, e.g. read:CHANGELOG.md:4942-4942
```

The marker said a cut had happened and nothing about undoing it, which is what a caller wants precisely when the cut line is the `edit` anchor they came for: the run that filed #1489 dropped to `python3` to read the raw bytes. The render is not lossy — measured byte-identical to raw on files from 500 to 20000 chars with tabs, trailing spaces and unicode — so the remedy already existed and was simply not written where the truncation happens.

`grep:` with an explicit `CONTEXT` argument (`grep:PATTERN:PATH:LIMIT:CONTEXT`) shares the `grep_around:` code path, so it is capped under the same `grep_around.max_bytes` budget. Plain `grep:` (no context) is unaffected — it has its own `LIMIT`/`max_results` bound.

## Truncation is stated, not implied

`(1 results in 1 files, scanned 118353 files, limit 1)` reads as a complete answer. It was not one — the second match was sitting just past the cap, and nothing in that line said so. A result that stopped at its limit now says which:

```
(1 results in 1 files, scanned 118353 files, limit 1 — TRUNCATED, 137 matches total)
```

**The marker states the scope, not just the fact.** `more matches exist` could not tell 21 from 500, and those warrant opposite next actions: at 21 you raise the limit and read them, at 500 the pattern is wrong and should be narrowed before anything is read. Three states, and they do not collapse into each other:

| Render                                                     | Means                                                    |
| ---------------------------------------------------------- | -------------------------------------------------------- |
| `— TRUNCATED, 137 matches total`                           | Counted, and that is all of them                         |
| `— TRUNCATED, 1000+ matches total (count capped at 1000)`  | Counting stopped at the ceiling; the number is a floor   |
| `— TRUNCATED, more matches exist, total unknown (…)`       | Nothing counted, and why — the delegated rtk path when its census could not answer, see below |
| no marker at all                                           | The answer is complete; the count above it is exact      |

## `all` says which question you are asking

`grep` has one shape for two questions, and its default limit answers the second with the first:

- *"show me some"* — orienting around a symbol. A limit is a **feature**; the top N are the useful ones.
- *"find every one"* — a rename, a security sweep, a call-site audit. A limit is a **wrong answer**, and the honest `TRUNCATED, 40 matches total` under it does not stop the reader treating the visible rows as the set. That is how a #907 sweep missed two call sites that a later pytest run found.

`all` removes the cap and says so on the count line:

```
(40 results in 1 files, scanned 1 files, limit all)
```

`limit all` and `TRUNCATED` are mutually exclusive by construction — a capped answer cannot render as a complete one, and vice versa. Raising the number instead (`:500`) is the same defect with extra steps: it is a guess that has to be high enough, and nothing on the line says whether it was.

Notes:

- **It never delegates to rtk.** `all` is a completeness question and the delegated path answers it by trimming, not by walking; the native walker is the only engine that can say `all` and mean it. (A *capped* delegated grep does now report an exact total — see [Delegated to rtk](#delegated-to-rtk) — but that is a count beside a trimmed body, not the whole body.)
- **`all` is a LIMIT and only a LIMIT.** `grep:PATTERN:PATH:5:all` is refused rather than read as either "limit 5" or "limit all": one of those runs a call nobody typed, the other ignores a token the caller believes changed something. Same for a third trailing token (`:5:2:all`), which the parser peels and never reads. **A third trailing *integer* is refused too, since [#1345](https://github.com/Digital-Process-Tools/claude-supertool/issues/1345)**: `grep:PAT:PATH:5:3:2` used to run limit 5 / context 3 and drop the `2` at exit 0 with no note — a well-formed answer to a slightly different question, which is the house defect in its quietest form. The identical argument had already been written down beside the `all` case and only `all` was closed. `all` outside the LIMIT slot keeps its own, more specific message. `grep_around` is `PATTERN:PATH:N:LIMIT` — context **first** — so `grep_around:PATTERN:PATH:all` is refused with the slot order named rather than an `int()` error. The token is matched exactly (`all`, not `All`) on both the colon CLI and the payload route.
- **`grep_around` keeps PATTERN and PATH in fixed slots, and a colon-bearing pattern is refused rather than rejoined** ([#1826](https://github.com/Digital-Process-Tools/claude-supertool/issues/1826)). `grep:PAT:WITH:COLON:PATH` peels the trailing numbers and rejoins everything left as the pattern; `grep_around` cannot, because three shipped guards read its slots by position — the containment gate takes the path from slot 2 statically, the `all`-in-N-slot refusal reads slot 3, and the fifth-token refusal counts them. So `grep_around:Class::CONST:PATH` puts `CONST` in the N slot, and until #1826 that surfaced as `ERROR: argument parsing: invalid literal for int() with base 10: 'CONST'` — the interpreter talking about the caller's search term. It is now a refusal that says how the call was read (`pattern=` / `path=`) and gives the payload spelling, `grep_around:@-` with `pattern`, `path`, `n`, `limit`. Note that `_colon_split_hint`'s `pattern read as` disclosure can never fire for this op — it needs a `:` in slot 1, and slot 1 cannot hold one — so the refusal carries the disclosure itself.
- **`grep:…:count` never needed it** — count mode has no cap and its header claims none.
- **`all` bounds matches, not bytes.** With a CONTEXT argument the op still shares `grep_around`'s ~16KB output cap, which cuts at a line boundary and names itself in a footer (`… truncated (~N more bytes)`). `limit all` is a claim about the match cap only; plain `grep:PATTERN:PATH:all` has no byte cap beyond the per-line one.

**The ceiling exists because counting is not free, and the number was measured rather than guessed.** Over a 67,855-file tree, a dense pattern's walk went from 0.01s (stop at `limit + 1`) to 10.3s (count everything); stopping at 1000 cost 0.05s. The bound is on *matches*, not files, so a sparse pattern still reads the whole candidate list — which is exactly what a **non**-truncated grep already does, so the op's worst case is unchanged rather than raised. Tune it with `grep.count_ceiling` / `SUPERTOOL_GREP_COUNT_CEILING`; it is never applied below your own `LIMIT`.

**The marker's absence is a claim, not a silence.** `grep` fetches *past* the limit and discards the surplus, so `limit N` with no marker means the walk looked for an N+1th match and did not find one — the count is exact. Stopping *at* N is not by itself evidence that an N+1th exists, and a marker that fired on `count == limit` would be a lie in the other direction: smaller, but still a lie, and still unusable, since every result would carry it.

**The over-fetch costs no extra traversal, and the ceiling did not change that.** The whole tree is already walked to produce the `scanned N` denominator, so the over-fetch only reads file *contents* further — to the `limit + 1`th match before the count existed, to the `ceiling + 1`th now. Measured on the same 67,855-file tree, dense pattern, `limit 20`: **0.0103s** stopping at `limit + 1` against **0.05s** stopping at the 1000 ceiling, both on top of a 4.3s traversal neither avoids. That is the honest before/after — the 10.3s figure above is the cost of the option that was *not* taken. On an exact result neither bound stops early at all, which is what proving exactness means.

`glob` carries the same disclosure on its own cap (`glob.max_results`):

```
(200 files — TRUNCATED, more files match)
```

## Hidden files are counted, not silently dropped

When `exclude-paths` drops a **file** from a result, the report line says so:

```
(2 results in 2 files, scanned 2 files, 10 files hidden by exclude-paths, limit 30)
```

`grep`, `glob`, `map` and `tree` all carry it. An exclusion that leaves no trace is indistinguishable from a file that was not there — the same silent-failure shape the `scanned N` denominator and the `TRUNCATED` marker exist to close. Credential files are why the list has file entries at all, but "your search skipped something" is yours to know, and `no-exclude` is one flag away.

**Only credential entries are counted.** Build output, caches and VCS metadata — `.git`, `node_modules`, `__pycache__`, `dist/`, `.venv/` — are skipped in silence: a counter that is never zero is noise rather than disclosure, and nobody searching a repo meant those.

The rule is **noise versus credential**, not file versus directory. Those two look identical until you are in a git **worktree**, where `.git` is a gitfile rather than a directory — so a file-versus-directory rule reported `1 files hidden by exclude-paths` on every single search in the tree, about a pointer file nobody was looking for ([#691](https://github.com/Digital-Process-Tools/claude-supertool/issues/691)). A number that is always `1` is one you stop reading, and then the search that says `2` because a real `.env` was hidden looks like all the others. Nothing is hidden any *less*: `.git` still never appears in a result.

A project's own `exclude-paths` entries always count, whatever they match. Supertool cannot know whether one is noise or a credential; over-disclosure is the safe direction, and whoever added the pattern is the person most likely to want to know it fired.

`grep:…:count` has no marker because it has no cap — it counts every match in every file, and its header (`N total matches across M files`) never claims a limit.

Its per-file lines carry a unit — `supertool.py: 30 matches` — because `PATH:NUMBER` is what every grep-like tool prints for `PATH:LINE`, so the bare `supertool.py:30` read as *one* match at line 30 ([#988](https://github.com/Digital-Process-Tools/claude-supertool/issues/988)). That is the opposite of what the op said, in the op you call before deciding whether to look at all.

## The pattern is a Python regex, always

There is no `re:` prefix on `grep`. Every pattern is already a regex, so a leading `re:` is literal text — and since `|` binds looser than concatenation, `grep:re:Checks|failed:PATH` searches for `re:Checks` **or** `failed`, and the first branch matches nothing ([#1065](https://github.com/Digital-Process-Tools/claude-supertool/issues/1065)). `between:re:START:END:PATH` is the op that has such a prefix, which is where the spelling comes from.

Because the colon CLI cannot tell where a pattern containing `:` ends, the tokens left of the path are rejoined with `:`. That is deterministic and documented, but it used to be invisible; a pattern containing a `:` now has the effective pattern echoed above the report line:

```
(pattern read as 're:Checks|failed', path as 'ci.log' — the ':' is part of the regex, not a separator. …)
(3 results in 1 files, scanned 1 files, limit 40)
```

The line names **both** halves of the split. Naming only the pattern was precise and incomplete: the split has two outputs, and a receipt silent about which token became the *file* reads as complete, which is what stops a reader checking the half that moved ([#1166](https://github.com/Digital-Process-Tools/claude-supertool/issues/1166)).

Use `grep:@-` with a `pattern` key when the split should fall somewhere else.

**`around` prints the same line, and did not until [#1821](https://github.com/Digital-Process-Tools/claude-supertool/issues/1821).** It rejoins a colon-bearing pattern exactly as `grep` does, so `around:mode:.*remind|title:DIR` searches for `mode:.*remind|title` under `DIR` — and where the PATH slot resolves, the only thing the caller saw was a well-formed answer. The escape hatch in the line names the op that ran (`around:@-`, whose keys are `pattern`, `path`, `n`), because a note that sends an `around` caller to `grep:@-` points at a route they did not call. #1821 was filed as an *ordering* defect on `grep` — results above the caveat — and that half does not reproduce: `op_grep` has prepended this line since [#1065](https://github.com/Digital-Process-Tools/claude-supertool/issues/1065), and a test now pins the order rather than leaving it to the next refactor.

**A PATH slot that came out as `.` is refused, not scanned** ([#1417](https://github.com/Digital-Process-Tools/claude-supertool/issues/1417)) — an empty slot and a literal `.` both produce it, and the refusal names the slot rather than guessing which one the caller typed, because `_parse_grep_args` collapses the two and nothing downstream can tell them apart. `grep:_FLAGS|def main:presets/github/issues.py:::40` rejoins to the pattern `_FLAGS|def main:presets/github/issues.py:` with the path `.`, then scans the whole tree and returns hits — a well-formed answer to a question nobody asked. The `|` is incidental; `grep:PATTERN:PATH:` does the same with no alternation in it. So when the path resolves to `.` and a segment of the rejoined pattern names an existing file or directory, grep declines and prints both readings:

```
ERROR: grep would have scanned the whole tree, not the file you named (#1417).
  Read as pattern='_FLAGS|def main:presets/github/issues.py:' + path='.' — …
  If you meant that file:
    grep:_FLAGS|def main:presets/github/issues.py
```

It declines rather than auto-correcting, and that is the difference from `around:.supertool.json:232:12`, which *is* redirected to `around_line` and disclosed. The reading that redirect replaces is a hard failure (`path not found: 232`), so there is nothing to lose. Here the reading it would replace is a successful whole-tree sweep: both readings are live, neither is rankable, and a tool with two candidate answers declines instead of guessing. A segment that fails the cwd containment check is skipped rather than named, so the refusal is not an existence oracle for paths outside the boundary.

The gate is narrow because the rejoin is normally right. Measured over the 165 distinct `grep:` spellings written down in this repo, 39 hit the rejoin and none of them is declined.

**It is `grep` only, and that is a decision rather than an oversight.** `around` and `between` share the error helper but not the defect: neither *defaults* a path to `.` — an empty slot becomes `""` and fails loudly — so a `.` there is always a value the caller typed. And `between:re:START:END:PATH` carries three caller-supplied arguments ahead of the path, so `between:re:START:code.py:.` is an ordinary tree-wide range search whose END happens to name a file. Wiring the same check into those two produced a false refusal on that call, and a printed repair that dropped the `re:` marker selecting the mode. Note which thing is `grep`-only: this **refusal**, not the `pattern read as` **disclosure** above it, which travels to `around` and is right to (#1821). A refusal has to be sure and this one is not portable; a disclosure only has to be true.

**The path that is checked against the cwd boundary is the one the split produced**, not a fixed argument slot. `grep:PATTERN:PATH` and `around:PATTERN:PATH[:N]` both take the path from the *last* token after the trailing numbers are peeled, so a `:` in the pattern moves it; containment runs on that value. A pattern containing an absolute path is therefore searched for, not opened ([#1166](https://github.com/Digital-Process-Tools/claude-supertool/issues/1166)).

**Delegation does not change the dialect.** When rtk is installed and enabled, a plain `grep` (no context, no count) is delegated to it. The system grep behind rtk reads a POSIX **BRE** unless told otherwise, where `|`, `+`, `?`, `(` and `{` are ordinary characters — so `ab+c` matched a literal plus rather than `abbc`, and only when that reading happened to match at all ([#987](https://github.com/Digital-Process-Tools/claude-supertool/issues/987)). Supertool passes `-E`, and delegates only patterns whose two readings are the same by construction: any backslash escape outside the punctuation both dialects agree on (`\. \$ \* \+ \? \( \) \[ \] \{ \} \| \\ \/ \-`) sends the search to the native walker, as do lookaround and inline flags (`(?...`), non-greedy quantifiers, and POSIX bracket classes (`[[:alpha:]]`, `[[=a=]]`, `[[.a.]]`). That is a whitelist rather than a list of known offenders, because the version that enumerated `\d`/`\w`/`\b` let GNU's `\<` and `\>` word boundaries through — they anchor in ERE and are the plain characters `<` and `>` in Python.

## Gitignored directories are skipped

`glob` and `grep` prune directories git ignores, at the walk boundary — the subtree is never opened. Without this, one `glob:**/Foo.php` in a repo with six agent worktrees under a gitignored `.claude/worktrees/` returned seven hits, six of them stale copies of other branches that sorted *first*; and `scanned 118353 files` was largely the same tree counted repeatedly.

**Supertool asks git rather than parsing `.gitignore`.** One `git ls-files --others --ignored --exclude-standard --directory` per search root answers with full ignore semantics — negations (`!keep/`), nested `.gitignore` files, `.git/info/exclude`, your global excludes — and `--directory` collapses an ignored tree to its top directory instead of descending into it. Reimplementing that pattern language would mean hiding files whenever we got a rule wrong, which is the failure direction this op exists to avoid.

**An ignored path you name explicitly is still searched.** The prune is switched off entirely when the search root is itself ignored, so all of these work:

```bash
./supertool 'grep:needle:.claude/worktrees/agent-a29f'
./supertool 'glob:.claude/worktrees/agent-a29f/**/*.php'
```

Only a walk that would have *descended into* an ignored tree is pruned. Deliberately entering one is not.

**Scope, deliberately narrow:** only ignored **directories** are pruned. Ignored *files* elsewhere in the tree are still searched — the win is at the directory boundary, and the secret-file case belongs to [`exclude-paths`](../configuration.md#excluding-paths-from-traversal-ops), which does filter files. And the prune is not a filter on results: it shrinks the walk, so `scanned N` drops with it and stays an honest denominator.

Three ways out, in descending scope:

| Escape hatch | Reach |
|---|---|
| `"gitignore": false` in `.supertool.json` | every op, every call in the project |
| `SUPERTOOL_NO_GITIGNORE=1` | one invocation |
| `:no-exclude` on the op | one call — also drops `.git/`, `node_modules/` and the rest of `exclude-paths` |

Outside a git repo, without `git` on `PATH`, or when the query times out, nothing is pruned — an unanswerable question yields "no opinion", never "skip it".

## Delegated to rtk

When [rtk](https://github.com/wilpel/rtk) is installed and `rtk` is not set to `false` in `.supertool.json`, a plain `grep:PATTERN:PATH` (no `CONTEXT`, no `count`, and no multi-segment exclude prefix such as `src/vendor/libs/`) is handed to `rtk grep` instead of the native walker. Nothing else about the op changes, but the **output shape does**, in two visible ways:

- The body is rtk's own compact `path:lineno:content`, one line per match, **not** grouped under a filename header and not subject to the per-line cap above.
- The report line reads `scanned ? files — delegated to rtk` in place of a number:

```
(3 results in 2 files, scanned ? files — delegated to rtk, limit 10)
./sub/c.txt:1:alpha in sub
./a.txt:1:alpha beta
./a.txt:3:alpha again
```

rtk shells out to the system `grep` and reports no scanned-file count, and re-walking the tree *in Python* to compute one is the traversal delegation exists to avoid — so on a complete result the denominator is stated as unknown rather than quietly omitted. (On a **truncated** one it is computed after all, by a second delegated `grep -rc` rather than a walk — see below.) **A `?` never appears next to a zero result.** rtk exits non-zero when it matches nothing, and an empty result falls through to the native walker, so every zero-result grep — the case the denominator was added for — comes back with a real count and the `— nothing matched the path/glob` marker where it applies. A `?` therefore always sits beside at least one result, which is itself proof that files were searched.

**A truncated delegated grep counts, and it counts exactly** ([#1771](https://github.com/Digital-Process-Tools/claude-supertool/issues/1771)). rtk is asked for `limit + 1` matches and the extra one is trimmed, which settles *whether* the answer is partial. It used to leave *how* partial unanswered — `(total not counted)`, over a `?` denominator — so a sweep whose whole purpose was completeness came back unquantified in both halves at once. That matters more here than in an ordinary grep: a rule shipped in `claude-jit-context` refuses the harness `Grep` tool and names this op as the replacement, so every completeness sweep an agent runs lands on this path.

When the first pass comes back truncated, a **second delegated pass** runs — `grep -rc`, the same engine over the same tree, minus the `-m` cap. `-rc` names every file it scanned, zero-match ones included, so one call answers the numerator and the denominator together:

```
(10 results in 3 files, scanned 1154 files — delegated to rtk, limit 10 — TRUNCATED, 47 matches total)
```

The total is **exact**, never the native walker's `N+ … (count capped at N)`: that spelling exists because a Python walk has to stop counting somewhere, and `-c` counts in C.

**Three things the census refuses to do.**

- **It does not run on a complete result.** No `TRUNCATED`, no second pass — the fast path keeps the single call it has always had, and a complete answer already shows every match there is.
- **It does not count what the report hides.** The default exclude list carries negations, which system grep cannot express, so a `server.pem` really is read by the census pass — and dropped from both the total and the denominator by the same `_is_excluded` the native walker applies. Otherwise the total would count matches the caller was told were not there.
- **It does not guess.** rtk absent, failing or timing out; `-rc` output that is not `path:N`; a total at or below the number of rows printed under it (the tree can change between the two passes); or the switch below turned off — any of those and the report says `— TRUNCATED, more matches exist, total unknown (the delegated count pass did not run)` and prints no number at all. That is the third state in the table above, and it is deliberately not the same sentence as a count that came back small.

**What it costs, and how to decline it.** A second full scan of the tree, bought only on a truncated result. Measured on this repository (1,159 files, dense pattern): 0.47s for the `-m`-capped match pass, 0.65s for the census. Set `builtin-ops.grep.count_truncated` to `0` in `.supertool.json` (or `SUPERTOOL_GREP_COUNT_TRUNCATED=0`) to skip it — which returns you the honest unknown, not the old aside.

**The `?` denominator survives on a complete result, and there it is not load-bearing.** With no `TRUNCATED` marker every match is on the screen, which is the only thing a breadth question needed the denominator for; and a `?` never appears next to a zero result, since those fall through to the native walker.

**Delegation is skipped when git ignores a directory the exclude list would still walk.** rtk shells out to the system `grep`, whose `--exclude-dir` takes bare directory names and cannot express a nested path like `.claude/worktrees/` — a delegated grep would return exactly the copies the native walker prunes, and which backend ran must never change the answer. The test is *residual*: an ignore set already covered by `exclude-paths` (a lone `node_modules/`, say) costs you nothing.

**Excluded files are filtered on the way back, not just on the way out.** The argv is the optimisation; the post-filter is the guarantee. Whatever rtk hands back is re-filtered through the same matcher the native walker uses, so a grep that ignores `--exclude` — or an rtk release that rewrites the argv — still cannot leak ([#691](https://github.com/Digital-Process-Tools/claude-supertool/issues/691)). If the filter drops anything, the search is **redone natively**: the delegated header's count, its `limit + 1` truncation probe and its `?` denominator all describe the unfiltered result set, and a header describing a result set that no longer exists is exactly the kind of quiet lie the `?` was introduced to avoid.

**Credential entries are deliberately withheld from the argv, so that redo fires.** Every single-segment entry sends `--exclude-dir=NAME`, but only **noise** entries (`.git`, `node_modules`, `dist`) also send `--exclude=NAME`. A credential entry sends the directory flag alone, so grep still returns the file, the post-filter drops it, and the redo produces a report that says `N files hidden by exclude-paths` ([#764](https://github.com/Digital-Process-Tools/claude-supertool/issues/764)). Sending `--exclude=.env` instead meant grep never opened the file, the post-filter never saw it, and the delegated report had nothing to disclose — so the disclosure was inverted against usefulness: honest whenever the flags failed, silent whenever they worked, which is the fast path and the common one. The count is the entire justification for hiding a file without asking, so the path that hides most often is the one that can least afford to skip it.

**What that costs.** A second walk, and only when a credential-shaped file actually *matched* your pattern — an ordinary search in a repo that merely contains a `.env` still runs once, because grep opening a non-matching file returns nothing to drop. It was already the behaviour for the wildcard half (`*.pem`, `.env.*`, `id_rsa*`), which system grep cannot be handed at all: it has no way to express a negation like `!.env.example`, so when the effective list carries one, wildcard entries are withheld from the argv and left entirely to the post-filter. Directory flags are never withheld — a pruned directory is never opened, so the native walker has no files to count there either, and dropping `--exclude-dir` would cost the traversal win and buy no disclosure.

Set `"rtk": false` in `.supertool.json` to keep every grep on the native walker and its exact scanned count; `SUPERTOOL_NO_RTK=1` does the same for one invocation.

## Common patterns

Find all usages of a function across a codebase, with 2 lines of context:

```bash
./supertool 'grep:handle_request:src/:20:2'
```

Search for a pattern containing `:` when there is no path to anchor the right-hand end (`grep:A::CONST` would read `CONST` as the path):

```bash
./supertool 'grep:@-' <<'EOF'
pattern = '''A::CONST'''
path = "src/"
EOF
```

The colon CLI handles `grep:PATTERN:PATH:LIMIT` on its own — it peels the trailing ints and takes the last token as the path — so `grep:Element: <:traces.txt:8:0` needs no payload. There is no backslash escape; see [input forms](../input-forms.md).

Count which files reference a symbol most:

```bash
./supertool 'grep:MyClass:src/:50:0:count'
```

Show how a pattern is used everywhere ("show me how everyone uses this"):

```bash
./supertool 'grep_around:def handle:src/:5:15'
```

Jump to a known line and see surrounding context:

```bash
./supertool 'around_line:src/app/Module.py:142:8'
```

Extract a full function body by name:

```bash
./supertool 'between:handle_request:src/app/Module.py'
./supertool 'between:handle:src/app/Module.php'
```

Extract a block by regex anchors (language-agnostic):

```bash
./supertool 'between:re:^def handle_request:^def :src/app/Module.py'
```

## See also

- [reads.md](reads.md) — when you know the path and want raw content
- [map.md](map.md) — when you want all symbols in a file/directory at once (faster than grep for orientation)
- [docs/validators.md](../validators.md) — validators fire on edits triggered after search-guided changes
