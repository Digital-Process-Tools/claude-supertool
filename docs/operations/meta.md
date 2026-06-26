# Meta

Ops for self-documentation and version introspection. Used primarily in session-start hooks and agent prompts to onboard an LLM to a project's supertool setup in one call.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `introduction` | `introduction` | Output the project introduction text from `.supertool.json`. No `---` dispatch header — clean markdown. |
| `output-format` | `output-format` | Output format examples from `.supertool.json`. Shows what responses look like. |
| `ops` | `ops` | Full operations reference from `.supertool.json` — built-in ops, custom ops, and aliases with descriptions and examples. |
| `version` | `version` | Show supertool version. |
| `cwd` | `cwd:PATH` | Set the working dir for the whole call. **Must be the first op** — chdir's once before dispatch, then is stripped, so every following op resolves against `PATH`. Replaces a `cd PATH && ./supertool …` prefix (which trips the use-supertool hook and risks stale-cwd path poisoning) for cross-repo sessions. `~`/`$VAR` expanded; non-directory or non-first → error before any op runs. |

## Common patterns

Full LLM onboarding in one call — everything an agent needs to use supertool:

```bash
./supertool 'introduction' 'output-format' 'ops'
```

Use this in session-start hooks or agent system prompts. The model learns what ops exist, how output is formatted, and what the project uses supertool for — without reading any config files.

Check installed version:

```bash
./supertool 'version'
```

Compact variant used by the session-start hook to stay under Claude Code's ~7KB hook output cap:

```bash
./supertool 'introduction' 'output-format' 'ops-compact'
```

`ops-compact` drops examples on self-explanatory ops and prepends a warning if output still exceeds the cap, telling the model to fetch the full listing via `./supertool 'ops'`.

## See also

- [index.md](index.md) — full op table for all categories
- [docs/validators.md](../validators.md) — validator reference
