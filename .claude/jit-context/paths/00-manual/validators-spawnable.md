---
title: "validators/ -- bin_resolve.describe_unresolved(), _refuse_spawn(), and the spawned-heads register"
match: "validators/"
---

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
