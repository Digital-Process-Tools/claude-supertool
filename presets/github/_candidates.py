#!/usr/bin/env python3
"""One line format, honoured by both ends of the candidates pipeline (#1387).

`gh-find-starable` and `gh-find-followable` write a reviewable list — one
candidate per line, with an inline `#` annotation carrying the star count or
why the account showed up. `gh-batch-star` and `gh-batch-follow` read that same
file back. Until #1387 they did not agree on what a line was: the producers
wrote `octo/tool  # 12 stars`, the consumers sent all of it as the repository
path, and the pipeline the two op pairs exist to form needed a hand edit in the
middle.

**The decision, of the three the issue laid out.** The consumers learn the
annotation; the producers keep the readable one-line format; and — the part
that makes it safe — nothing is *silently* reinterpreted.

Two rules, and the second is the one that matters:

* A `#` **preceded by whitespace** ends the name. Everything from it to the end
  of the line is an annotation and is dropped.
* A `#` with nothing but name characters before it is **not** an annotation.
  `evil#tool/x` stays whole and is then refused by `unusable()`, because
  trimming it would produce `evil`, a *different* account that may well exist.
  A consumer that silently strips what it does not understand is how a wrong
  name becomes a starred repository.

So the split can only ever remove text that no GitHub owner, repository or
login could have contained, and anything the split leaves behind that still
cannot be a name is declined by name rather than sent. The callers count both
outcomes onto their receipt: a batch op that quietly rewrites its input is this
repository's house defect with a write attached.
"""
from __future__ import annotations

#: What starts a comment, at line start or after whitespace.
COMMENT = "#"


def split_annotation(line: str) -> tuple[str, str]:
    """`(value, annotation)` — the name, and the inline `# ...` that followed it.

    The annotation is returned rather than discarded so a caller can count what
    it dropped without re-deriving the split. Both halves are stripped; the
    annotation is `""` when the line carried none.
    """
    text = str(line)
    for i, ch in enumerate(text):
        if ch == COMMENT and i > 0 and text[i - 1].isspace():
            return text[:i].strip(), text[i:].strip()
    return text.strip(), ""


def is_comment(line: str) -> bool:
    """A whole-line comment — `#` first, leading whitespace allowed."""
    return str(line).strip().startswith(COMMENT)


def unusable(value: str) -> str:
    """Why `value` cannot be a GitHub name, or `""` when it can be one.

    Deliberately not a positive charset test. GitHub's own rules for logins,
    owners and repository names are theirs to change, and an allowlist written
    here would reject a legal name on the day they widen one — refusing a name
    the user reviewed and meant is the *worse* failure of the two. What is
    asserted is only what the split guarantees: whitespace and `#` cannot
    appear in any of the three, so their presence means the line was not a
    name, and sending it would be a guess.
    """
    text = str(value)
    if not text:
        return "empty after the inline annotation was removed"
    if any(c.isspace() for c in text):
        return ("contains whitespace, so it is not one name — an inline "
                "comment must be introduced by `#`")
    if COMMENT in text:
        return ("contains '#', which no GitHub owner, repository or login "
                "may — an annotation must have whitespace before its '#'")
    return ""
