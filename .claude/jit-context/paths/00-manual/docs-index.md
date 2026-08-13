---
title: "docs/ — index, not a summary"
match: "docs/"
---

# `docs/contributing.md` (124KB / 1415 lines) — section map

**No line numbers, on purpose.** This table carried them until 2026-08-11 and they
were wrong twice over: thirteen of twenty-one rows sat ~79 lines short — `Cutting a
release` said 284 and it is at 363 — and the heading *levels* were wrong too, so a
reader looking for `## Cutting a release` found a `###` nested under `Changelog
fragments`. The note above the table said "search the heading text, not the number"
and the numbers were still there to be trusted, which is what a reader does.

The one call that answers this correctly, and cannot go stale:

```
supertool 'read:docs/contributing.md:::grep=^#'
```

| Heading | When you need it |
|---|---|
| Quick start | first-time setup |
| Checking syntax against the supported floor | before claiming "works on 3.9" |
| What the repo-wide Python guards scan/skip | guard false-positive on a new dir |
| Custom ops | adding an op to `.supertool.json` |
| Op schema | table of `cmd`/`timeout`/`syntax`/etc |
| `replaces` — the raw command this op supersedes | the #1347 guard's mapping |
| An op that takes a path declares where paths may point | `"paths": {"args": […], "root": …}` |
| Placeholders | `{file}`/`{dir}`/`{arg}`/`{args}`/`{path}` |
| Dispatch order | builtin vs custom vs preset vs alias |
| Extra config keys as environment variables | passing config into an op's subprocess |
| `scripts/` — this repo's own maintainer ops | the repo's own tooling entries |
| Presets / File layout / Resolution order / Preset schema | writing a preset manifest |
| Validators | stub only — real doc is `docs/validators.md` |
| Changelog fragments | `changelog.d/NNN.section.md` naming |
| Cutting a release (`###`, under Changelog fragments) | assembling fragments into `CHANGELOG.md` |
| Helper script conventions | writing `presets/*/foo.py` |
| HTTP requests go through `presets/_http.py` | any preset that calls a URL |
| Fetching a URL somebody else chose | user-supplied URL handling |
| The body is bounded, in bytes and in wall clock | streaming/size-limit rules |
| Text encoding | UTF-8 read/write rules, Windows console traps |

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
