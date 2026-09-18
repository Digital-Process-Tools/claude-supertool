---
title: "An already-tagged CHANGELOG.md section: the written rule and practice disagree"
match: (^|/)CHANGELOG\.md$
---

`docs/contributing.md`'s "Changelog fragments" section says, unconditionally, "Do not edit
`CHANGELOG.md` in a pull request. Add a file to `changelog.d/` instead." Read literally, that
also forbids fixing a factual error in a section that is already tagged and released -- and for
that case there is no `changelog.d/` file to add instead, because fragments are deleted at
release time.

Real precedent runs the other way: commits `54ae5c4a`/`d01879d7` (#2440) edited the already-tagged
v0.34.0 `#1317` entry directly, years after release, to redact a leaked username -- no fragment
was involved for that specific correction (a `changelog.d/2440.fixed.md` fragment was added, but
only for the new, unreleased work in that same pull request).

So a factual correction to an already-tagged section, made in place, matches standing precedent
even though the written rule does not carve out this case. Do not read a reviewer's flag on this
exact shape -- "you edited `CHANGELOG.md` in a PR" -- as automatically correct; check whether the
edit is new unreleased content (the rule as written) or a factual correction to a tagged, released
section (#2440's own shape). Whether such a correction needs its own marker -- a note in the entry,
or a `changelog.d/` fragment for the *next* release documenting the correction -- is not decided
anywhere; `docs/contributing.md` has not been amended to say (#2607).
