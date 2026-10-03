# Validator Output Schema

Universal JSON. Every adapter emits this shape. Validator core never parses tool-specific output.

## Schema

```json
{
  "tool": "phplint",
  "file": "src/Foo.php",
  "ok": true,
  "count": 0,
  "errors": [],
  "duration_ms": 42
}
```

## Fields

| Field         | Type             | Required | Notes                                                                 |
|---------------|------------------|----------|-----------------------------------------------------------------------|
| `tool`        | string           | yes      | Adapter id (`phplint`, `phpstan`, `phpunit`, `prettier`, ...)         |
| `file`        | string           | yes      | Path validated. Repo-relative.                                        |
| `ok`          | bool             | yes*     | Pass/fail. Validator rolls back on `false` if op marked rollback. *Absent on a `skipped` result. |
| `count`       | int              | yes*     | Issue count. Used for before/after diff arithmetic. *Absent on a `skipped` result. |
| `errors`      | array of objects | yes*     | `[]` when ok. Each: `{line, col, severity, code, msg}`. *Absent on a `skipped` result. |
| `duration_ms` | int              | yes      | Wall time, **measured** — never the budget written back. On a decline it is evidence: `tests/_adapter_verdict.stalled_at_its_own_wall` reads it as a floor to tell a real wall from broken error routing, and a literal satisfies that floor whatever happened (#1604, #1683). |
| `metrics`     | object           | no       | Tool-specific counters (`tests_total`, `tests_passed`, etc.). Numeric values. Used by renderer for before/after diff on metric keys even when `count` is unchanged. |
| `diff`        | string           | no       | Unified diff produced by the tool (e.g. rector). Rendered as a fenced block below all errors in verbose mode. Ignored in default mode. |
| `skipped`     | string           | no       | Reason the validator declined to analyse the file (scope allowlist, tool absent, ...). Its presence — not its value — marks the result as a third state. |
| `count_basis` | string           | no       | `total` (`count` counts every row `errors` carries, `adapter` stall rows included) or `measured` (`count` already excludes them). Declared with `errors_truncated` or not at all. See "Declaring how `count` relates to `errors`" below. |
| `errors_truncated` | bool        | no       | `true` when findings were **dropped** from `errors` — not merely that a cap exists. Declared with `count_basis` or not at all. |

A blank line between two rows ends a Markdown table, and every row after it renders as a paragraph of literal pipe characters. One sat after `metrics` from #411 until #1042, which left `diff` and `skipped` — the third state — outside the rendered field table. `tests/test_schema_contract_drift_1042.py::test_no_field_row_falls_outside_its_table` now fails on it.

### Core-only fields

The keys the **core** stamps on a result. An adapter may never send one: `_validator_strip_core_keys` drops each from every adapter payload — fresh or replayed from cache — before any decision reads it, so setting one is neither refused nor honoured, it is simply not there.

| Field         | Type             | Set by   | Notes                                                                 |
|---------------|------------------|----------|-----------------------------------------------------------------------|
| `no_verdict`  | bool             | core     | The core watched this adapter break down — no output, non-JSON, a reply carrying neither `ok` nor `skipped`, or (#2185) its `resolve` command could not look (git absent, timed out, not a repo, unspawnable, or its own crash receipt). A crash of the adapter itself is not one of these any more: `refusal.guard_main` publishes it as an `adapter` error (#1697). Distinct from a healthy adapter declining on its own terms; under `$SUPERTOOL_REQUIRE_VALIDATORS` the first exits 1 and the second does not (#975). |
| `timeout`     | bool             | core     | The subprocess exceeded its budget. Whether an adapter answered inside its budget is observed by the process holding the budget, never reported by the process being timed. |
| `elapsed_s`   | float            | core     | Wall time as the core measured it. Re-stamped on a cache hit from the run in hand — the elapsed time of a lookup is the lookup. |
| `resolved_to` | string           | core     | The target `resolve` mapped the edited file to. Re-stamped on a cache hit for the same reason. |

This table is the doc's half of a contract whose other half is `_VALIDATOR_CORE_ONLY_KEYS` in the core. Neither is generated from the other, so `tests/test_schema_contract_drift_1042.py` compares them in both directions and fails if either side names a field the other does not (#1042). Do not add a row here without adding the key there.

**A field in the adapter table above is not thereby a field a core decision may read (#1277).** Emitting and consulting are two permissions, and only the second is a containment property: a key that one of the core's own decisions — no-verdict, regressed, baseline, gate-did-not-run, not-checked — consults is a key an adapter can write into to decide something the adapter is not entitled to decide. That list is `DECISION_READABLE_KEYS` in `tests/test_adapter_cannot_forge_core_keys_1036.py`, it is held apart from this document on purpose, and it is deliberately narrower: five of the nine documented adapter fields are on it, and `diff`, `duration_ms`, `file` and `metrics` are withheld. Adding a row here therefore reddens that test rather than silently widening the exemption, which is what it did while the check used this table directly.

### Skipped: the third state

`ok` alone has two values and the world has three: clean, broken, and **never looked at**. A validator that refused to run has produced no information about the file, so folding that into either `ok` value is a lie in one direction or the other.

An adapter reports it by emitting `"skipped": "<reason>"` and **omitting `ok`, `count` and `errors` entirely** (#515). A receipt carrying `ok: true` reads as a pass to anything keying off `ok`, which is the mistake the third state exists to end; leaving the verdict keys out makes a skip structurally impossible to misread as one. `tool`, `file` and `duration_ms` stay — they describe the attempt, not a verdict.

Consumers must branch on the presence of `skipped` before reading any verdict key. Every core consumer already does, and has to: the reason string exists only on a skip. Use `validators/common/refusal.py:skipped()` rather than building the dict by hand — and for the specific case of the adapter's **tool being absent**, use `absent(tool, file, reason, dur_ms)` instead, which is `skipped()` plus the `$SUPERTOOL_REQUIRE_VALIDATORS` escalation. Spelling that decision out per adapter is how five of thirty-four ended up with an escalation that worked and ten ended up answering an absent tool with `ok: true` (#1202). `tool` there is the validator name a repo writes in `.supertool.json`, not the binary. The core then guarantees:

- the row renders as `skipped (<reason>)`, never `0 → 1 (+1) ✗`;
- the result is excluded from the before/after delta;
- the result is **not cached** (a skip is config-derived; the cache key is a content hash);
- the result **never triggers rollback**, whatever `rollback_on_fail` says.

### `adapter`: the reserved code for "no verdict was obtained"

`code: "adapter"` is reserved across every adapter for a failure of the adapter or its tool rather than a finding about the file: a binary that is absent, a timeout, output that would not parse, a tool that exited non-zero without saying anything about the file. It stays a real error — `ok: false`, `count: 1` — because the process ran and something is broken that someone has to fix; a fault routed to `skipped` is a validator quietly reporting clean.

Two consequences a consumer can rely on:

- **The message names what failed**, including the exit code and the tool's raw output when there is any, and says so explicitly when there is none.
- **The result is never cached.** `adapter` is in the core's `_NONDETERMINISTIC_ERROR_CODES`: a verdict that was never obtained is not a function of the file's content, and the cache key is a content hash, so caching one replays it until the file changes.
- **The result never triggers rollback, whatever `rollback_on_fail` says** — the same guarantee `skipped` carries above. `count: 1` is the channel this schema gives an absence, not a measurement of the file, so the core never subtracts it from a baseline in either direction and never reverts an edit over it (#969). **The guarantee is per row, not per payload (#1717).** An `adapter` row beside real findings is still exempt from the arithmetic — `_validator_measured_count` drops it from `count` on both sides — while the payload as a whole is still rendered as a measurement, because the findings measured the file. Until #1717 the two were one test and a mixed payload got neither answer: it rendered as a verdict, which is right, and its stall row was subtracted as a finding, which reverted correct edits. The core's own timeout (`code: "orchestrator"`) is treated identically, by a different route: it arrives as a whole fabricated payload carrying the core-only `timeout` field, which is caught before any arithmetic runs. It is deliberately *not* exempted per row — an `orchestrator` row sitting beside a finding can only be an adapter writing the core's provenance code, which is the boundary "Core-only fields" closes at the other door.

Emit it whenever the tool's output does not confirm it looked at the file. Where the boundary is genuinely unclear, prefer the finding: an `adapter` result is fully legible to a reader, while a real finding relabelled `adapter` sends them to the wrong place. See `docs/validators.md`, "Declining instead of guessing".

**A code that is not `adapter` gets none of the three guarantees above, and nothing anywhere says so.** The payload is well-formed, no core path warns, and the only symptom is a fault presenting as a finding — which is why pyright spelling its timeout `code: "timeout"` survived until #1464, cached and able to roll an edit back. The timeout arm is the one case that can be checked mechanically, so it is: `tests/test_pyright_timeout_is_an_adapter_fault_1464.py` parses every adapter's `except subprocess.TimeoutExpired` handler and refuses any literal error code other than `adapter`. The other three cases in the sentence above are still enforced only by reading.

**`no_verdict` is core-internal — an adapter must never set it.** The core adds it to the `skipped` result *it* synthesises when an adapter produced nothing it could read (no output, non-JSON, a reply with neither `ok` nor `skipped`). A crash used to be on that list; since #1697 every adapter wraps its `main` in `refusal.guard_main`, so an escaping exception is self-reported as an `adapter` error naming the class rather than observed as a silence. It is the one signal separating "the core watched this adapter break down" from "a healthy adapter declined on its own terms", and under `$SUPERTOOL_REQUIRE_VALIDATORS` the first exits 1 while the second does not (#975). An adapter that sets it would be asking for its own scope decisions to be read as a broken gate. An adapter that is genuinely unable to run says so through `refusal.required()` and the `adapter` code above.

**The prohibition is on the whole class, not on that one key (#1036).** `no_verdict` was the only core-internal field named here, and the sentence was read as covering the boundary rather than one instance of it — so `timeout`, which the core stamps on the result it fabricates when a subprocess exceeds its budget and which `rollback_on_fail` reads as "nothing checked this file", stayed unmentioned and unenforced. An adapter that printed `"timeout": true` beside a real finding therefore turned off the rollback guard: the row said `NOT CHECKED`, the edit that broke the file survived, and the adapter's own sentence was printed under the `orchestrator` provenance column, which exists to mark text the core wrote.

The rule: **an adapter's fields are the ones in the table above, and nothing else.** The core's timeout and an adapter's claim of one are different facts, and only the first is evidence — whether an adapter answered inside its budget is observed by the process holding the budget, never reported by the process being timed. The core now drops every field in the "Core-only fields" table above from each adapter payload before any decision reads it, so setting one is neither refused nor honoured; it is simply not there. That table is the enumeration — re-listing the keys in this sentence made a second hand-maintained copy of the same set, which is #1042. Refusing the result outright would have handed the same adapter the same bypass through the other door, since a refused result is a skip and a skip never rolls back either. An adapter's own verdict — `ok`, `count`, `errors` — is untouched.

**The cache is the same payload, so it is the same boundary (#1044).** A cached result is an adapter payload that outlived the run which parsed it, and the core reads one back through a different `return` than the fresh path. Entries written before the drop existed still carry the forged key — they are signed with this machine's own secret so they verify, and (until #1048) no part of the key described the build that wrote them, so an upgrade did not retire them. Both paths drop the core's keys now, and the key carries a meaning version derived from this file's content and the core-only key set, so a later change to what a stored field *means* misses rather than being read under the new rules. `elapsed_s` and `resolved_to` are then re-stamped from the run in hand rather than replayed: the elapsed time of a cache hit is the lookup, and the resolved target is the one this call resolved.

**What confirms it looked at the file is per-tool, and it is a located diagnostic, not the exit code** (#753). An adapter picks the marker out of its own tool's output format — `file:LINE:` (xmllint, ruby), `: line N:` (bash), a resolved-path header (node), `path:line:col:` (gofmt, cargo short format), `on <file> line N` (terraform). Two rules generalise from that sweep:

- **Never take a location from the output at large.** Search anchored to the diagnostic, and where the tool prints the path it resolved, check it against the file. `node --check` reports a missing module by printing `node:internal/modules/cjs/loader:1386` first, and a bare `:(\d+)` search turned that into the file's syntax-error line.
- **A finding you cannot place reports `line: null`.** Reclassifying and inventing a location are separate fixes; keeping a finding does not license borrowing a number for it.

Where a tool documents distinct exit codes, read them — `terraform fmt -check` returns `3` for "needs formatting" and `2` for "I failed" — but the located diagnostic is still the marker, because an exit code alone cannot say *which* file it is about.

### A located diagnostic still has to be about *this* file (#754)

Some analysers do not work per file. `cargo check` compiles the whole crate, and a per-file adapter is handed one path out of it, so "the output carries a located diagnostic" and "the output says something about the file you edited" are two different facts. `cargo-check` used to conflate them: an error in `src/sibling.rs` was published with `file` naming `src/main.rs`, `line` set to a line number belonging to the other file, and `source_context` read from a crate-relative path resolved against the adapter's own working directory — three false statements about a file that compiled fine.

**A crate error caused by another file is real, and the fix is not to hide it.** Filtering it out leaves a healthy file in a crate that does not build reporting clean, or falling into the `adapter` branch above with a message that names nothing. Either trades a misreport for a silent loss, which is the worse of the two: the crate genuinely does not compile and a caller told nothing cannot act on it. So the diagnostic is kept and only the *attribution* changes:

| The diagnostic names | `line` / `col` | `code`      | `source_context` | `ok` |
|----------------------|----------------|-------------|------------------|------|
| the file under validation | as reported | the tool's (`E0308`) | rendered from the target | `false` |
| any other file       | `null`         | `adapter`   | absent           | `false` |

`adapter` rather than a new code, because every guarantee it already carries is the one wanted here: the message names what happened, the result is never cached — a whole-project verdict is not a function of this file's content, and the cache key is a content hash — and it never triggers rollback, so `rollback_on_fail` cannot revert a good edit over a defect in a file the edit did not touch. `skipped` would be wrong for the same reason it is wrong for a tool fault: it omits `errors` entirely, so the crate error would vanish.

**Fold case before normalising the separator, never after, and compare against the raw target as well as the resolved one.** `os.path.normcase` is the only stdlib call that knows whether a platform is case-insensitive, and on Windows it does a second thing its name does not advertise: it rewrites every `/` into `\\`. Normalising separators first and folding afterwards therefore un-normalises them, and a suffix rule still looking for a `/` boundary matches nothing at all — on Windows every diagnostic, including the file's own, was demoted to a non-verdict. Make the fold injectable so the behaviour can be asserted from any platform (`refusal.daemon_transport_reason`'s `has_uds` is the same pattern); a platform behaviour pinned only on that platform is pinned only where it was already going to be noticed. And do not let the answer rest on two `resolve()` calls agreeing character for character: `os.path.abspath` joins onto the working directory while `Path.resolve()` goes through `_getfinalpathname`, returning the canonical on-disk spelling of whatever prefix exists and following `subst` and symlinked drives on the way.

**Compare two absolute paths for equality. Anchor a relative one to the base the tool actually used — never to the base you have to hand.** This paragraph prescribed a suffix match on segment boundaries until #669, and by then `cargo-check` had already stopped doing that, twice over, because a suffix match cannot be made correct: it cannot tell a short path that is a *tail* of the target from a different file higher up the tree, so `/abs/elsewhere/src/lib.rs` "was" `src/lib.rs` and a foreign file's error was charged to the file under validation on a `rollback_on_fail` validator (#1037). A two-segment floor was put under it and did not survive either — a package at the workspace root prints exactly `src/lib.rs`, which every member's absolute path also ends with (#1045).

What is right depends on which base the tool prints relative to, and there are two cases:

- **The adapter chose the working directory**, so it knows the base with nothing to infer. `go-vet` runs `go vet .` in the package directory for exactly this reason, and `validators/common/pkg_paths.py` is the whole comparison: fold, normalise, join, compare. `tsc-check` is the same case arrived at from the other end (#1509, fixed in #1519): it sets no working directory, and `tsc` prints relative to the one it inherits — measured, including that an absolute argv does not make the output absolute — so the base is `os.getcwd()` and there is still nothing to ask.
- **The tool has a base of its own** — cargo prints relative to the *workspace* root, which is not the nearest `Cargo.toml` above the file. Ask the tool (`cargo metadata --no-deps`) rather than guessing, and when it cannot answer, the attribution is `"unknown"`, which is a third answer and not a coin flip between the other two.

The naive join this paragraph used to warn against is still wrong and for the reason it gave: joining cargo's `member/src/sib.rs` onto the *crate* root double-counts the member directory and demotes every real finding. The fix is the right base, not the absence of one.

**Render `source_context` from the target the adapter was handed, never from a path rebuilt out of the tool's output.** Once the diagnostic is known to be about this file, its path adds nothing and its resolution can only go wrong.

### Declaring how `count` relates to `errors` (#1728)

`count` and `errors` have independent sources. Twenty adapters write `count = len(errors)`; `phpstan` takes `count` from `totals.file_errors` while building `errors` from a different key of the same document. `_validator_measured_count` therefore cannot read one off the other, and its `max(count - absences, len(rows) - absences, 0)` is a heuristic that serves two conventions at once — an adapter whose `count` already excludes its stall rows (the floor stops it being subtracted from twice) and one that caps `errors` (the subtraction stops fifty findings reading as five).

**Neither term survives a payload whose `count` is bounded by the same cap that bounds `errors`.** Both saturate, before and after compare equal, `_validator_regressed` returns `false`, and a validator with `rollback_on_fail: true` does not revert over a genuinely new finding:

```
before  count=4  errors=[f1, f2, f3, f4, stall]  -> measured 4
after   count=4  errors=[f5, f2, f3, f4, stall]  -> measured 4
```

No arithmetic can separate those two, because **a cap is invisible in a single payload unless the adapter says so**. So the fix is a contract rather than a formula:

- **An adapter may pre-subtract its `adapter` rows from `count`, or it may cap `errors`. It may not do both.** `count_basis: "measured"` beside `errors_truncated: true` is the forbidden pair and is refused.
- **`count` is always the whole total.** A `count` truncated by the same cap as `errors` is the defect above, and it is checkable: under `errors_truncated: true`, `count` must exceed the rows printed.
- **`count` may never fall below the rows printed under it.** Correcting a count is in scope; contradicting the list is not. Undeclared, the floor repairs this silently; declared, it is reported.
- **Both keys together or neither.** Half a declaration leaves the question the pair exists to force unanswered while looking answered.

A payload that breaks any of these is published as a fault **against the adapter** — one `code: "adapter"` row carrying the numbers, which makes the result a non-verdict: rendered `NOT CHECKED`, kept out of the delta, never a rollback, and still exiting non-zero. Never `skipped`, which would be quieter than the defect it reports.

**Declaring is optional at runtime and mandatory for a shipped adapter.** Any repo may name its own validator in `.supertool.json`, so a runtime mandate would break every third-party adapter on upgrade over a shape none of them has been shown to have; an undeclared payload keeps the heuristic exactly as it was. The mandate over this tree is `tests/test_count_basis_contract_1728.py::_GRANDFATHERED`, a set of the adapters that do not yet declare (`validators/*` and `formatters/*` both, since #2159), which may only shrink — the pattern `_UNDECLARED_PATH_OPS` uses. `cargo-check` (`total`) and `phpstan` (`measured`) declare, because they are the two the core cites as the divergent conventions and therefore the two most likely to be copied.

**Declaring changes no number for a conforming payload** — for a complete list the floor already produces the declared answer, and for a truncated one `count` already dominates it. The declaration buys the guard, not the arithmetic.

**What one payload cannot check, stated rather than implied.** An adapter that caps `errors` and declares `measured` anyway is indistinguishable here from a well-formed `measured` payload: saturation makes `count` equal the visible findings by construction. That residue is why this is a statement an author makes and not a property the core infers.

### Error object

| Field            | Type             | Required | Notes                                              |
|------------------|------------------|----------|----------------------------------------------------|
| `line`           | int \| null      | yes      | 1-indexed. `null` if tool gives no location.       |
| `col`            | int \| null      | yes      | 1-indexed. `null` if not provided.                 |
| `severity`       | string           | yes      | `error` \| `warning` \| `info`                     |
| `code`           | string \| null   | yes      | Rule id (`missingType`, `PSR12.Files...`). Nullable. |
| `msg`            | string           | yes      | Human message. Single line preferred.              |
| `source_context` | array of strings | no       | Source lines near the error. The line containing the error uses `→` as separator; surrounding lines use `:`. Example: `["40:     return foo;", "41: ", "42→     bar();", "43: }", "44: "]`. Rendered indented under the error in verbose mode. Ignored in default mode. |
| `context_unavailable` | string        | no       | Why there are no source lines, when the reason is that the file could not be read. Present only alongside an empty `source_context`, never alongside lines. Rendered in verbose mode as `[no source context: <reason>]`. |

### An empty `source_context` and an unreadable file are two different facts (#1446)

`[]` used to mean both — a located finding whose window falls outside the file, and an `OSError` on the way to opening it — and the receipt printed them identically. The house defect: an absence produced by the tool, read as an absence in the world.

**The finding survives.** The tool said something is wrong at that line, and that claim does not depend on our ability to reprint the line. Only the illustration is missing, so only the illustration is qualified — `ok` stays false, `errors` stays intact, `line` keeps its number, and a second key says why the lines are absent. Routing this to `skipped` would drop `errors` entirely and lose a true diagnostic to a failed `open()`; that is the loud bug traded for the quiet one.

| What happened                        | `source_context` | `context_unavailable` |
|--------------------------------------|------------------|------------------------|
| lines read                           | the lines        | absent                 |
| file read, no line in range          | `[]`             | absent                 |
| file could not be read               | `[]`             | the reason             |
| no `line` on the finding             | absent           | absent                 |
| the diagnostic is about another file | absent           | absent                 |

The last row is §"A located diagnostic still has to be about *this* file (#754)" above, and it is deliberately **not** this key: nothing was attempted, so nothing failed.

## Split a tool's output on LF, CR and CRLF — never `str.splitlines()` (#1486)

`str.splitlines()` also breaks on U+2028, U+2029, U+0085, VT and FF. No analyser adapted here frames its output that way, and most of them echo the source text they are complaining about verbatim into the diagnostic. So one of those five characters inside a string literal in the file under validation ends the adapter's idea of a line mid-diagnostic, the fragment re-matches the adapter's own regex, and it is published as a **second finding**. Measured on `go-vet`: `go vet` emitted one diagnostic, the receipt said `count: 2`.

That is arithmetic, not cosmetics. `count` is the number `_validator_regressed` subtracts, so a file that mints itself an extra record partly chooses its own baseline — and on a `rollback_on_fail` validator that baseline reverts edits.

Use `split_lines()` from `validators/common/linebreaks.py` for anything **parsed** as a line-oriented protocol. `str.splitlines()` stays correct where a message is being **flattened** for display: splitting on every separator is what neutralises them, and `terraform-check.plain()` and `pyright`'s message join both want that. `tests/test_validators_splitlines_1486.py` refuses any new call site that is neither.

Adapters do not spell any of this. `validators/common/source_context.py` returns the fields and the adapter spreads them — `{..., **context_fields(target, line)}` or `err.update(context_fields(target, line))`. There is no `source_context()` function to call: it returned a list with nowhere to put the reason, and two adapters had already grown private copies of it.

## Contract

- Adapter **always exits 0** if it produced JSON (even on tool failure). Core reads `.ok`.
- Adapter exits non-zero only on infrastructure failure (tool missing, file unreadable). Core treats as `{ok: false, errors: [{msg: "adapter failed"}]}`.
- Adapter writes **one JSON object on stdout**. Nothing else. Logs go to stderr.
- Input: one arg = file path.

## Spec fields (`.supertool.json` validators/formatters entries)

| Field             | Type             | Required | Notes                                                                                     |
|-------------------|------------------|----------|-------------------------------------------------------------------------------------------|
| `cmd`             | string           | yes      | Shell command. `{file}` and `{supertool_dir}` are substituted before execution.           |
| `hooks_into`      | array of strings | yes      | Ops that trigger this validator automatically (`edit`, `paste`, `vim`, ...).              |
| `match`           | string           | no       | Glob pattern to filter by filename (e.g. `*.php`). Matches all files when absent.        |
| `exclude`         | string or list   | no       | Glob (or list of globs) to skip even when `match` matches (e.g. `*tests/*`). Skip if any matches. |
| `timeout`         | int              | no       | Seconds before the subprocess is killed. Default 60 (validators), 30 (formatters).       |
| `rollback_on_fail`| bool             | no       | Revert the file if the validator reports a regression. Default false.                     |
| `opt_in`          | bool             | no       | When true, validator only runs when explicitly requested (not on every hook).             |
| `resolve`         | string           | no       | Shell command that maps the edited file to the file the adapter should receive.           |
| `env`             | object           | no       | Extra environment variables merged into the subprocess env (`os.environ \| spec.env`). Values are coerced to strings. Example: `{"PHPSTAN_LEVEL": "8", "PHPSTAN_CONFIG": "phpstan.neon"}`. |
| `warm_unsafe`     | string or list   | no       | Regex (or list of regexes) matched against the **resolved** target's content. A hit makes the validator return `skipped` instead of running the adapter — for targets whose verdict this validator's warm process cannot be trusted to produce (see `docs/validators.md`, "Declining instead of guessing"). Never a ✗, never a rollback, never cached. Unreadable targets and patterns that do not compile are ignored, so a config typo cannot silently mute a validator. Example: `["extends\\\\s+SiControllerTestCase"]`. |
| `engine_glitches` | list of strings  | no       | Adapter-specific (rector-mcp). Case-sensitive substrings identifying non-deterministic warm-daemon engine glitches (PHP fatals / engine-state corruption — NOT findings about the file) that the adapter drops at the source so they never surface as a red or reach the cache. The adapter reads this prop straight from `.supertool.json`; built-in defaults apply when absent. Example: `["System error:", "toMutatingScope() on null"]`. |
| `tier`            | string           | no       | `"fast"` (default) or `"slow"`. `fast` runs inline per-op with per-op rollback semantics. `slow` defers to end-of-call, deduped by `(validator, path)` — runs once per unique pair regardless of how many ops touched the file. Failures reported under `[validators-deferred]` header; no auto-rollback. Use `slow` for heavy validators (phpstan, phpunit, rector) to avoid re-running on every edit in a multi-op batch. |

## Adding a validator

1. Write `validators/<tool>/<tool>.sh` (or `.py`)
2. Register in `.supertool.json` under `validators` block
3. Done. Core picks it up automatically.
