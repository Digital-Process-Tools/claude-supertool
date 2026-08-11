---
title: "docs/ — index, not a summary"
match: "docs/"
mode: once, remind
---

# `docs/contributing.md` (110KB / 1300 lines) — section map

Re-derived 2026-08-11 by grepping the headings. Fourteen of these twenty rows
were stale by 13–45 lines, all in the same direction — the file only grows, so a
row is a floor. Search the heading text, not the number.

| Heading | Line | When you need it |
|---|---|---|
| Quick start | 7 | first-time setup |
| Checking syntax against the supported floor | 17 | before claiming "works on 3.9" |
| What the repo-wide Python guards scan/skip | 44 | guard false-positive on a new dir |
| Custom ops | 62 | adding an op to `.supertool.json` |
| Op schema | 83 | table of `cmd`/`timeout`/`syntax`/etc |
| Placeholders | 109 | `{file}`/`{dir}`/`{arg}`/`{args}`/`{path}` |
| Dispatch order | 122 | builtin vs custom vs preset vs alias |
| Extra config keys as environment variables | 126 | passing config into an op's subprocess |
| `scripts/` — this repo's own maintainer ops | 154 | the repo's own tooling entries |
| Presets | 174 | writing a preset manifest |
| File layout | 178 | where preset JSON + helper scripts live |
| Resolution order | 190 | project vs user vs shipped preset wins |
| Preset schema | 198 | same as op schema, wrapped in manifest |
| Validators | 231 | stub only — real doc is `docs/validators.md` |
| Changelog fragments | 237 | `changelog.d/NNN.section.md` naming |
| Cutting a release | 284 | assembling fragments into `CHANGELOG.md` |
| Helper script conventions | 326 | writing `presets/*/foo.py` |
| HTTP requests go through `presets/_http.py` | 338 | any preset that calls a URL |
| Fetching a URL somebody else chose | 374 | user-supplied URL handling |
| The body is bounded, in bytes and in wall clock | 410 | streaming/size-limit rules |
| Text encoding | 429 | UTF-8 read/write rules, Windows console traps |

## Load-bearing details, don't re-derive

- **Syntax floor is 3.9–3.12.** `ast.parse(src, feature_version=(3,9))` is NOT sufficient — it only gates grammar (walrus/match/except*), not the tokenizer. PEP 701 nested same-quote f-strings parse clean under `feature_version` on a 3.12 host but `SyntaxError` on 3.9/3.10/3.11 (shipped as #473, caught by #478). Use `supertool._syntax_floor_check(paths)` / `pytest tests/test_syntax_floor_478.py`, or `$PYTHON39=/path pytest ...`.
- **Python guards** (`tests/_repo_walk.py`) decide source-vs-machine-state in order: (1) hardcoded name list (`__pycache__`, `.venv`, `.git`, etc. — git can't see these, esp. `.git` and untracked venvs), (2) `git ls-files --others --ignored --exclude-standard --directory`, (3) denylist fallback if git is unavailable. A dot-prefix alone is NOT exempt (#593 — `.github/`, `.githooks/` are source).
- **Custom op `syntax` field is PARSED, not just displayed**, for any op whose `syntax` contains `:::` — e.g. `MESSAGE[:::PATHS...]` derives field names `message`, `paths` for the `@file`/`@payload` route. Rewording that string for readability can silently break field derivation, silently deleting the op's payload route (no error) — docs still describe a route that no longer exists. Pinned by `tests/test_at_file_route.py::TestPayloadRoutePin`. Ref #770.
- **Preset resolution order**: `./presets/{name}.json` (project) → `~/.config/supertool/presets/{name}.json` (user) → `{install dir}/presets/{name}.json` (shipped). First found wins; project always overrides preset on name conflict.
- **Changelog fragments**: never edit `CHANGELOG.md` directly in a PR — add `changelog.d/<issue>.<section>.md`. Section = `added`/`fixed`/etc. Enforced by `.github/workflows/changelog.yml`. **And nothing outside `changelog.d/` may name a pending fragment by path** — the tag deletes it, which reddened four release commits before `tests/test_changelog_findable_1293.py` started refusing it (#1293).

## Sibling docs — what each owns

| File | Owns |
|---|---|
| `docs/validators.md` (117KB) | authoritative validator contract — contributing.md's Validators section just points here |
| `docs/configuration.md` | `.supertool.json` config keys |
| `docs/formatters.md` / `docs/notifiers.md` | formatter / notifier adapter contracts |
| `docs/input-forms.md` | `@file`/`@payload`/`@-` input-form mechanics |
| `docs/mcp-integration.md`, `docs/mcp-warm-process-servers.md` | MCP server usage/config, warm-process pattern |
| `docs/operations/{index,edits,map,meta,reads,search}.md` | per-op-family reference for the builtin file ops |
| `docs/presets/{git,github,gitlab,watch}.md` (largest: `watch.md` 165KB) | per-integration op reference; plus `bluesky.md`, `claude-log.md`, `dashboard.md`, `devto.md`, `hashnode.md`, `index.md`, `xml.md` |

**These two rows were swapped until 2026-08-09**, and the swap was load-bearing: two agents in one evening went looking for `docs/operations/watch.md`, found `docs/operations/` holding a different six files, and concluded *"the index points at a directory layout that is gone"* — so one of them documented `radar` by guessing at a file and the other reported the whole index as rotten. Neither read the 165KB doc that actually existed one directory over.

Note what made it convincing: **`watch.md` really is 165KB.** The size was right and the path was wrong, so the entry corroborated itself. Verify a path by listing the directory, not by recognising a detail in the row.

There is no `docs/presets/radar.md`. `radar` is documented inside `docs/presets/watch.md`.
