# Releasing

`master` carries everything: the plugin, its 1,200+ test files, the docs, the
maintainer tooling, and a `CHANGELOG.md` over 2.5 MB. The Anthropic plugin directory
does not accept that. Its pre-submission checklist holds any version whose plugin
folder has more than 512 files, or a file of 256 KiB or more that is not an image or
font. Measured 2026-10-02 on `master`: **1,726 files, 24.6 MB** -- the directory
scanned it and stopped with `VALIDATION_INCOMPLETE` / `VALIDATION_NOT_EVALUATED`,
both policy holds ("Validation ran out of time"). Ported from
[claude-remember#851](https://github.com/Digital-Process-Tools/claude-remember/issues/851)
as [#2705](https://github.com/Digital-Process-Tools/claude-supertool/issues/2705).

So there are two things people can install from:

| Branch | Who reads it | What decides the version they get |
| --- | --- | --- |
| `master` | the DPT marketplace, manual installs | `version` in `.claude-plugin/plugin.json` on `master` |
| `release` | the Anthropic directory, once its listing's "Tracked branch or tag" is pointed at it (a human step, not done by this workflow) | the latest commit on `release`, which only the release workflow writes |

`release` is built by
[`.github/workflows/release-branch.yml`](../.github/workflows/release-branch.yml) from
a tag. It never shares history with `master`: each release is one commit on top of
the previous release commit, and its message names the tag and the `master` commit it
came from.

## The sequence

1. **Fold `changelog.d/` into a `## [x.y.z]` section of `CHANGELOG.md` and bump every
   version site** (`.oss.json`'s `version_sites`: `.claude-plugin/plugin.json`,
   `_supertool.py`, `pyproject.toml`, `CHANGELOG.md`, `README.md`).
2. **Run the test suite and commit the release on `master`.**
3. **Tag that commit `vx.y.z` and push the tag with your own credentials**:
   `git tag vx.y.z <release-commit-sha> && git push origin vx.y.z`. A tag pushed by
   another workflow with `GITHUB_TOKEN` does not start workflows (GitHub suppresses
   them to prevent loops), so this has to be a person's (or the release flow's own)
   push.
4. **Watch the `release branch` run** (`gh run list --workflow release-branch.yml`).
   Two jobs:
   - `verify`, read-only: installs PyYAML, builds the tree from the tag, runs
     [`check_release_tree.py`](../.github/scripts/check_release_tree.py) (the
     directory's pre-submission checklist), installs the pinned `claude` CLI
     (`CLAUDE_CLI_VERSION` in the workflow) and runs `claude plugin validate --strict`,
     then runs every hook in `hooks/hooks.json` once in an isolated temp HOME and
     project, with fake `claude`/`codex` binaries that refuse every call
     ([`smoke_release_tree.py`](../.github/scripts/smoke_release_tree.py)). If the npm
     install of the CLI fails, the run does not fail -- validate is SKIPPED and the
     only trace is a `::warning::` annotation, so read the run rather than its status;
   - `publish`, the only job allowed to write: rebuilds the same tree, refuses to push
     unless it is byte-for-byte the tree `verify` passed, and pushes one commit to
     `release`.

   Two jobs rather than one so the smoke test -- which runs the plugin's own hooks --
   never holds a token that can push.
5. **Publish the GitHub release** (the maintainer loop's `scripts/release_publish.py`,
   reading `CHANGELOG.md` from the local `master` checkout -- never from the release
   tree, whose `CHANGELOG.md` is cut to the latest section only).
6. **The directory picks up the new `release` commit** once the listing's tracked
   branch is pointed at it, through the push webhook or a periodic scan. A version
   with a policy hold waits for an Anthropic reviewer.

## What the release tree contains

[`build_release_tree.py`](../.github/scripts/build_release_tree.py) reads the tag
straight from git (`git ls-tree` and `git cat-file`; never the working tree, and never
`git archive`, which would need `export-ignore`). Then:

- **It drops the deny-list** in
  [`.github/release-branch.json`](../.github/release-branch.json): `tests/`, `docs/`,
  `.github/`, `.githooks/`, `.claude/`, `.oss/`, `changelog.d/`, `outbound/`,
  `trap.d/`, `.editorconfig`, `.markdownlint.json`, `.oss.json`, `.supertool.json`,
  `.supertool.example.json`, `CLAUDE.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`,
  `SECURITY.md`, `pyproject.toml`. It is a deny-list on purpose: a path nobody listed
  still ships, and the check catches it loudly if it is too big. With an allow-list, a
  forgotten runtime file would vanish from every user's install with no error
  anywhere. A new dev-only top-level file or directory needs adding here.

  **Not denied, on purpose, even though they look like dev config:**
  `_shipped_reference.py` is the fallback `_shipped_config()` reads once
  `.supertool.json` is absent beside `_supertool.py` (the pip-install route, #1783) --
  and this release tree denies `.supertool.json`, so the fallback is load-bearing
  here. `.mcp.json` registers the `claude-channel` notifier the plugin manifest
  declares.
- **It cuts `CHANGELOG.md`** to the latest released `## [x.y.z]` section, skipping
  `[Unreleased]` even when it has entries, plus that section's link and a link to the
  full file on `master`.
- **It rewrites links** in every shipped `.md` file that point at a removed path
  (README's `docs/` links) to absolute URLs on `master`: `raw.githubusercontent.com`
  for images, `github.com/.../blob/master` for everything else. Links to files that
  still ship are left alone.

Built from `v0.64.0` (2026-10-02): **336 files, 6.5 MB**, down from 1,726 files and
24.6 MB.

### The `_supertool.py` exception

`_supertool.py` is 1.7 MB -- the tool itself, so it cannot be denied. It is a
declared, issue-referenced exception in `.github/release-branch.json`'s
`exceptions` list, naming [#2706](https://github.com/Digital-Process-Tools/claude-supertool/issues/2706)
(splitting the file). `check_release_tree.py` reports a declared exception as a
`REVIEW` line rather than a `FAIL`:

```
REVIEW _supertool.py: 1740853 bytes, over the 256 KiB limit for non-image files -- declared exception (#2706): the tool itself, ...
```

Any other file over `max_file_bytes` still fails. Declaring a path here is not a
second deny-list; it is a visible, tracked exception for exactly one file.

## When the workflow fails

Nothing is pushed. `release` stays on the previous release, so the directory keeps
serving that, and people on `master` are not affected at all. Fix the cause on
`master`, then run the workflow again for the same tag: **Actions, `release branch`,
Run workflow, ref = `vx.y.z`**, or

```bash
gh workflow run release-branch.yml -f ref=vx.y.z
```

A manual run takes the workflow, the scripts and `.github/release-branch.json` from
the branch it is run on (normally `master`) and the plugin files from the ref named.
Re-running the same tag when `release` already holds that exact tree pushes nothing.

## Building and checking locally

```bash
python3 .github/scripts/build_release_tree.py --ref vX.Y.Z --out /tmp/release-tree   # the latest tag
python3 .github/scripts/check_release_tree.py /tmp/release-tree
python3 .github/scripts/smoke_release_tree.py /tmp/release-tree   # needs bash; validate needs `claude`
```

`check_release_tree.py` prints `REVIEW` lines both for the declared `_supertool.py`
exception and for code that reads a credential from the environment or a config file
(`presets/_secrets.py`, the `devto`/`hashnode`/`slack`/`youtube` presets' own API-key
handling, and so on). Neither ever fails the check. The credential lines are a
starting list for what `README.md` should disclose, not a prediction of what the
portal will flag.

## Reusing this in another repository

Ported wholesale from claude-remember (#851); see that repository's own
`docs/releasing.md` for the fuller write-up of what is repository-specific
(`build_release_tree.py` reads `repo`, `default_branch`, `deny`, `changelog`,
`rewrite_links` and the optional `link_ref` from the config; `check_release_tree.py`
reads `budget` and `exceptions`; `smoke_release_tree.py` reads its hook list from
`hooks/hooks.json` but the fake-binary env var names
(`SUPERTOOL_SMOKE_CLAUDE_BIN`/`SUPERTOOL_SMOKE_CODEX_BIN` here) are per-repository).
