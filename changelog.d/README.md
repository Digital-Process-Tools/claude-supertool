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

**The first four columns belong to the assembler.** A fragment is inserted into
`CHANGELOG.md` verbatim, so a line here that CommonMark would read as a heading
or a link-reference definition becomes one in the released file — it reparents
the entries below it, and a definition planted here sits above the genuine
link-ref block at the bottom, where the *first* definition of a label is the one
that resolves
([#923](https://github.com/Digital-Process-Tools/claude-supertool/issues/923),
[#930](https://github.com/Digital-Process-Tools/claude-supertool/issues/930)).

Refused, therefore: a `#` heading or a `[label]:` definition in the leftmost
**four columns** — **0 to 3 leading spaces**, **any case** (`[unreleased]:` is
the same label as `[Unreleased]:`), **any label** (redefining `[docs]` from the
top of the file is the same hijack). `--check` refuses those, naming the file
and the line, so it fails on your PR rather than at release time.

To quote a heading in prose — which entries here do all the time — **indent it
by four spaces**, inside the indented paragraphs the bullet format already asks
for:

```
- **Renamed the release heading** ([#923](link)). It now reads:

    ```markdown
    ## [Unreleased]
    ```
```

**Four, not two.** At four columns CommonMark reads an indented code block, and
so does every scanner in the assembler; at one to three it reads a heading, which
is what made the old two-space advice a bypass rather than a remedy. A leading
tab is already four columns or more by CommonMark's tab-stop rule, so a
tab-indented line is allowed.

The rule is positional, not fence-aware, and that is on purpose: nothing that
reads `CHANGELOG.md` afterwards understands fences either, so an unindented
heading inside a fence corrupts the file exactly as hard as one outside it.

Why: 55 of the last 60 merged PRs touched `CHANGELOG.md`, every one of them at
the top of the same 2,670-line file, so each merge re-conflicted every other
open PR ([#906](https://github.com/Digital-Process-Tools/claude-supertool/issues/906)).
Two PRs never touch the same path here.

At release, `python3 .github/scripts/assemble_changelog.py --version x.y.z`
folds these into a new section of `CHANGELOG.md` and deletes them. See
`docs/contributing.md`, "Changelog fragments".
