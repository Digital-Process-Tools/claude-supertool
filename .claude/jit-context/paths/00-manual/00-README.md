# Declined trap.d fragments

Curate pass over `trap.d/`, 2026-09-17. Every fragment below was read, judged not worth a
jit-context rule, and deleted from `trap.d/` — this line is the trace, so a re-discovered,
already-declined observation is not re-logged forever (`skills/manager/phases/findings.md`).

- **1165.windows-shlex-backslash-corrupts-unquoted-cmd-template-path** — already documented
  word-for-word in `tests/test_custom_ops.py::TestPlaceholderInjection`'s own docstring; the gap
  was not searching for it before writing a fresh helper, which a new rule does not fix either.
- **1221.no-cut-blocks-write-op-receipts** — real gap (`paste ... | tail` is wrongly blocked
  alongside genuine reads), but `supertool-no-cut.md` was already at 3,183 of its 3,200-byte
  tools-rule cap with no room to add the caveat; needs a code fix (exempt write-op receipts from
  the matcher) rather than a longer rule body.
- **1712.grep-full-word-flag-collides-with-a-directory-named-full** — narrow, and the identical
  tradeoff for `count`/`no-auto-read` was already accepted; unconfirmed anyone has hit any of the
  three in practice.
- **1783.shipped-reference-path-message** — a specific, already-scoped-out code fix (one error
  message names the wrong file on one fallback route), not a recurring lesson.
- **227.http-error-body-not-newline-stripped** — one-off code defect (an unstripped newline in a
  truncated error string), not an agent-facing lesson.
- **227.malformed-limit-silent-default** — one-off code defect (a malformed `|N` silently falls
  back to the default with no notice), not an agent-facing lesson.
- **227.stale-87-shipped-ops-docstring** — a stale count in test prose, never asserted, never
  reddens CI; not worth a rule.
- **2296.fake-api-page-substring-match** — narrow to one pagination-stub shape; the fragment's own
  author was "not sure this is worth a rule on its own."
- **2342.output-format-hardcoded-version-goes-stale** — one-off code defect (a stray hardcoded
  version literal outside `version_sites`); the fix is code (render from `VERSION`) or adding a
  site, not agent knowledge.
- **2358.auditor-spawn-violated-read-only-instruction** and **2226.reviewer-deleted-untracked-file-in-main-clone**
  — both about a spawned review agent's containment when briefed read-only. That briefing and
  enforcement lives in the `oss` plugin's own `agents/developer/review.md`, not this repo's
  jit-context; no path/tool/vocabulary trigger in claude-supertool itself captures "a spawned
  reviewer ran a mutating command."
- **2362.no-run-stale-ages-committer-date-not-push** — one-off code defect (a staleness clock
  reads committer date instead of first-observed time), not an agent-facing lesson.
- **2375.review-return-backref-false-positive** — a false positive in the `oss` plugin's own
  `review_return.py` classifier; the fragment's own disposition is to report it upstream as a
  plugin-tooling defect, not to write a supertool rule.
- **2404.cross-repo-gh-pr-statusline-fragment-mismatch** and **2472.cross-repo-gh-pr-mirror-write-wrong-repo**
  — one-off code defects (a fragment/mirror key that never folds in the resolved repo), not
  agent-facing lessons.
- **2429.teardown-force-single-colon-misparsed** — one-off code defect (colon-tokenizer misparse
  of `worktree:teardown:force`); its secondary note (a write op run during a read-only audit) is
  the same containment question as 2226/2358, declined for the same reason.
- **2433.walkup-loaders-do-not-catch-the-same-exceptions** — names a missing cross-loader
  consistency test; a code/test gap, not something a path pattern would change an agent's
  behaviour about.
- **2434.git-config-identity-pollution** — root cause (which process wrote the polluted identity
  into shared `.git/config`) was never found; with no root cause there is no actionable
  "before you touch X" rule, only a thin, unconfirmed-to-recur "check the author after committing."
- **2436.no-run-streak-fixture-omits-persisted-state** — a test-fixture gap unreachable via normal
  polling operation, one-off.
