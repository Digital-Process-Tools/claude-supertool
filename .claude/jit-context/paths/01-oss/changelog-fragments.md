---
title: "Changelog fragments"
description: "One file per pull request; do not hand-edit CHANGELOG.md while changelog.d/ exists -- the fold overwrites it and deletes the fragments."
match: (changelog.d/|(^|/)CHANGELOG\.md$)
---

One file per pull request, so two open PRs never touch the same file. `CHANGELOG.md` is assembled
from these at release time and the fragments are deleted.

**Name:** `<issue>.<section>[.<slug>].md`, where the section is a Keep a Changelog heading,
lowercased: `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`. `<slug>` is optional
and lets one issue file two entries in one section without two pull requests colliding on a path.

**Body:** a single top-level `-` list. No headings, no raw HTML, no unclosed fences. Name the issue
in the text as well as the filename -- the filename is metadata, and metadata does not survive being
read out of context.

**A `removed` fragment must declare compatibility**, as one more bullet in that list:

    - Compatibility: breaking - <reason>
    - Compatibility: compatible - <reason>

`/oss:release` reads it to propose the version. A removal that declares nothing stops the proposal
and names the file, rather than being read as a quiet minor -- whether a removal breaks anything is
the question the number turns on, and an author who knows the answer and writes it as prose puts it
where nothing can read it. The reason is part of the field: a bare verdict is the same unsourced
answer one field further along. Other sections may carry the bullet and are read as compatible when
they do not.

**Do not hand-edit `CHANGELOG.md`** while this directory exists. The fold overwrites it and deletes
the fragments; an entry written directly into the file is lost at the next release, silently,
because the fold has no way to know it was meant to stay.

**The fragment checker could not be located in this repository**, so this rule
names no command. A path guessed here would fail the first time anybody ran
it, and read as this repository being wrong.

**`/oss:scaffold` will not put one here.** A changelog gate already runs in
this repository under a different name (`already present: .github/scripts/assemble_changelog.py, .github/workflows/changelog.yml`), so the owned checker was
declined rather than written on top of it -- and running `/oss:scaffold`
again declines again. **This rule does not know that gate's command.** Read
what the parentheses above name -- one file or several, and possibly a note
about part of the tree that could not be read: that is the gate this
repository actually runs.
`/oss:scaffold --force-owned` installs this plugin's checker alongside it,
after which both gates run on every pull request.
