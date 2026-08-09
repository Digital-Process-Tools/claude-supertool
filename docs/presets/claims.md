# `claims` — does this document's references still hold?

One op, read-only, no writes anywhere:

| Op | Syntax |
| --- | --- |
| `claims` | `claims:PATH` |

```
supertool 'claims:.claude/jit-context/paths/00-manual/presets-github.md'
```

```
.claude/jit-context/paths/00-manual/presets-github.md: holds 9 | contradicted 3 | couldn't check 1

CONTRADICTED
  L27    issue    #1181 — listed under an open-defects heading, but the tracker says CLOSED
  L28    issue    #1207 — listed under an open-defects heading, but the tracker says CLOSED
  L29    issue    #1180 — listed under an open-defects heading, but the tracker says CLOSED

COULDN'T CHECK
  L38    op       repo — resolves to no op in this project's registry; ...
```

## Why it exists

`.claude/skills/opensource-manager/SKILL.md` told the maintainer, in a bullet
headed **"Two genuine gaps"**, that no op rendered a commit's run list and that
nothing tallied label distribution. Both had shipped — `gh-branch:COMMIT_SHA`
(#1083) and `gh-labels:tally=PREFIX` (#1084) — and the second is the cohort
burn-down the same file orders the maintainer to report every tick. So the file
was prescribing hand-rolled jq for a one-call op. The same file records the same
failure happening before, with `repo:`.

A document that is *loaded* rather than *read* does not merely risk being
wrong. It **produces the behaviour it describes**, because the reader obeys it.
`.claude/jit-context/*.md` is the sharpest case: it is injected at the moment of
a tool call, so a stale line there arrives with more authority than a doc
somebody chose to open. On the day this op was written, that directory listed
five issues under "Open defects" headings and all five were closed.

## What it checks, and what it refuses to

**References, never reasoning.** That boundary is the whole design and it was
bought with a measurement rather than argued.

A probe that flagged `#NNNN` citations by *issue state* plus an absence-marker
word list (`no op`, `there is no`, `nothing <verb>s`, `cannot`, `missing`,
`filed as`) scored **15 flagged, 2 real — 13% precision**. Every false positive
had the same shape: past-tense narration of a bug that was fixed is lexically
identical to a present-tense claim that a hole exists.

```
L96   "#429 was filed as two copies and was seven call sites"   history, correct forever
L266  "surfaced #498, a crash on master no test covered"        history, correct forever
L295  "a checker that cannot answer must say so"                the rule, not a gap
L351  "#1083, no op renders a commit's run list"                live, and wrong
```

Three narrower lexical anchors were measured against this repository's whole
documentation corpus while building the op, each an attempt to rescue the
sentence-level check:

| Anchor | Flagged | Real | Precision |
| --- | --- | --- | --- |
| absence marker + the sentence names an op that resolves | 7 | 1 | 14% |
| ... + the op's signature contains a word denied after the negation | 9 | 1 | 11% |
| ... + that word is a *rare* placeholder of that op's own signature | 5 | 1 | 20% |

None beat the 13% that had already been rejected. **So there is no lexical lens
in this op at all.** A sentence is never a finding, and the render says so in
its footer. Emitting plausible findings at that precision would be this
repository's house defect one layer up: an absence produced by the tool, read
as an absence in the world.

## The three lenses

### `op` — names and named flags

A backticked `name:...` token, resolved against the live registry
(`.supertool.json` plus every `presets/*.json` beside it), keyed by the head of
each `syntax` string and merged, so a second entry for the same op counts.

Only `key=` flags are checked. A bare segment sits in a placeholder slot and is
a value: a membership test flagged `gh-pr:master:status`, `gh-branch:master`
and `around:localhost:/etc/hosts:1`, all wrong.

A head that resolves to nothing is **couldn't check**, never contradicted. 19
such tokens in this repo's docs were skill ids (`code-review:code-review`),
label filters (`priority:high`) and other tools' namespaces; not one was a
stale op name. Field notation (`ok:true`, `code:"adapter"`) and anything with
whitespace after the colon (`status: in_progress`) is not an op reference.

### `path` — files, line numbers, quoted lines, headings

* Path with a `/` that resolves → `holds`; with `:LINE` beyond the end of the
  file → `contradicted`, naming the real length.
* Missing, but its directory exists → `contradicted`. Missing with the
  directory missing too → **couldn't check**: `hooks.d/after_save/50-git-backup.sh`
  is `claude-remember`'s, and answering for it would be this repo inventing a
  verdict about another one.
* A bare basename resolves if it is unique in the tree, or sits at the
  repository root; zero or several matches are **couldn't check**.
* A path whose components read as placeholders — `changelog.d/NNN.section.md` —
  is a naming convention being described, not a file being cited.
* A path introduced as a counter-example — as in `presets/mytools/status.py`, **not** `scripts/status.py` — is skipped. The negation has to sit on the same line as the path it disowns; a line break between them puts the path back in scope, which is how this very bullet reported itself.
* **A quotation beside a line number is checked against that line.** A line
  number alone only proves the file is long enough: `docs/validators.md:650` in
  a JIT file was inside a 1031-line file and read as holding while the sentence
  it quoted had moved to line 681. With no line number, a quotation after a
  `.md` path is checked as a section heading instead.

### `issue` — only under a heading that declares an open defect

`# Open defects`, `# Open defect #1202 — ...`. Every `#NNNN` in that block, down
to the next heading of the same or higher level, must be OPEN.

The match is **anchored to the start of the heading text**, because a heading
that *mentions* open defects is not a heading that declares a list of them. An
unanchored match turned the section you are reading into a defect list and
reported the example number above as a live stale citation.

This is the narrow third rule, and it carries a verdict where prose cannot for
one reason: **it reads the document's own structural annotation instead of
guessing at its grammar.** The heading is the author saying "these are live
gaps". Checking that claim needs no semantics.

Cross-repository citations are **couldn't check**, both the attached
`owner/name#N` form and a line naming a sibling project. The sibling's family
prefix is derived from this repository's own name (`claude-supertool` →
`claude-`); with no repository resolved, the rule declines rather than guessing
what "another project" would mean. Prose slashes are deliberately *not* scanned
for repository names — a general `owner/name` sweep read
`dependabot/outside-contributor` and `presets/github` as other repositories and
demoted two real contradictions to "couldn't check". A third state that eats
findings is the same defect as a third state that never fires.