- **2447.monkeypatch-through-a-re-export-misses** — the fragment names two candidate lessons and
  is "not sure which is the rule"; both are general testing discipline (patch where the body
  resolves the name; anchor assertions on a field, not a bare word) already covered by this
  repo's TDD culture rather than a new path-specific trap.
- **2449.force-respawn-reaps-under-autospawn-suppression** and **2449.pid-probe-blind-during-respawn-window**
  — one-off code defects in the MCP daemon respawn/reap sequencing, not agent-facing lessons.
- **2469.node-version-check-swallows-parse-failure-reason** — a one-off diagnostic-quality gap in
  a test helper's exception handling, not a recurring trap.
- **2472.gh-issue-mirror-isdigit** — one-off code defect (a sibling file not yet swept into the
  existing `test_ascii_digit_guards_1727.py` GUARDED tuple); the fix is adding a file to an
  existing test, not a jit rule.
- **2473.repeated-single-table-header-silently-merges-in-fallback-toml** — a one-off parser
  divergence between the stdlib and fallback TOML readers, code-level fix.
- **2478.stranded-header-present-tense-from-clockless-record** — one-off code defect (a channel
  health header overclaims currency with no clock behind it), not an agent-facing lesson.
- **2484.a-swallowed-chmod-leaves-a-credential-unprotected-silently**, **2484.chmod-failure-swallowed-silently**,
  **2484.session-json-write-not-atomic-across-processes**, **2484.two-writers-share-one-session-file-non-atomically**
  — one-off code defects in the bluesky session-file writer, each explicitly named out of scope
  by #2484 itself.
