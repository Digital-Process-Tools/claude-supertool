feat(vi)!: replace `;` action separator with `␞` (U+241E)

## Summary

**BREAKING:** vi scripts no longer use `;` as the action separator. The new separator is `␞` (U+241E SYMBOL FOR RECORD SEPARATOR) — chosen because it never appears in source code or natural text. TEXT inserts can now contain `;`, `{`, `}`, real newlines, etc. with no escaping needed.

## Why

Every other separator candidate collided with code Kevin pastes: `;` in PHP/JS, `|` in regex alternation, `&&`/`,` in expressions. Kevin kept tripping on `o use Foo;\nuse Bar;` (the `;` ended `o`'s TEXT, then `use Foo` became an "unknown verb" action). The `\;` escape softened it but never eliminated the failure mode.

`␞` has zero collision surface with anything Kevin can paste.

## What changed

- **Splitter:** `op_vi` now splits the script on `␞` only. No more `;` separator, no more `\;` escape, no more `\n`-as-separator autocorrect, no more `:s` PAT/REPL state machine (it existed only to keep `;` literal in regex — `␞` is always literal in regex anyway).
- **Real newlines are no longer separators.** They flow through to TEXT / regex args verbatim. One separator, zero ambiguity.
- **Tests:** 67 + 7 + 7 + 46 = 127 op_vi scripts rewritten across `test_vi.py`, `test_vi_autocorrect.py`, `test_vi_integration.py`, `test_vi_canonical.py`. Two tests deleted (their bug class no longer exists under `␞`).
- **README:** vi op syntax and example updated.

## Migration

Every vi script must replace `;` (separator) with `␞`. `\;` escape disappears (just type `;`).

Kevin's orchestrator (`CommandKevin::installSupertoolSymlink()`) does `git fetch + reset --hard origin/master` on every run, so the pinned `.kevin/claude-supertool/supertool.py` auto-refreshes after merge.

`CLAUDE-kevin.md` in dvsi will get a follow-up MR to sweep all `;` examples to `␞`.

## Test plan

- [x] 1244 tests pass, 91% coverage
- [x] Manual repro of the failing Kevin pattern: `8G␞o use Reflection;\nuse SiMonitor;` now works (semicolons inside the `o` TEXT stay literal)
