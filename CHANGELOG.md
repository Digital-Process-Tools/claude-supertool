# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file carries only the latest release. The full history is in [CHANGELOG.md on the default branch](https://github.com/Digital-Process-Tools/claude-supertool/blob/master/CHANGELOG.md).

## [0.65.0] - 2026-10-03

### Added

- **A slim `release` branch ships to the Anthropic plugin directory instead of the full tree** ([#2705](https://github.com/Digital-Process-Tools/claude-supertool/issues/2705)), ported from [claude-remember#851](https://github.com/Digital-Process-Tools/claude-remember/issues/851). The directory had scanned the default branch and stopped with `VALIDATION_INCOMPLETE` / `VALIDATION_NOT_EVALUATED` -- both policy holds -- on this repository's 1,726-file, 24.6 MB tree.

  A tag push now runs `.github/workflows/release-branch.yml`: it builds a tree with `tests/`, `docs/`, `.github/`, `.claude/`, `.oss/` and the other dev-only paths removed (`.github/scripts/build_release_tree.py`, driven by `.github/release-branch.json`), checks it against the directory's pre-submission checklist (`.github/scripts/check_release_tree.py`), smoke-tests every hook in isolation (`.github/scripts/smoke_release_tree.py`), and only then pushes one commit on top of `release`. Built from `v0.64.0`: 336 files, 6.5 MB, down from 1,726 files and 24.6 MB.

  `_supertool.py` cannot be denied -- it is the tool itself -- so it is a named, issue-referenced exception in `release-branch.json` (tracked separately in [#2706](https://github.com/Digital-Process-Tools/claude-supertool/issues/2706)): `check_release_tree.py` reports it as a `REVIEW` line rather than failing the build. Any other file over the 256 KiB limit still fails. See `docs/releasing.md` for the full sequence.

### Changed

- Moved the vim implementation out of `_supertool.py` into a new in-process
  module, `_supertool_vim.py`, imported lazily on the first `vim` call rather
  than paid by every invocation. `_supertool.py` keeps a thin `op_vim` stub
  that does the lazy import and delegates; dispatch, validators and rollback
  are unchanged. Step 1 of the `_supertool.py` size split (#2706) that keeps
  the plugin directory's 256 KiB per-file rule enforceable without a
  standing exception for the tool's own source.

- Moved `introduction`/`output-format`/`version`/`help`/`ops`/`ops:full`/
  `ops:roster`/`ops-compact`/`registry` out of `_supertool.py` into a new
  `_supertool_catalog.py` (~68 KiB), another slice of #2706 (shrinking
  `_supertool.py` under the plugin directory's 256 KiB per-file limit). Unlike
  `_supertool_doctor.py`/`_supertool_gc.py`, this part is loaded the same way
  as `_supertool_guard.py` -- `_load_part("_supertool_catalog")`, an
  `exec(code, globals())` at the exact source position the code used to
  occupy, not a real `import` -- because the moved code carries 8+ live
  `monkeypatch.setattr(supertool, ...)` sites on `_shipped_config` and its
  `_SHIPPED_CONFIG`/`_SHIPPED_CONFIG_DIR`/`_SHIPPED_CONFIG_STATE` globals
  across five test files; a real module split would give this code its own
  namespace and break every one of those patches. The moved code sat in two
  spans interleaved with `op_doctor`/`op_init` (already split out by #2714);
  those two stubs are untouched and still sit exactly where they were, between
  the two spans this file now holds concatenated, loaded by one `_load_part`
  call at the first span's former position.

- The config/presets-merge/env-knobs/exclude-paths/gitignore/rtk/display region
  of `_supertool.py` (#2706) is now a `_load_part()`-loaded part too, split out
  verbatim into `_supertool_config.py` beside it -- `.supertool.json`
  discovery/merge, every env-knob reader, the exclude-paths matcher, gitignore
  integration, rtk (Rust Token Killer) detection, and the display region
  (`plain_mode`, `mark`, the shared line-break helpers). No behavior change:
  every function keeps `__globals__ is _supertool.__dict__`, so the 46+
  existing `monkeypatch.setattr(supertool, ...)` sites into this region keep
  reaching the code they patch.

- Split `dispatch`, `dispatch_verdict`, `_dispatch_impl`, the per-op
  accumulators and `log_call` out of `_supertool.py` into `_supertool_dispatch.py`
  (~95 KiB), one slice of #2706 (shrinking `_supertool.py` under the plugin
  directory's 256 KiB per-file limit). Loaded with `_load_part("_supertool_dispatch")`
  at the exact source position the code used to occupy, exec'd into
  `_supertool.py`'s own `globals()` rather than imported as a real module --
  the same mechanism `_supertool_guard.py` already uses -- so every existing
  `monkeypatch.setattr(supertool, "dispatch", ...)` (18 sites across 6 test
  files) and `monkeypatch.setattr(supertool, "log_call", ...)` (24 sites
  across 10 test files) keeps reaching the code it patches, untouched.
  `_dispatch_impl`'s `global _DEFER_FORMATTERS, _FORMAT_QUEUE,
  _VALIDATOR_DEFER_QUEUE, _VALIDATOR_DEFER_SEEN` rebind is a non-issue for
  the same reason: every part shares one `globals()` dict with the core.

- Split `doctor`/`doctor:probe`, `init`/`init:write` and `gc` out of
  `_supertool.py` into `_supertool_doctor.py` (~50 KiB) and `_supertool_gc.py`
  (~8.6 KiB), one slice of #2706 (shrinking `_supertool.py` under the plugin
  directory's 256 KiB per-file limit). `op_doctor`/`op_init`/`op_gc` stay in
  `_supertool.py` as thin stubs that import the new modules lazily, so
  dispatch is unchanged; every cross-module call back into `_supertool.py`
  goes through a qualified `_supertool.NAME` form resolved at call time, so a
  test's `monkeypatch.setattr(supertool, ...)` on a helper that stayed behind
  is still seen. `_cache_root`, `_maybe_auto_gc` and its `atexit.register`
  call stay in `_supertool.py` -- moving an import-time atexit registration
  into a lazily-imported module would change both when it fires and its
  position in the process's atexit LIFO order. 11 tests across 8 files
  migrated their direct calls/monkeypatches of the moved private helpers
  (`_doctor_*`, `_init_*`, `_gc_sweep_all`, `_gc_sweep_kind`,
  `_GC_DEFAULT_RETENTION_DAYS`) to reference the new modules directly, rather
  than re-exporting them back into `_supertool.py` to keep an old patch
  green while it no longer reaches the code.

- Wave 1 of the `_supertool.py` size split (#2706): the write-op region
  (`op_replace`, `op_edit`, `op_json_set`, `op_paste`, `op_append`,
  `op_replace_lines`, `_atomic_write`, `_result_line`, and the
  rollback/near-miss-diagnostic helpers they share) moved verbatim into
  `_supertool_edit.py`, loaded by `_load_part()` at the position it used to
  occupy. Every existing `monkeypatch.setattr(supertool, ...)` patch site
  (55 over this region, including 44 on `_branch_reading`) keeps reaching
  the code it patches.

- Part of the `_supertool.py` size split (#2706): op names, synonyms,
  near-miss suggestions, safety classification, `_split_arg`, and the
  colon/extra-token refusals move out of `_supertool.py` into
  `_supertool_parse.py`, a `_load_part()`-loaded part (two contiguous spans
  of the pre-split file, concatenated into one, loaded with a single call).
  `monkeypatch.setattr(supertool, ...)` patch sites keep reaching the moved
  code (0 test monkeypatch sites besides `_batch_prevalidation_refusal`, all
  4 of them pinned). `tests/test_grep_around_comment_1842.py` is widened to
  search `_core_sources.core_source_text()` instead of `_supertool.py` alone,
  since the comment it pins moved with `_PATH_ARG_POSITIONS`.

- Lane 0 of the `_supertool.py` size split (#2706): a `_load_part()` loader
  that `exec`s a `_supertool_<x>.py` part file into `_supertool.py`'s own
  namespace at the exact position its code used to occupy, keeping the #931
  bytecode cache and every existing `monkeypatch.setattr(supertool, ...)`
  patch site intact. The raw-command guard is the pilot part, moved verbatim
  into `_supertool_guard.py` (0 test patch sites). `tests/_core_sources.py`
  derives the core-plus-parts file list from the `_load_part(...)` call
  sites so a source-scanning test widened to use it tracks the split instead
  of rotting with it.

- #2706 wave 1: the TOML mini-parser and `@file`/payload routing
  (`_detect_payload_format` through `_at_file_to_parts_generic`, including
  `op_payload_lint`) moved out of `_supertool.py` into `_supertool_payload.py`,
  a `_load_part()`-loaded part sharing `_supertool.py`'s own namespace, the
  same mechanism lane 0 built for the raw-command guard. No behaviour change.

- Part of the `_supertool.py` size split (#2706): the custom-op / preset
  resolution region -- `_safe_path`, containment, `_expand_env`,
  `_resolve_custom_op`, alias resolution and their direct helpers -- moved
  verbatim into `_supertool_presets.py`, loaded the same way lane 0's
  `_supertool_guard.py` is, with `_load_part("_supertool_presets")` at the
  exact position the code used to occupy. No behaviour change; every existing
  `monkeypatch.setattr(supertool, "_safe_path", ...)` site keeps reaching the
  same code, since the part still runs with `_supertool.py`'s own `globals()`.

- Part of the `_supertool.py` size split (#2706): `render_file`, `op_read`,
  `op_glob`, `op_ls`, `op_head`, `op_tail`, `op_wc`, `op_stat`, `op_tree` and
  `op_map` (the tree-sitter/ctags symbol map) moved verbatim into a new
  `_load_part`-loaded part, `_supertool_read.py`; `op_grep`, `op_around`,
  `op_between_symbol`, `op_between_pattern` and the pattern gate moved into
  `_supertool_grep.py`. The two parts' functions interleaved in the original
  source across five read spans and two grep spans rather than one contiguous
  chunk each (unlike lane 0's guard); `op_check` and `op_diff`, physically
  sitting inside that span but unrelated to either op family, were relocated a
  few hundred lines within `_supertool.py` itself rather than smuggled into
  either part. No behaviour changed and no test needed updating — both parts
  share `_supertool.py`'s own globals() exactly as lane 0's loader already
  established.

- Split validators, formatters, advice generation, `_run_with_validators`,
  `op_validate*` and `op_format*` out of `_supertool.py` into
  `_supertool_validate.py` (~144 KiB of moved code, ~148 KiB on disk with its
  header), one slice of #2706 (shrinking `_supertool.py` under the plugin
  directory's 256 KiB per-file limit). Loaded via a single
  `_load_part("_supertool_validate")` call -- a plain `exec(code,
  globals())` at the exact source position the moved code used to occupy,
  not a real `import` -- so every function still has `__globals__ is
  _supertool.__dict__` and every existing `monkeypatch.setattr(supertool,
  ...)` on a validator/formatter name keeps reaching the code it patches.
  `_FORMAT_QUEUE`, `_DEFER_FORMATTERS`, `_VALIDATOR_DEFER_QUEUE` and
  `_VALIDATOR_DEFER_SEEN` move with it; the `global` rebindings in
  `_dispatch_impl`/`main` keep working under the shared namespace. Five
  physical spans, not three or one: `_flat_cell`, and separately
  `_flat_field`/`_flat_keys` (plus the `_UNTRUSTED_FLAT` state the latter
  rebinds), sit inside the byte range this part occupied but are generic
  string-flattening helpers used all over core's own dispatch and payload
  code -- moving them would have cost ~55 `# noqa: F821` call sites in
  unrelated core code instead of the ~20 genuinely tied to this part's own
  dispatch points, so they stay in `_supertool.py` and the part is loaded
  from five source regions through the one `_load_part` call at the first
  region's position. The notifier subsystem (`_applicable_notifiers`,
  `_run_notifiers`, `_first_changed_line`, `_sweep_old_notifier_temp_files`
  and its import-time call/`atexit` registration) stays in `_supertool.py`
  for the same kind of reason -- a third caller, dispatch-level read-op
  notification, has no validator/formatter involvement of its own. One
  test, `test_format_staged_skips_symlinks`, grepped
  `Path(supertool.__file__).read_text()` for an implementation detail that
  moved with this split; it now reads `_core_sources.core_source_text()`
  (core plus every loaded part) instead of assuming everything still lives
  in one file. Three dead `import fnmatch` lines surfaced inside the moved
  code once it became a standalone file subject to whole-file lint (the
  tree-wide F401 ignore covers only files with pre-existing history) and
  were removed.

### Fixed

- `git-commit:@-`'s `message = @rest` header now refuses a message whose last
  line looks like a swallowed `paths = [...]` payload field, when no paths
  were given any other way. Previously the tail of a `@rest` message was
  taken verbatim with no scanning for a later sibling field: a `paths = [...]`
  line written after the message text became the last line of the commit
  body, PATHS stayed empty, and -- with foreign changes already staged --
  the op committed the index exactly as it stood, under a mangled message,
  and reported PASS (#2667).

- `FIELD = @rest` payload tails no longer carry their heredoc's own closing
  newline into `old`/`new` (`edit`/`replace`) or `pattern` (`grep` and its
  siblings) -- an `edit` used to write a stray blank line and a `grep`
  pattern used to match every line in the file. `content`-shaped fields
  (`paste`/`append`) still get the tail byte for byte, since a full-file
  write may legitimately end on a blank line (#2668).

- `git-commit`'s leaked-key guard (#2656) now matches any TOML-legal spacing
  and leading indentation around a leaked `paths =` / `message =` key, not
  only the canonical `paths = [` / `message = ` byte-for-byte prefix.
  Previously `paths=[...]`, `paths  = [...]` and an indented `  paths = [...]`
  all slipped past the guard and committed under the mangled subject (#2669).

- **`gitlab-mr`'s poller re-asks the approvals endpoint while a stored
  `approved` is still `False`, not only on the one tick that exits
  `not_approved`** ([#2670](https://github.com/Digital-Process-Tools/claude-supertool/issues/2670)).
  Previously, once a higher-priority blocker (a draft, an open thread, a
  failing pipeline) took the `detailed_merge_status` field on the same tick
  the MR left `not_approved`, the one confirm request answered `False` and
  was never asked again -- the approval rule could later be satisfied while
  the MR was still draft, or after it went `mergeable`, and no tick would
  ever re-confirm it. The cost stays bounded to the same "once per red
  streak, not once per tick" budget #2645 already pays.

- `git-push`'s open-MR and dead-MR lookups (`query_open_mr_result`,
  `query_last_mr_result`) now filter out cross-repository matches. Both
  probed `gh pr list --head BRANCH`, which matches the head branch name in
  ANY repository -- a stranger's fork PR with the same branch name could be
  rendered as this branch's own open or dead MR. Both gh probes now request
  `isCrossRepository` and read past a fork's row to the first same-repo
  match, raising `--limit` so a same-repo match is not dropped when it sits
  behind a fork match (#2672).

- `git-push`'s dead-MR line no longer renders a tracker row's missing
  `state` field as an ordinary `closed` MR. `_gh_last_fields` and
  `_glab_last_fields` pass a row's `state` through untouched, so a row with
  no `state` key gave `state: None`, and `_dead_mr_lines` folded that into
  `(mr.get("state") or "closed")`, silently equating "unanswered" with
  "closed". It now renders `state unknown (the tracker row carried none)`
  instead (#2673).

- `git-commit`'s post-commit "Next:" hint no longer silently recommends a
  plain push when the branch's own MR/PR was already merged or closed.
  `_existing_mr_for_branch` used `query_open_mr`, which discards WHY nothing
  is open, so a dead request read exactly like a branch that never had one.
  When the open lookup finds nothing, the hint now asks `query_last_mr_result`
  (the same #2657 lookup `git-push` uses) and, when the branch's most recent
  request was merged or closed, names it instead of pointing at a plain push
  (#2674).

- **`channel:health`'s own description now names all six states**, including
  `BOUND, UNPROVEN` (exit 8), added by #2658
  ([#2675](https://github.com/Digital-Process-Tools/claude-supertool/issues/2675)).
  It previously still said "five states" and its first-line branching sentence
  omitted the sixth, so a caller who branched the way the description told
  them to had no key for it.

- **`gh-branch`'s poller no longer re-emits `went_green` for an unchanged
  sha after an intermediate `unknown` reading**
  ([#2676](https://github.com/Digital-Process-Tools/claude-supertool/issues/2676)).
  `UNKNOWN` in any form -- an unreconciled tally, or the #2436/#2537 grace
  window reaching its threshold -- is not an answer this poller settled on:
  the stored state now keeps the last real answer (`answer_state`/
  `answer_sha`) separately from the last raw read, and a recovery back to
  that same answer on the same sha no longer fires. A genuine transition
  (GREEN -> FAILED -> GREEN, a re-run) still fires every time.

- **The guard-wrapper test spawns now scale their outer timeout on Windows instead of sitting at a flat 120s**
  ([#2687](https://github.com/Digital-Process-Tools/claude-supertool/issues/2687)).
  `test_the_wrapper_denies_a_replaced_command` timed out at exactly 120s on a
  `windows-latest` runner in PR #2683's CI, while that same job's own
  duration report already named it the slowest test in the whole suite --
  the margin was thin even on a run that did not hit the wall. Same shape as
  #658/#702: the bash wrapper (`hooks/pre-bash-guard.sh` under `bash.EXE`)
  has no internal timeout of its own for `tests/_adapter_budget.py`'s
  `inner_budget()` to read, so the new `wrapper_budget()` applies the same
  Windows multiplier directly to a flat 120s base instead of deriving one.
  Seven spawns across six files moved off the hardcoded literal:
  `test_guard_interpreter_ladder_1390.py` (both sites, including the
  module-level gate that decides whether the rest of the file runs at all),
  `test_guard_hook_cost_1377.py`, `test_guard_partial_envelope_1377.py`,
  `test_guard_envelope_serialisation_1613.py`,
  `test_guard_reserialised_envelope_1625.py` and
  `test_hook_interpreter_windows_1401_1402.py`.

- `git-push`'s open-MR and dead-MR lookups (`query_open_mr_result`,
  `query_last_mr_result`) no longer report "no MR" when a `gh pr list` fetch
  came back full of forks exactly at its own `--limit`. A same-repo match
  could be sitting behind the last fork row, unseen -- the fetch is now
  reported as unanswered/unknown in that case, same as any other lookup that
  did not fully run, instead of the stated absence a branch with zero rows
  also produces (#2691, found during #2672's own self-review).

- `watches` no longer keeps a poller `STALE` forever after a confirmed
  `:reload`, nor recommends `:reload` again as the fix once it already ran.
  `_reload_poller` now records `reloaded_at` and a fresh fingerprint on the
  state file when the reload succeeds; `version_state_of` gets a fourth
  state, `RELOADED`, for a process whose `poller.py` is current as of that
  reload but whose `dispatcher.py`/`transport.py` are still the fork-time
  copies. The footer for a `RELOADED` row now names `unwatch` + `watch` as
  the only way to a fully current process, instead of the `:reload` that
  cannot reach it -- and that remedy now actually works: a fresh fork clears
  any earlier lifetime's `reloaded_at`, so a genuinely current process no
  longer inherits a stale reload marker from the poller it replaced (#2694).

- `watches` no longer claims that `unwatch` then `watch` forks a fresh poller
  with an empty state that re-announces everything as new on its first tick.
  `cmd_unwatch` never cleared the state file, so the fresh poller actually
  resumes the prior `source_state` from disk and stays quiet unless the world
  changed while it was down -- the text was wrong, not the code. Reworded the
  `cmd_reload` docstring and note, the `cmd_list` STALE and RELOADED footers,
  a `dispatcher.py` module comment, and `docs/presets/watch.md` to say so
  (#2697).

### Security

- `gh-issue-comment`'s `:edit=COMMENT_ID` now verifies the comment belongs to
  the issue or PR named before PATCHing it. Previously the PATCH was built
  from `COMMENT_ID` alone -- a mistyped or stale id could silently overwrite
  a comment on an unrelated issue or PR, while the `[result]` line still
  reported success against the issue number the caller asked for (#2665).

- The bluesky/devto/hashnode/youtube injection-scan warning (`⚠ POSSIBLE
  INJECTION — ...`) only flattened `\r` and `\n` out of a detected hit before
  printing it, so U+2028, U+2029, VT, FF, FS, GS, RS and NEL -- every other
  separator `str.splitlines()` treats as a line boundary -- still let attacker
  text land at column 0 of what looks like the tool's own line. Routed through
  `_untrusted.flat()`, which handles the full set, in all four presets'
  `_sanitize.py` (kept byte-identical) and in each preset's own `read.py`,
  which builds the same warning line independently of `wrap()`. The
  free-text fields rendered right next to that warning -- a comment's
  `author`/`text` (youtube's `read`/`status_since`), a reply's own text
  (bluesky), a comment body (devto/hashnode), and youtube's own video
  `title`/`channelTitle` -- carried the same `\n`-only gap and are now
  routed through `flat()` too (#2671).

- Same #2671 gap, at the sites that fix deliberately left out (#2680 said so
  in its own commit message): `list.py`/`search.py`/`status_since.py` across
  bluesky, devto, hashnode and youtube, plus the bluesky-engagement watch
  poller, each reimplemented the pre-#2671 bare `\n`-only replace instead of
  calling `_untrusted.flat()`, so a post/comment/notification text carrying
  U+2028, U+2029, VT, FF, FS, GS, RS or NEL could still put attacker text at
  column 0 of a forged output row. Routed through `flat()` at all eight
  sites (#2681).

- `claude-log`'s session-transcript renderer (`tail.py`'s `[result]`/`[result/ERR]`
  lines, `list.py`'s excerpt) now flattens every line-boundary separator
  `str.splitlines()` recognises, not just bare `\r`/`\n`. A tool_result part
  can carry text a stranger chose -- a WebFetch body, a bash stdout capture --
  even though the `.jsonl` transcript file itself is local on disk, and one of
  the eight separators the old bare `.replace()` idiom left untouched
  (U+2028, U+2029, VT, FF, FS, GS, RS, NEL) could still forge a line at
  column 0 of a rendered transcript row. Same defect class, and same fix
  (`_untrusted.flat()`), as #2671/#2680/#2681 -- #2681 itself deferred this
  file alongside `presets/xml/_common.py` as rendering "local files", but the
  rationale did not hold here (#2685).

- No test caught the bare `.replace("\r", ...)`/`.replace("\n", ...)` idiom
  recurring at a new untrusted-render call site -- it was fixed three separate
  times by hand (#2671, #2680, #2681), each time in a self-review, because
  that idiom only strips CR/LF and leaves U+2028, U+2029, VT, FF, FS, GS, RS
  and NEL -- every other separator `str.splitlines()` treats as a line
  boundary -- able to put attacker-chosen text at column 0 of a forged output
  row. `tests/test_bare_separator_replace_2686.py` is a repo-wide AST scan
  for the literal idiom itself across every preset that renders remote or
  tracker text (bluesky, devto, hashnode, youtube, watch, github, gitlab,
  git, notifiers), so a new instance is caught in CI instead of by hand
  (#2686).

[0.65.0]: https://github.com/Digital-Process-Tools/claude-supertool/releases/tag/v0.65.0
