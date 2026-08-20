---
title: "The version lives in five places"
match: .claude-plugin/|pyproject.toml
---

`.claude-plugin/plugin.json`, `_supertool.py` (`VERSION`), `pyproject.toml`, `CHANGELOG.md`, and the `README.md` badge.

All five are guarded. Four by name — `test_plugin_manifest_version_matches_code`, `tests/test_pyproject_version_522.py`, `test_changelog_link_refs_918`, `test_readme_version_badge_matches_code` — and the fifth, `_supertool.py`, is what they are all pinned to. **The badge is not the unguarded one**; believing it was is what produced #1854.

Since #1854 the **set** is guarded too: `tests/test_version_sites_agree_1854.py` derives the sites from `.oss.json`'s `version_sites` and reds if one disagrees, if one cannot be read, or if the list names a site the test has no reader for. That last arm is the sixth-site case — declaring a new site now fails the suite instead of quietly gaining no guard.

```bash
git grep -n "0\.32\.0"          # no path filter, no --include
```

- **No extension allowlist.** `--include="*.py" --include="*.json" --include="*.toml"` cannot see `README.md`, which is why the badge sat at `0.14.1` for fifteen releases, hyperlinked to the file it disagreed with. Unquoted globs also let zsh expand them, and the resulting error reads exactly like "no matches".
- **Sweep both directions.** A sweep keyed on the outgoing version only finds sites that are half-bumped. It cannot see one frozen at some third value — the badge case. A zero means "nothing is half-done", never "everything is right".
- **The tag is not the delivery.** The updater compares manifest versions and nothing else, so a tag whose manifest was not bumped reaches nobody already installed.
