# `lsp` preset

Documentation for five ops that are **built into supertool** and dispatch from core: `workspace`, `resolve`, `diag`, `hover` and `rename`.

This preset ships no `cmd` and no helper scripts. Enabling it does not add capability — those five ops run whether or not it is listed. What it adds is documentation: with the preset loaded, `ops`, `ops:full` and `help:OP` answer for them; without it, they are reported under `ops`' "Also accepted, no reference in .supertool.json" line, and `help:hover` says it has no documented help.

Its entries live under **`builtin-ops`** in the manifest, not `ops`. That is the difference between documenting an op and defining one, and it is load-bearing: four guards sweep `config["ops"]` on the premise that every entry there resolves to a script ([#1269](https://github.com/Digital-Process-Tools/claude-supertool/issues/1269)), declares a safety class ([#1231](https://github.com/Digital-Process-Tools/claude-supertool/issues/1231)), declares a path-containment boundary ([#1287](https://github.com/Digital-Process-Tools/claude-supertool/issues/1287), [#1350](https://github.com/Digital-Process-Tools/claude-supertool/issues/1350)) and appears in the `replaces` census ([#1384](https://github.com/Digital-Process-Tools/claude-supertool/issues/1384)). A built-in's documentation can supply none of those — its script is `_supertool.py`, its class is `_OP_SAFETY_BUILTIN`, its paths go through the built-in chokepoint — so it belongs in a section those sweeps do not read.

```json
{ "presets": ["lsp"] }
```

## Why the docs are separable at all

Four of the five reach a language server through `.supertool.json`'s `mcp` block — see [mcp-integration.md](../mcp-integration.md). A project with no server configured cannot use them, and until [#2025](https://github.com/Digital-Process-Tools/claude-supertool/issues/2025) it still paid for their entries in every `ops` listing, including the one the SessionStart hook prints under a byte cap. Measured on this repository: 69 bytes in `ops` and `ops-compact`, 641 in `ops:full`.

`resolve` is the exception and is included anyway: it uses `mcp.tools.resolve` when one is configured and falls back to a heuristic glob when none is, so it works everywhere. It sits with the cluster it belongs to rather than alone in `builtin-ops`.

## Ops

| Op | Syntax | Notes |
|----|--------|-------|
| `workspace` | `workspace:PATH` | One-shot IDE-style view: file + symbols + validators + siblings + git + references + tests. Heavy; for first-touch on an unfamiliar file. |
| `resolve` | `resolve:SYMBOL[:FROM_FILE]` | Symbol → file. LSP when configured, heuristic glob otherwise. |
| `diag` | `diag:FILE` | LSP errors and warnings for one file. |
| `hover` | `hover:SYMBOL:FILE` | LSP hover: type and docs for a symbol. |
| `rename` | `rename:OLD:NEW:FILE` | LSP workspace rename. Safer than `sed`. **Writes.** |

## A preset cannot shadow a built-in

`dispatch` reaches `_resolve_custom_op` — the path every preset op takes — only on the fallthrough, after each built-in branch has declined. A preset entry named `hover` is therefore never consulted for execution, whatever `cmd` it declares. That is what makes a doc-only entry safe, and `tests/test_lsp_preset_2025.py` pins it rather than leaving it to be re-derived.

The same is true of the safety class: `_op_safety_classes` skips any name already in `_OP_SAFETY_BUILTIN`, so this preset deliberately declares none. `rename` is classified `writes` by the binary, and a preset saying otherwise would be text nobody reads.
