# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file carries only the latest release. The full history is in [CHANGELOG.md on the default branch](https://github.com/Digital-Process-Tools/claude-supertool/blob/master/CHANGELOG.md).

## [0.65.1] - 2026-10-03

### Fixed

- Carved out the two files `hooks/shipped_rules.py` reads at runtime (`.claude/jit-context/tools/00-manual/00-index.tsv` and `supertool-no-cut.md`) from the `release` branch's `.claude/` deny-list, and made `smoke_release_tree.py` fail a build where the shipped jit-context guard rule loads nowhere -- previously a release install shipped that rule disabled, silently (#2729).

[0.65.1]: https://github.com/Digital-Process-Tools/claude-supertool/releases/tag/v0.65.1
