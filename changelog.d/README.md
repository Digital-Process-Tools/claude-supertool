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

**A fragment is bullets and prose, and the guard is a whitelist.** A fragment is
inserted into `CHANGELOG.md` verbatim, so a line here that CommonMark reads as a
heading or a link-reference definition becomes one in the released file — it
reparents the entries below it, and a definition planted here sits above the
genuine link-ref block at the bottom, where the *first* definition of a label is
the one that resolves
([#923](https://github.com/Digital-Process-Tools/claude-supertool/issues/923),
[#930](https://github.com/Digital-Process-Tools/claude-supertool/issues/930),
[#934](https://github.com/Digital-Process-Tools/claude-supertool/issues/934)).

So the accepted shape is stated, and everything else is refused:

> One or more `- ` bullets at column 0; every other line blank or indented at
> least two spaces under one; and, fenced code blocks excepted, no line opening
> anything but a list item, a table row or ordinary prose.

Concretely, `--check` refuses a line that opens a heading (`#`, or a `===` /
`---` underline), a link-reference definition (`[label]:`, including one whose
label runs onto the next line), a blockquote (`>`) or raw HTML (`<`) — at **any**
indent, not in some leading band of columns — and it refuses a line that is
neither a bullet nor indented under one, tabs included. It names the file and
the line, so it fails on your PR rather than at release time.

**To quote a heading in prose — which entries here do all the time — put it in a
fenced code block at the bullet's own indent:**

```
- **Renamed the release heading** ([#923](link)). It now reads:

  ```markdown
  ## [Unreleased]
  ```
```

**A fence, not an indent, and this is the part that was wrong before.** This file
used to say "indent it by four spaces". CommonMark's four-column code-block
threshold is relative to the containing block's content column, and a `- `
bullet's content column is 2 — so inside a fragment, four spaces is *two*
relative columns: an ordinary paragraph, in which a heading is a live heading and
a link-reference definition resolves. Rendered through a real CommonMark parser
inside a bullet, a definition is live at 2, 4, 5 and tab indent; the threshold is
6. Following the old advice produced the injection the guard had just refused.
The earlier worked example was safe only because it happened to include a fence.

**The rule is fence-aware, and that is a reversal** ([#927](https://github.com/Digital-Process-Tools/claude-supertool/pull/927),
[#932](https://github.com/Digital-Process-Tools/claude-supertool/pull/932) both
declined it). Their reason was that nothing downstream understands fences, so a
fence bought no safety — an argument about the assembler's own column-scoped
scanners. The whitelist keeps those safe by position instead: no body line ever
reaches column 0 except a `- ` bullet. That leaves the fence free to do what it
actually does for a reader's parser, which is make its contents inert — so a
`# comment` inside a ```bash block is an ordinary entry, not a finding.

Why: 55 of the last 60 merged PRs touched `CHANGELOG.md`, every one of them at
the top of the same 2,670-line file, so each merge re-conflicted every other
open PR ([#906](https://github.com/Digital-Process-Tools/claude-supertool/issues/906)).
Two PRs never touch the same path here.

At release, `python3 .github/scripts/assemble_changelog.py --version x.y.z`
folds these into a new section of `CHANGELOG.md` and deletes them. See
`docs/contributing.md`, "Changelog fragments".
