---
title: "validators/ — one contract, 35 adapters"
match: "validators/"
---

# The contract (all adapters, one shape)

Three states only: `ok` (bool), a finding (`ok:false` + `errors`), or `skipped`. A checker that
could not run must say so — never report clean. Canonical write-up:
`docs/validators.md` §"Declining instead of guessing". Cite that exact path — it's repo-specific,
mis-cited across a repo boundary before.

**Absent tool → one call, `refusal.absent()`** (`validators/common/refusal.py:227`):

```python
if not spawnable(TOOL):
    emit(absent(TOOL, file, INSTALL_HINT, int((time.time() - start) * 1000)))
    return
```

**Never `shutil.which(TOOL)` for the gate.** `spawnable(TOOL)` (`from spawnable import
spawnable`) excludes a repo-planted cwd match (#2575) -- the same guard the spawn already uses via
`argv0()`/`resolve_bin_cmd()`. A raw `shutil.which()` gate searches a different path list than the
spawn it guards: a cwd shim passes the gate, then fails to spawn (#2579).
`tests/test_gate_matches_spawn_2579.py` keeps this closed.

`tool` is the **validator name** — the key a repo writes in `.supertool.json` — never the binary. `tsc-check` runs `tsc` and `html-check` runs `node`; escalating on the binary name would ignore the only spelling anyone can configure. Reserve it for an *absent* tool; a tool that ran and fell over is a different arm.

Where the absence lands — `skipped`, or a loud failure under `$SUPERTOOL_REQUIRE_VALIDATORS` — is decided inside `absent()`, not by the adapter. That is the point of #1202: `required()` was a helper each adapter had to remember to call, **six of them did**, and the other ten spelled the moment as a fabricated `{"ok": true, "count": 0, "errors": []}` about a file nothing opened.

**This block used to say "copy `shellcheck.py:131-137` verbatim, do not invent a variant".** That code is still there and still correct — shellcheck is one of the six that always gated — but copying it now reproduces the pre-#1202 shape, one adapter's memory at a time, which is the thing that broke. Grepping an adapter for `required(` will come back empty and mean nothing: the call is indirect through `absent()`. One agent read that emptiness as "html-check is still inert" on 2026-08-09 and was wrong.

# #1202 is CLOSED — do not go looking for this gap

**#1202 shipped in #1213** ("An absent tool is never a clean pass — 16 adapters, and ten were
fabricating `ok:true`"). Every adapter now routes its absent-tool arm through `refusal.absent()`,
which consults `required()` internally. Verified 2026-08-09 against master.

This section used to enumerate 13 adapters with the gap, and was still being injected at every
call touching `validators/` after all 13 were fixed.

# `skipped` and rollback

A `skipped` never rolls back (`docs/validators.md:724`, "No verdict never rolls back an edit") —
so turning `ok:true` into `skipped` changes rollback reachability. Don't do it casually.
(Read `:681` until #1042. Kept as line+quote rather than a heading because it is *not* a heading —
it is a bolded lead-in — and `claims:PATH` verifies a quote against its line, so this citation is
checked on every run rather than remembered.)

# Schema fields — `validators/SCHEMA.md`

Cited by **heading**, not by line. Both of these were line numbers until #1042, and #1042 —
which inserted a table near the top of that file — moved `:37` to 51 and `:79-84` to 93-96
without touching a word either entry describes. A line citation into a live document decays on
every edit above it, silently, in a file that is injected verbatim on every call touching
`validators/`.

- §"Skipped: the third state" — a skip **omits** `ok`, `count`, `errors` entirely. Only `tool`,
  `file`, `duration_ms`, `skipped` (reason string) survive. `ok:true` on a skip is exactly the
  misread the third state exists to prevent.
- §"A located diagnostic still has to be about *this* file (#754)" — cargo-check precedent for a
  mixed payload: a diagnostic about *another* file in the same crate keeps `ok:false` + the real
  error, but `line/col:null`, `code:"adapter"`, `source_context` absent. Don't fold a whole-crate
  error into `skipped` — that drops `errors` entirely, which is worse.
- §"Core-only fields" — the four keys the core stamps and strips from every adapter payload
  (`no_verdict`, `timeout`, `elapsed_s`, `resolved_to`). Compared against the core's own set in
  both directions by `tests/test_schema_contract_drift_1042.py`; add to one and the guard makes
  you add to the other.

# #1203 is CLOSED

`tomllint` was declared in `.supertool.example.json` and absent from this repo's own
`.supertool.json`. Fixed and verified CLOSED 2026-08-09.

**Both stale entries above shared a heading that begins `Open defect`**, which is what made them
mechanically findable: `claims:PATH` checks that every issue cited under such a heading is actually
OPEN. It needs no semantics, because the heading is the author saying "this is a live gap". That
lens is 5-for-5 across `.claude/jit-context/`, where a lexical rule over the same corpus scored 13%.
So: if you write a heading like that, the tracker is the thing that keeps you honest — and if you
are tempted to hand-maintain a defect list in an auto-injected file, read the two corpses above
first.

# changelog-fragment

Fires on any mutating op under `changelog.d/` and republishes the assembler's own messages. No
`markdown-it-py` parser available → `skipped`, not `ok` (`validators/changelog-fragment/changelog-fragment.py:24,173`).

# Run one by hand

```
./supertool 'validate:PATH[:tool1,tool2][:verbose]'
```

`verbose` = uncapped errors + source context + raw stdout/stderr. Colon-in-filename → use
`validate:@payload.toml` or `validate:@-` (fields: `path`|`paths`, `tools`, `verbose`).

# `bin_resolve.describe_unresolved()` can leak an internal sentinel path (#2578)

`resolve_bin_cmd()` -> `_spawnable()` -> `_refuse_spawn()` returns a synthetic sentinel path
(`/supertool-<issue>-refused-<uuid>/<name>`) when a `_BIN` env var names a bare tool that only
resolves via a cwd-only match (refused for security, #2575/#2578). `describe_unresolved(raw,
resolved)` sees `resolved != raw` and appends `(configured: "<raw>")`, so the message becomes
`GLAB_BIN not found: /supertool-2578-refused-<uuid>/glab (configured: "glab")` -- an internal
sentinel the operator never configured, with no indication the real story is "a cwd-only match
existed and was refused for security" (that lives only in a code comment). Narrow but real:
needs a `_BIN` env var set to a bare name with no genuine PATH entry and a cwd-only match (a
same-named file at the repo root). Open, unfixed: `describe_unresolved()` needs to special-case
the refused-spawn prefix, or `_cwd_only_match`/`_refuse_spawn` need to return something it can
detect structurally instead of an opaque string.

# `_refuse_spawn()`'s `FileNotFoundError`-only guarantee doesn't hold for reserved Windows names (#2578)

`validators/common/spawnable.py`'s `_refuse_spawn(name)` docstring guarantees the returned path
raises `FileNotFoundError` specifically, on either platform -- verified for an ordinary `name` on
POSIX, but `_cwd_only_match()`'s only guard is `os.path.dirname(name)`; it does not exclude a
Windows reserved device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-9, `LPT1`-9, case-insensitive) or
a character invalid in a Windows path component (`< > : " | ? *`). Building the sentinel path
with such a `name` could raise a different `OSError` subtype on Windows (reserved-device open,
invalid-path-syntax) that escapes a caller's narrow `except FileNotFoundError`, uncaught. Low
reachability -- every call site in this repo passes a hardcoded literal or an operator-supplied
`_BIN`, not attacker-controlled input -- and not observed on real Windows, reasoned from
documented semantics. No test exercises a reserved-name or invalid-character `name` on any leg.

# `_spawned_heads` register (`tests/test_spawnable_register_2540.py`) is blind to a `Starred`/`Name` argv head (#2540)

The register's `_spawned_heads()` records `ast.unparse(node.elts[0])` for every list literal, so
it never matches a `which()`-probed argument string when the argv head is a list-literal
unpacking (`[*bin_cmd, "--write"]`, unparsing to `*bin_cmd`) or a bare `Name`. This exact shape
lives at `formatters/prettier-write/prettier-write.py:89` -- invisible to the register, correct
today only because `resolve_bin_cmd` was independently fixed in the same delta, not because the
register saw it. Open: extend `_spawned_heads` to resolve a `Starred`/`Name` head against the
variable's own assignment, or document the blind spot so a reviewer doesn't trust the register
past where it reaches. Related drift, not itself this finding: `bin_resolve._spawnable` and
`spawnable.argv0` are now two copies of the same ~8 lines and the same 3.12 `shutil.which`
docstring, a surface this register also does not watch.
