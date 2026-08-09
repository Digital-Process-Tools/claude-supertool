---
title: "validators/ — one contract, 35 adapters"
match: "validators/"
mode: once, remind
---

# The contract (all adapters, one shape)

Three states only: `ok` (bool), a finding (`ok:false` + `errors`), or `skipped`. A checker that
could not run must say so — never report clean. Canonical write-up:
`docs/validators.md` §"Declining instead of guessing". Cite that exact path — it's repo-specific,
mis-cited across a repo boundary before.

**Absent tool → copy `validators/shellcheck/shellcheck.py:131-137` verbatim:**
```python
if not shutil.which(TOOL):
    if required(TOOL):
        _adapter_error(file, required_but_absent(TOOL, INSTALL_HINT), dur)
    else:
        emit(skipped(TOOL, file, INSTALL_HINT, dur))
    return
```
Do not invent a variant.

# Open defect #1202 — `required()` gate is inert on most adapters

`validators/ruff/ruff.py:120-121` emits `skipped` on absent tool unconditionally — never calls
`refusal.required()`. `SUPERTOOL_REQUIRE_VALIDATORS=ruff` is silently inert: it can never force a
loud failure for ruff.

Grepped every adapter for `required(`: only **shellcheck, eslint, gitleaks, tomllint,
changelog-fragment** gate on it. 16 adapters check tool presence at all; the other **13 have the
same gap as ruff**: `tsc-check, phpstan, markdownlint, ruby-check, prettier-check, git-status,
cargo-check, hadolint, gofmt-check, terraform-check, pyright, psr, html-check`. Verified by name —
they check `shutil.which`/binary-exists and go straight to `emit(skipped(...))`, no `required()`
call anywhere in the file.

# `skipped` and rollback

A `skipped` never rolls back (`docs/validators.md:650`, "No verdict never rolls back an edit") —
so turning `ok:true` into `skipped` changes rollback reachability. Don't do it casually.

# Schema fields — `validators/SCHEMA.md`

- `:37` — a skip **omits** `ok`, `count`, `errors` entirely. Only `tool`, `file`, `duration_ms`,
  `skipped` (reason string) survive. `ok:true` on a skip is exactly the misread the third state
  exists to prevent.
- `:79-84` — cargo-check precedent for a mixed payload: a diagnostic about *another* file in the
  same crate keeps `ok:false` + the real error, but `line/col:null`, `code:"adapter"`,
  `source_context` absent. Don't fold a whole-crate error into `skipped` — that drops `errors`
  entirely, which is worse.

# Open defect #1203

`tomllint` is declared in `.supertool.example.json:666-667` and **absent** from this repo's own
`.supertool.json` — a validator that ships and isn't registered here checks nothing here.

# changelog-fragment

Fires on any mutating op under `changelog.d/` and republishes the assembler's own messages. No
`markdown-it-py` parser available → `skipped`, not `ok` (`validators/changelog-fragment/changelog-fragment.py:24,173`).

# Run one by hand

```
./supertool 'validate:PATH[:tool1,tool2][:verbose]'
```
`verbose` = uncapped errors + source context + raw stdout/stderr. Colon-in-filename → use
`validate:@payload.toml` or `validate:@-` (fields: `path`|`paths`, `tools`, `verbose`).