## Three states, and the third is load-bearing

`holds` / `contradicted` / `couldn't check`. A document with nothing
contradicted but something unchecked prints

```
NOT A CLEAN DOC: 1 reference(s) could not be checked — an unread reference is not a verified one.
```

because a doc that could not be checked must not render as a checked doc
(`docs/validators.md`, "Declining instead of guessing"). If there is no
`.supertool.json`, the op lens says it did not run rather than reporting every
op reference as unresolved. If the tracker cannot be read, each citation says
so and names the reason; it never falls back to "open".

## Measured yield

Over 22 documents in this repository — all of `.claude/jit-context/`, `CLAUDE.md`,
both agent files, `SKILL.md`, `README.md`, `docs/contributing.md`,
`docs/validators.md`, `docs/presets/index.md`:

**11 contradicted, 11 real.** Five closed issues under open-defects headings,
four `gh-issues:per=` sites where the flag works and the signature omits it, one
line number whose quoted sentence had moved, one path that no longer exists.

Zero of the maintainer's original 15 are flagged — the op declines them.

## Scope

Fenced code is skipped entirely; an unclosed fence swallows the rest of the
file rather than guessing where it ended. Nothing is written, nothing is
mutated, and the op never reaches outside the repository root.

It reads the tracker only for citations under an open-defects heading, so a
document without one costs no network at all.