- **2484.claude-md-write-class-op-list-is-short-by-two** — about `CLAUDE.md`'s own accuracy, which
  only a session explicitly asked to edit `CLAUDE.md` may touch (`CLAUDE.md`'s own "curated by
  hand" rule); not this pass's job and not a jit rule.
- **2493.py-escape-advisory-global-leaks-across-calls** and **2493.py-escape-advisory-silent-for-tilde-relative-paths**
  — one-off code defects in a module-level advisory global's pop/key logic, code-level fixes.
- **2505.same-needs-awk-pattern-elsewhere** — names four files that may need the same fix #2505
  made to three siblings, but the fragment's own text says the remaining work is first checking
  which jit-context rule each of the four exercises before anything can be written down; a
  code/test dedup task, not an agent-facing lesson yet.
- **2509.budget-section-goes-silent-when-the-process-scan-fails** — one-off code defect (a missing
  `else` branch); the general defect it names is already `three-states.md`'s own subject, and a
  second entry there would restate the table rather than add to it.
- **2509.gh-stderr-embedded-newlines-not-fenced-in-budget-render** and **2509.rate-limit-digit-collision-in-identifier**
  — one-off rendering/matching defects in the watch fleet's rate-limit handling, code-level fixes.
- **2536.pr-only-silent-collapse-on-malformed-config** — one-off code defect (one except clause
  collapsing two different causes to the same answer); a design question for a future pass per
  the fragment's own text, not a jit rule.
- **2543.force-token-confirms-and-overrides-two-guards** — already fixed in `youtube_comment`; the
  `bluesky` sibling is an open code defect. The proposed general rule ("a confirmation token and
  an override token must not be the same flag") is code-review guidance for whoever next adds
  `require_confirm`, better placed in `presets/_publish_safety.py`'s own docstring than a jit rule
  with no path/tool trigger to attach to.
- **2543.gh-branch-renders-a-failed-read-as-NO-RUN** — the general defect (an absence the tool
  produced read as an absence in the world) is already `three-states.md`'s own subject; the
  `gh-branch` instance is a code fix, and the polling-loop lesson ("break on a positive state,
  never on a missing token") has no path/tool trigger in this repo to attach a rule to.
- **520.channel-consumer-ships-without-its-dependency** — an install/packaging defect (a fresh
  plugin install has no `node_modules` for the bundled MCP consumer); install-audit's territory,
  not a jit-context trap for an agent mid-task.
- **526.devto-engagement-per-article-fetch-failure-silent-forever** — one-off code defect, not new
  in kind to a pre-existing silent failure, explicitly ranked non-urgent by its own fragment.
- **691.a-refusal-that-prints-its-own-bypasses** — a self-declared design decision, already
  recorded in the #691 changelog entry, not a fresh finding needing a rule.
- **227.oauth-error-body-unescaped-in-receipt** and **227.oauth-loopback-port-race-raw-traceback**
  -- both already fixed in `f632a37d` (#2584): `comment.py:140` now `repr(safe_short(str(e),
  300))`, and `_oauth.py`'s `HTTPServer` bind is now wrapped in `except OSError`. Verified
  directly against the current file, not just the commit message.
- **227.comment-overstates-safe-short-vs-repr** -- a one-line inline-comment wording nit
  ("escaped the same way MISMATCH does" slightly overstates `safe_short`'s own share of the
  work); caller-visible behaviour is correct either way, no single best rewrite, not worth a
  rule.

Curate pass over `trap.d/`, 2026-09-18.

- **1595.edit-at-dash-no-match-visually-present** -- root cause undetermined at the time (a
  literal-backslash interaction or a whitespace difference `read`'s rendering does not
  distinguish, the fragment's own text is unsure which); with no root cause there is no
  actionable "before you touch X" rule, same reasoning as 2434 above.
- **2607.raw-write-guards-blocked-correctly** -- the fragment's own text says it: "not a
  defect -- the guards did their job," logged only as a data point on how often the raw-write
  reflex fires despite the existing guard. No action needed unless it recurs at a rate the
  guard's own prose isn't landing; nothing here for a rule to say that the guard doesn't
  already say.
- **2607.read-start-end-vs-offset-limit-ambiguity** -- `read:PATH:1:90` parsed as
  OFFSET:LIMIT, not the START:END the caller meant; recurred for two independent callers in
  one session. Already documented in the op's own signature (`read:PATH[:OFFSET:LIMIT|:
  START-END|:full]`, `.supertool.json`'s `read` entry) and the op self-corrects on the next
  call via its own returned hint -- cost one round-trip, not a wrong answer. The fragment's
  own text says any further fix is a design question for the op itself (a warning on a
  large second argument, or a more prominent description), not a jit-context rule; no
  path/tool/vocabulary trigger here would fire only on this mistake without firing on every
  ordinary `read` call.

Curate pass over `trap.d/`, 2026-09-19.

- **1985.oss-local-json-git-excluded-claim-is-per-checkout** -- `presets/oss/tick.py` and
  `presets/oss.json` describe `.oss.local.json` as "git-excluded", which is only true of a
  checkout that ran whatever writes this maintainer's own `.git/info/exclude` line (the tracked
  `.gitignore` has no entry for it); a fresh checkout that never did so has no exclusion at all.
  A specific, already-scoped wording fix to two comments, not a recurring agent-facing lesson --
  no path/tool trigger here would change what an agent does differently next time, only what two
  comments claim.

Curate pass over `trap.d/`, 2026-09-23.

- **1868.review-return-classifier-false-positive** -- another false positive in the `oss`
  plugin's own `review_return.py` classifier, same class as `2375.review-return-backref-false-positive`
  above and declined for the same reason: it is plugin-tooling behaviour, not something a
  claude-supertool jit-context rule can change. Re-running the classifier against synthetic
  messages shaped like the one described (a trailing "Findings: N" tally sentence after
  properly-headed enumerated findings) reproduced neither `states-findings` nor
  `referred-not-stated` -- the fragment's own raw message text was not preserved, and without it
  the exact trigger (whether `_HEADER`'s `re.search` matched the trailing tally line rather than
  a real header, or something else) cannot be confirmed from here. The fragment's own text already
  names this as the right next step ("determine via `scripts/review_return.py`'s actual regex")
  and the right owner (the `oss` plugin's own source, not this repo).

Curate pass over `trap.d/`, 2026-09-23 (second pass, worktree curate/20260923T162954Z).

- **2661.fetch-approvals-severity-nuance-dropped** -- merged, not declined: folded a short
  severity-bound sentence back into `.claude/jit-context/paths/00-manual/presets-watch.md`'s
  `_fetch_approvals()` section ("Bounded, not a standing outage: fires at most once per real
  transition, so a failure caps at one missed tick."), the paragraph #2660's curate commit dropped
  when folding in `trap.d/2645.approvals-lookup-failure-not-surfaced.md`. Kept to 110 bytes so the
  file stays under the 6,000-byte paths-rule budget (`tests/test_jit_rule_body_budget_1433.py`) --
  5,988 bytes after the edit, the same cap #2660 was closed for exceeding.
- **1868.rest-message-swallows-paths**, **1868.rest-tail-newline-any-field** and
  **1868.rest-tail-trailing-newline-edit-replace** -- three one-off code defects in the generic
  `_load_at_file_raw`/`@rest` payload-tail parser (a following `paths = [...]` line swallowed into
  the message body; the raw trailing newline turning a `pattern = @rest` into a match-everything;
  the same newline landing verbatim in `edit`/`replace`'s `new` field). All three are parser fixes
  release-auditor flagged as "worth a maintainer glance," not agent-facing lessons -- no
  path/tool/vocabulary trigger here would change what an agent does differently, only what the
  parser does.
- **1868.stale-comment-reference** -- #1868's 2026-09-16 comment (propose folding in #2545/#2204)
  is stale relative to its own later 2026-09-19 comment, which already treats both as
  resolved/superseded. The fragment's own text says this was "not acted on as written" -- nothing
  for a rule to change, since the later comment already supersedes the earlier one on the same
  issue.
- **2645.approved-missed-behind-other-blocker** -- one-off code defect in
  `presets/watch/sources/gitlab-mr/poller.py` (the `approved` event never re-fires once the
  approvals endpoint has answered False on the one tick it is asked); a fixture/fix task for that
  poller, not an agent-facing lesson. Fixed by #2670.
- **2649.youtube-linesep-unicode-not-flattened** -- one-off code defect: the `\r`/`\n` flattening
  #2649 added does not cover U+2028/U+2029 and other Unicode line separators, and `_untrusted.flat`
  already exists unused at all three call sites; a code fix (route through the existing helper),
  not a jit rule.
- **2656.leaked-key-guard-spacing-variants** -- one-off code defect: `_leaked_key_hazard` matches
  only the canonical `paths = [`/`message = ` spacing, missing `paths=`, extra spaces, or leading
  whitespace; a regex widening plus a parametrised test, not an agent-facing lesson.
- **2657.commit-py-existing-mr-hint-same-gap** -- `presets/git/commit.py`'s post-commit MR hint has
  the identical "open lookup found nothing" gap #2657 already fixed on the push side; a wiring task
  (point it at the same `query_last_mr_result` seam), not a new lesson.
- **2657.dead-mr-lookup-matches-fork-prs** -- `query_last_mr_result`/`_dead_mr_lines`'s `gh pr list
  --head BRANCH --state all` can match a stranger's cross-repo fork PR sharing the same branch
  name; a code fix (filter `isCrossRepository`), not a jit rule -- no path/tool trigger here
  changes what an agent does differently.
- **2657.mr-lookup-state-absent-fallback** -- `_glab_last_fields`/`_gh_last_fields` fold a
  genuinely-absent `state` key into the same `or "closed"` fallback a real closed state reads as;
  flagged informational-only by oss:auditor since it requires a malformed CLI response neither
  gh's nor glab's contract produces. A three-state fix for next time the file is touched, not a
  rule to write now.
- **2658.dispatcher-transport-verdict-disagreement** -- recon note that `presets/watch/dispatcher.py`
  /`transport.py`'s radar-board delivery classifier may share the same verdict-disagreement shape
  #2658 fixed in `channel.py` (#2663); not investigated or fixed here, a different subsystem's own
  diff. An investigation task for whoever next touches that file, not an agent-facing lesson yet.
- **2658.health-description-and-statusline-lack-unproven** -- `presets/watch.json`'s `channel:health`
  description named only five states after #2658 added a sixth (`BOUND, UNPROVEN`, exit 8); fixed
  by #2675. The vendored `.oss/statusline.py` half is not this repo's to fix --
  scaffolded wholesale from the `claude-oss` plugin's own template, and remains open.
