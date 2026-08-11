---
title: "changelog.d/ — CommonMark-gated fragments"
match: "changelog.d/"
mode: once, remind
---

# Filename grammar

`<issue>.<section>[.<slug>].md` — sections (Keep a Changelog headings):

```
added | changed | deprecated | removed | fixed | security
```

# Never edit CHANGELOG.md in a PR

Add a fragment under `changelog.d/` instead. Rule: `docs/contributing.md`,
"Changelog fragments" (the line number here went 41 lines stale; the heading did not).

# Nothing outside this directory may name a pending fragment

The tag **deletes** it. A test, doc example, fixture or jit note keyed to
`changelog.d/<n>.<section>.md` is green until the release and red on it and
every one after — and invisible from inside the PR that writes it. Shipped four
times: #941 (5 legs, v0.26.0), #953 (13/20, v0.27.0), #1231 (13/22, v0.33.0 —
not an assert, a filename in a swept tuple), #1053.

Point at `CHANGELOG.md`, or call `assert_change_is_findable(<issue>)` from
`tests/_changelog_findable.py`. Naming an *already consumed* fragment is fine.
Enforced by `tests/test_changelog_findable_{1053,1293}.py` (#1293).

# The guard is a real parser, not a scanner

Validator = CommonMark (`markdown-it-py`). A hand-rolled scanner was bypassed across four rounds: #927 → #930 → #932 → #934 → #935 → #936. Refused at any depth:
- headings
- link-reference definitions
- raw HTML
- an unclosed fence
- top level that isn't a single `-` bullet list

# No parser installed → `skipped`, not `ok`

There is deliberately no text-scanning fallback. `skipped` ≠ validated — don't read it as a pass.

# Quoting a heading inside a bullet

Fence it at the **bullet's own indent (2 columns)**, not 4 — CommonMark's 4-column code threshold is relative to the containing block, not the file.

```md
- Summary line.

  ```
  ## Quoted Heading
  ```
```

Closing fence at column 0 ends the fence early **and** the bullet **and** the list. Keep opener/closer both at 2.

# When it fires

`changelog-fragment` validator runs on any mutating op under `changelog.d/` **at write time**, not just in CI — #1132.
