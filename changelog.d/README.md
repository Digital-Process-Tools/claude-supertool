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

**A fragment is bullets and prose, and the guard is a CommonMark parser.** A
fragment is inserted into `CHANGELOG.md` verbatim, so a line here that CommonMark
reads as a heading or a link-reference definition becomes one in the released
file — it reparents the entries below it, and a definition planted here sits
above the genuine link-ref block at the bottom, where the *first* definition of a
label is the one that resolves
([#923](https://github.com/Digital-Process-Tools/claude-supertool/issues/923),
[#930](https://github.com/Digital-Process-Tools/claude-supertool/issues/930),
[#934](https://github.com/Digital-Process-Tools/claude-supertool/issues/934),
[#936](https://github.com/Digital-Process-Tools/claude-supertool/issues/936)).

**A `paste` or `edit` into this directory now runs that same check** and prints
the same message, so you find out at write time instead of from a 20-leg CI run
twenty minutes later ([#1132](https://github.com/Digital-Process-Tools/claude-supertool/issues/1132)).
The receipt truncates long messages; `supertool 'validate:changelog.d/<file>:verbose'`
prints one in full.

`--check` parses your fragment with `markdown-it-py` and refuses it if the token
stream holds any of these, **at any depth**:

- a heading — ATX (`# x`), setext (a `===` or `---` underline), or a `<h1>` tag,
  and it makes no difference whether it is nested in a list, a quote or both;
- a link-reference definition, however it is spelled — split across lines, with
  an escaped bracket, lowercased, behind a `>`;
- raw HTML, block-level or inline.

It also refuses a fence that does not close inside the fragment, and a fragment
whose top level is not a single `-` bullet list — that last one is not about
safety, it is what `_entry_count` is counting when the assembler proves the cut
lost nothing. Findings name the file and the line, so they land on your PR rather
than in front of whoever is cutting the release.

**Three attempts at doing this with patterns lost, so there is no pattern.**
[#927](https://github.com/Digital-Process-Tools/claude-supertool/pull/927)
anchored at column 0 and #930 found three bypasses;
[#932](https://github.com/Digital-Process-Tools/claude-supertool/pull/932)
widened to 0-3 spaces and #934 found six more;
[#935](https://github.com/Digital-Process-Tools/claude-supertool/pull/935)
inverted to a whitelist with its own fence state machine and #936 walked through
the fence. Every one of those was the same shape — the scanner disagreed with
CommonMark — so the guard and the reader are one parser now. If a construct is
inert to a renderer it is accepted here, and if it is not, it is refused, without
either judgement being re-derived by hand.

**A bullet may open with a link.** `- [#123](url) fixed the thing.` is an
ordinary entry; so is a wrapped continuation line that begins with one. The old
rule refused a bare `[` anywhere, and an inline link can never be a
link-reference definition.

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

**Close the fence at the bullet's indent too, and this is #936's whole subject.**
A code block takes no lazy continuation, so a line that reaches column 0 inside
your fence ends the fence, the bullet and the list — and whatever you thought you
were quoting is then live at document level, exactly as if there were no fence at
all. Keep every line of the block at or beyond the bullet's indent, closer
included.

**The fence really does make its contents inert now.**
[#927](https://github.com/Digital-Process-Tools/claude-supertool/pull/927) and
[#932](https://github.com/Digital-Process-Tools/claude-supertool/pull/932) both
declined fence-awareness, on the grounds that nothing downstream understood
fences so a fence bought no safety. That was true of them and is no longer true:
the assembler finds its insertion point and its link-ref block by asking the
parser, not by matching line prefixes, so a `## [Unreleased]` inside a fence is
inert to the release for the same reason it is inert to a reader. Which is what
makes the example above the remedy rather than a second hole.

Why: 55 of the last 60 merged PRs touched `CHANGELOG.md`, every one of them at
the top of the same 2,670-line file, so each merge re-conflicted every other
open PR ([#906](https://github.com/Digital-Process-Tools/claude-supertool/issues/906)).
Two PRs never touch the same path here.

At release, `python3 .github/scripts/assemble_changelog.py --version x.y.z`
folds these into a new section of `CHANGELOG.md` and deletes them. See
`docs/contributing.md`, "Changelog fragments".
