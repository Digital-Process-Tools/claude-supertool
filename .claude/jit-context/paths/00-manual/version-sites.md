---
title: "The version lives in five places"
match: .claude-plugin/
---

`.claude-plugin/plugin.json`, `_supertool.py` (`VERSION`), `pyproject.toml`, `CHANGELOG.md`, and the `README.md` badge.

Four are guarded by tests — `test_plugin_manifest_version_matches_code`, `tests/test_pyproject_version_522.py`, `test_changelog_link_refs_918`, `test_readme_version_badge_matches_code`. Believing a site is unguarded is what justifies skipping the sweep; the sweep's real value is finding a **sixth** site no test covers. When one turns up, add its guard in the same commit.

```bash
git grep -n "0\.32\.0"          # no path filter, no --include
```

- **No extension allowlist.** `--include="*.py" --include="*.json" --include="*.toml"` cannot see `README.md`, which is why the badge sat at `0.14.1` for fifteen releases, hyperlinked to the file it disagreed with. Unquoted globs also let zsh expand them, and the resulting error reads exactly like "no matches".
- **Sweep both directions.** A sweep keyed on the outgoing version only finds sites that are half-bumped. It cannot see one frozen at some third value — the badge case. A zero means "nothing is half-done", never "everything is right".
- **The tag is not the delivery.** The updater compares manifest versions and nothing else, so a tag whose manifest was not bumped reaches nobody already installed.
