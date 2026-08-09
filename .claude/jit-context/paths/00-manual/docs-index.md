---
title: "docs/ — index, not a summary"
match: "docs/"
mode: once, remind
---

# `docs/contributing.md` (98KB / ~1255 lines) — section map

| Heading | Line | When you need it |
|---|---|---|
| Quick start | 7 | first-time setup |
| Checking syntax against the supported floor | 17 | before claiming "works on 3.9" |
| What the repo-wide Python guards scan/skip | 44 | guard false-positive on a new dir |
| Custom ops | 62 | adding an op to `.supertool.json` |
| Op schema | 83 | table of `cmd`/`timeout`/`syntax`/etc |
| Placeholders | 96 | `{file}`/`{dir}`/`{arg}`/`{args}`/`{path}` |
| Dispatch order | 108 | builtin vs custom vs preset vs alias |
| Extra config keys as env vars | 112 | passing config into an op's subprocess |
| Presets | 132 | writing a preset manifest |
| File layout | 136 | where preset JSON + helper scripts live |
| Resolution order | 148 | project vs user vs shipped preset wins |
| Preset schema | 156 | same as op schema, wrapped in manifest |
| Validators | 188 | stub only — real doc is `docs/validators.md` |
| Changelog fragments | 194 | `changelog.d/NNN.section.md` naming |
| Cutting a release | 239 | assembling fragments into `CHANGELOG.md` |
| Helper script conventions | 281 | writing `presets/*/foo.py` |
| HTTP requests go through `presets/_http.py` | 293 | any preset that calls a URL |
| Fetching a URL somebody else chose | 329 | user-supplied URL handling |
| The body is bounded, in bytes and wall clock | 365 | streaming/size-limit rules |
| Text encoding | 384 | UTF-8 read/write rules, Windows console traps |

## Load-bearing details, don't re-derive

- **Syntax floor is 3.9–3.12.** `ast.parse(src, feature_version=(3,9))` is NOT sufficient — it only gates grammar (walrus/match/except*), not the tokenizer. PEP 701 nested same-quote f-strings parse clean under `feature_version` on a 3.12 host but `SyntaxError` on 3.9/3.10/3.11 (shipped as #473, caught by #478). Use `supertool._syntax_floor_check(paths)` / `pytest tests/test_syntax_floor_478.py`, or `$PYTHON39=/path pytest ...`.
- **Python guards** (`tests/_repo_walk.py`) decide source-vs-machine-state in order: (1) hardcoded name list (`__pycache__`, `.venv`, `.git`, etc. — git can't see these, esp. `.git` and untracked venvs), (2) `git ls-files --others --ignored --exclude-standard --directory`, (3) denylist fallback if git is unavailable. A dot-prefix alone is NOT exempt (#593 — `.github/`, `.githooks/` are source).
- **Custom op `syntax` field is PARSED, not just displayed**, for any op whose `syntax` contains `:::` — e.g. `MESSAGE[:::PATHS...]` derives field names `message`, `paths` for the `@file`/`@payload` route. Rewording that string for readability can silently break field derivation, silently deleting the op's payload route (no error) — docs still describe a route that no longer exists. Pinned by `tests/test_at_file_route.py::TestPayloadRoutePin`. Ref #770.
- **Preset resolution order**: `./presets/{name}.json` (project) → `~/.config/supertool/presets/{name}.json` (user) → `{install dir}/presets/{name}.json` (shipped). First found wins; project always overrides preset on name conflict.
- **Changelog fragments**: never edit `CHANGELOG.md` directly in a PR — add `changelog.d/<issue>.<section>.md`. Section = `added`/`fixed`/etc. Enforced by `.github/workflows/changelog.yml`.

## Sibling docs — what each owns

| File | Owns |
|---|---|
| `docs/validators.md` (117KB) | authoritative validator contract — contributing.md's Validators section just points here |
| `docs/configuration.md` | `.supertool.json` config keys |
| `docs/formatters.md` / `docs/notifiers.md` | formatter / notifier adapter contracts |
| `docs/input-forms.md` | `@file`/`@payload`/`@-` input-form mechanics |
| `docs/mcp-integration.md`, `docs/mcp-warm-process-servers.md` | MCP server usage/config, warm-process pattern |
| `docs/presets/{index,edits,map,meta,reads,search}.md` | shipped preset catalog + per-op-family reference |
| `docs/operations/{git,github,gitlab,watch}.md` (largest: `watch.md` 165KB) | per-integration op reference; plus `bluesky.md`, `claude-log.md`, `dashboard.md`, `devto.md`, `hashnode.md`, `xml.md` |
