# `vim` preset

Documentation for the built-in `vim` op — the default edit op for any pattern-based edit.

Like [`lsp`](lsp.md), this preset defines nothing. Its single entry is under the manifest's **`builtin-ops`** section, because it documents an op it does not own: `vim` dispatches from `_supertool.py` and runs whether or not the preset is listed.

```json
{ "presets": ["vim"] }
```

## What loading it changes, and what it does not

**Unaffected either way.** The op itself, and its payload route. `vim:::PATH:::SCRIPT` dispatches from core, and `vim:@-` derives its `path` and `script` fields from `_AT_FILE_BUILTIN_DEFAULTS` — a table in the module, not in any manifest — so both forms work in a repo with no `.supertool.json` at all. `tests/test_vim_preset_2026.py` asserts that against an empty config rather than against this repo's, because [#770](https://github.com/Digital-Process-Tools/claude-supertool/issues/770) is the case where a route was deleted by an edit to a `syntax` string and nothing said so.

**Affected.** Where the macro grammar appears in the *listing*. That description is 1,373 bytes carrying `hint: true`, which is why it renders in `ops-compact` — the listing that runs against the SessionStart byte cap — rather than only in `ops:full`. Measured on this repository, leaving the preset unlisted saves 1,478 bytes in `ops-compact` and `ops:full`, and 19 in the default `ops` listing, which has been signatures-only since [#1774](https://github.com/Digital-Process-Tools/claude-supertool/issues/1774).

**Also unaffected: `help:vim`.** `_shipped_config()` folds the shipped presets' `builtin-ops` into the reference it answers from, so a repo that never lists this preset still gets the full grammar one op away — with the line saying the entry describes the binary rather than that tree. Without that fold the move reintroduced [#1773](https://github.com/Digital-Process-Tools/claude-supertool/issues/1773) exactly: `help:vim` answering "no documented help" for an op the binary dispatches. It is the fallback only, so the listing bytes are still saved — `ops` renders the project's merged config and never the shipped reference.

## Whether to list it

List it if the repo edits with `vim` often enough that the grammar belongs in the listing rather than one `help:vim` away.

Leave it unlisted otherwise. The op still works, `help:vim` still answers in full, and `ops` reports the name under its "Also accepted, no reference in .supertool.json" line rather than staying silent about it. That is the whole trade after the fallback fix: 1,478 bytes against a `help:` round-trip.

## The alternative that was not taken

Splitting a one-line `description` from a `long` key that only `help:OP` renders reaches the same byte count in every listing while keeping the grammar available everywhere. It was not done here because it is a schema change touching every fat entry rather than one op, and it remains the better answer for the five `hint: true` entries still in `.supertool.json`: `guard` (2,166 bytes of description), `doctor` (1,341), `repo` (1,051), `registry` (583), `claims` (481).
