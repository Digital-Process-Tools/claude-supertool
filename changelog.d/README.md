# changelog.d — one file per change

Do not edit `CHANGELOG.md` in a pull request. Add a file here instead:

```
changelog.d/906.added.md
changelog.d/895.fixed.md
changelog.d/878.fixed.second-entry.md
```

`<issue>.<section>[.<slug>].md`, where `<section>` is one of the Keep a
Changelog headings — `added`, `changed`, `deprecated`, `removed`, `fixed`,
`security`. The optional slug lets one issue file two entries in one section
without two PRs colliding on a path, which is the whole point of the
directory.

The **content is the entry exactly as it would appear** under that heading
today — a `- **Bold summary** ([#906](link)). Prose.` bullet, with as many
indented paragraphs after it as the change deserves. Nothing is reformatted.

Why: 55 of the last 60 merged PRs touched `CHANGELOG.md`, every one of them at
the top of the same 2,670-line file, so each merge re-conflicted every other
open PR ([#906](https://github.com/Digital-Process-Tools/claude-supertool/issues/906)).
Two PRs never touch the same path here.

At release, `python3 .github/scripts/assemble_changelog.py --version x.y.z`
folds these into a new section of `CHANGELOG.md` and deletes them. See
`docs/contributing.md`, "Changelog fragments".
