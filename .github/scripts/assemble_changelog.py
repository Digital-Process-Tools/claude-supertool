#!/usr/bin/env python3
"""Assemble `changelog.d/` fragments into a release section of `CHANGELOG.md` (#906).

55 of the last 60 merged PRs touched `CHANGELOG.md`, all of them appending to the
same place. With N open PRs each merge re-conflicts the other N-1. One file per
change removes the shared path, so the conflict class disappears rather than
being merged around — `merge=union` is not available here, because
`tests/test_git_resolve_heading_dup_839.py` demonstrates it silently reparents
unreleased work under a tagged release.

**Three states, never two.** This script can `ok`, it can produce a `finding`
(refused, naming the file), and it can `skip` — and it says which, every run.
An assembler that finds no fragments and exits 0 has reported "released" when
what happened is "nothing to release", which is the defect class this tracker
is full of: an absence produced by a tool read as an absence in the world.

Stdlib plus `markdown-it-py`, which is the one dependency and is the point
(#936). `towncrier` and `scriv` both solve the assembling half and both are
dependencies; this repo still ships one file and no install step, and nothing
a user installs imports this — it is a repo-internal release tool.

    python3 .github/scripts/assemble_changelog.py --version 0.24.0
    python3 .github/scripts/assemble_changelog.py --check     # CI: names *and* bodies
    python3 .github/scripts/assemble_changelog.py --count     # exact fragment count

Exit codes: 0 ok, 1 skipped (nothing to do, or nothing *provable* — stated
either way), 2 refused (a finding).
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import (AbstractSet, Dict, Iterator, List, Optional, Sequence, Set,
                    Tuple)

try:
    import markdown_it as _markdown_it
    from markdown_it import MarkdownIt as _MarkdownIt
except Exception as _import_error:  # pragma: no cover - exercised by monkeypatch
    _markdown_it = None
    _MarkdownIt = None
    _MD_IMPORT_ERROR = "{0}: {1}".format(type(_import_error).__name__, _import_error)
else:
    _MD_IMPORT_ERROR = None

_MD_VERSION = getattr(_markdown_it, "__version__", "unknown")

REPO = Path(__file__).resolve().parents[2]

#: Keep a Changelog 1.1.0, in the order the spec lists them. The order is data,
#: not a sort: "Added" before "Fixed" is a convention readers rely on, and
#: alphabetical would put Security second.
SECTIONS = ("added", "changed", "deprecated", "removed", "fixed", "security")

#: `<issue>.<section>[.<slug>].md`. The slug exists so one issue can file two
#: entries in one section without the two PRs colliding on a path again.
#: `\Z` and not `$`. A POSIX filename may end in a newline, and `$` matched
#: before one — so `1188.fixed.md\n` parsed as a fragment for issue 1188, got
#: folded into the release and then deleted as consumed (#1188).
_NAME_RE = re.compile(r"^(\d+)\.([a-z]+)(?:\.([A-Za-z0-9][A-Za-z0-9._-]*))?\.md\Z")

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\Z")  # \Z, not $ — #1188

#: Not fragments, and not mistakes either — refusing these would make the
#: directory unable to document itself.
_IGNORED = {"README.md", ".gitkeep", ".gitignore"}

_UNRELEASED_LINK_RE = re.compile(  # anchored-ok: matched per line of CHANGELOG.md; the newline is the delimiter
    r"^\[Unreleased\]:\s*(?P<base>\S+?)/compare/v(?P<prev>[0-9][^.\s]*(?:\.[^.\s]+)*)\.\.\.HEAD\s*$"
)

#: Any link-reference definition, used to find the block of them at the bottom.
#: 0-3 leading spaces, like everything else CommonMark calls a link ref (#930).
#: This pattern decides where the trailing block *starts*, and anchored at column
#: 0 it stopped its backward walk at an indented definition — truncating the block
#: or missing it entirely, so the release advanced no link at all and shipped a
#: `## [x.y.z]` heading whose link resolves to nothing, under a receipt that said
#: only "no compare line found". Recognising an indented line as part of the block
#: is not permission to write it: `_UNRELEASED_LINK_RE` stays anchored at column
#: 0, which is where the assembler's own line is, so an indented look-alike inside
#: the block cannot capture the rewrite.
_LINK_REF_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*\S")

#: The guard and the reader are the same parser now (#936).
#:
#: Three rounds of hand-written Markdown scanning produced three bypasses, and
#: each fix opened the next hole. #927 anchored its patterns at column 0 and
#: #930 found three ways past. #932 widened them to `^ {0,3}` and made labels
#: case-insensitive, and the next audit found six more plus a false refusal
#: plus a prescribed remedy that was itself an injection. #934 inverted to a
#: whitelist resting on a positional guarantee and its own fence state
#: machine, and #936 walked straight through the fence: a column-0 line inside
#: an open fence was `continue`d with no indent check and no opener check, so
#: `# INJECTED HEADING` and `[Unreleased]: https://evil.example/pwned` were
#: copied verbatim into the released file under a receipt that said `ok`.
#:
#: Every one of those is the same shape — **our scanner disagreed with
#: CommonMark.** Column 0 versus 0-3 leading spaces; ATX versus setext; our
#: fence state machine versus the real one; our info-string handling versus
#: the spec's, which forbids a backtick inside a backtick fence's info string,
#: so ``` `x` is an ordinary paragraph to a reader and was an open fence here.
#: That race is not winnable by patching patterns, and the fourth attempt at
#: patterns would have lost it the same way, so this stops running it.
#:
#: `markdown-it-py` is a CommonMark reference implementation and was already
#: this file's test oracle. It is the guard itself now. The guard and the
#: reader agree by construction, which is the only property that closes the
#: class rather than the instance.
#:
#: **What the guard establishes**, which is also everything it claims: parsed
#: as CommonMark, the fragment produces no heading, no link-reference
#: definition and no raw HTML at any depth; every fence it opens closes inside
#: it; and its top level is one `-` bullet list, which is what `_entry_count`
#: is counting when the balance guard proves nothing was lost.
#:
#: **What it does not establish** is that the released file is sound, because
#: a fragment is validated alone and inserted into a document. So the write is
#: verified separately, against the assembled text — see `_verify_written`.
#: One guard has now been wrong three times; the second layer is what makes
#: the fourth time survivable.
_BULLET = "- "

#: Token types that restructure a document, at any depth. `heading_open`
#: covers ATX and setext alike because the parser has already resolved which
#: is which. `html_inline` is here because `<h1>` mid-paragraph is not an
#: `html_block` and renders the same heading — the previous guard refused a
#: line *starting* with `<` and said so in its message, and put the same tag
#: after a word to sail past. Link-reference definitions are not tokens at
#: all; they are collected into the parse environment, which is checked
#: alongside these.
_REFUSABLE = {
    "heading_open": "a Markdown heading",
    "html_block": "a raw HTML block, which renders as a heading without being one",
    "html_inline": "raw HTML inside a paragraph, which renders a heading tag",
}

_SHAPE = ("a fragment is `- ` bullets at column 0 plus lines indented under "
          "them, and parsed as CommonMark it may hold no heading, no link ref "
          "definition and no raw HTML at any depth")

_REMEDY = ("To show one in an entry, put it in a fenced code block at the "
           "bullet's own indent (```), which is what every fenced example in "
           "CHANGELOG.md already does — and close the fence at that same "
           "indent, because a line reaching column 0 ends the bullet, the "
           "fence and the list, whatever the fence was meant to be hiding "
           "(#936). Indenting further is not a remedy: inside a `- ` bullet an "
           "indented line is still a live heading and a live definition, which "
           "is what the advice this message used to give got wrong (#934).")

#: Said in full wherever a run cannot validate, because the alternative is a
#: receipt with nothing behind it — which is the thing this file exists to
#: stop being possible.
_NO_PARSER = (
    "markdown-it-py is not importable ({0}), so nothing can be established "
    "about these fragments and nothing is claimed. Install it — "
    "`pip install markdown-it-py`, or `pip install -e .[dev]` — and run again. "
    "There is deliberately no text-scanning fallback: three of them shipped "
    "and all three were bypassed within one audit (#930, #934, #936), so a "
    "fallback here would be the same bug wearing a receipt.")


class CannotValidate(Exception):
    """The tool cannot answer. Not a finding, and emphatically not an `ok`."""


def _parser():
    if _MD_IMPORT_ERROR is not None or _MarkdownIt is None:
        raise CannotValidate(_NO_PARSER.format(_MD_IMPORT_ERROR or "unavailable"))
    return _MarkdownIt("commonmark")


def _flatten(tokens: Sequence, line: Optional[int] = None) -> Iterator[Tuple[object, int]]:
    """Every token in document order, each with the nearest line it maps to.

    Inline tokens carry no map of their own, so they inherit their block's.
    A finding without a line number sends the author hunting, and the author
    is the person standing in CI when this fires.
    """
    for token in tokens:
        at = token.map[0] if token.map else line
        yield token, (at if at is not None else 0)
        if token.children:
            for pair in _flatten(token.children, at):
                yield pair


def _finding(name: str, number: int, what: str, line: str) -> str:
    """One refusal, naming the file, the line number, the shape and the remedy."""
    return ("{0}:{1}: {2} — {3}. Inserted verbatim into CHANGELOG.md, this line "
            "becomes one. {4} Line: {5}"
            .format(name, number, what, _SHAPE, _REMEDY, line.strip()[:120]))


def _line_of_reference(md, lines: Sequence[str], label: str) -> int:
    """The first line at which `label` becomes a definition, per the parser.

    Bisecting the parse rather than matching a pattern: a definition's label
    may run across lines and may carry escaped brackets, and every regex this
    file has owned for that shape has been wrong. Fragments are a handful of
    lines, so the cost of re-parsing prefixes is not worth a cleverer answer.
    """
    for count in range(1, len(lines) + 1):
        env: Dict = {}
        md.parse("\n".join(lines[:count]) + "\n", env)
        if label not in env.get("references", {}):
            continue
        # `count` is where it *ends*. Its own first line is the largest start
        # whose slice still defines the label, so a definition split across
        # lines is reported where the author began writing it rather than
        # where the parser happened to finish reading it.
        for start in range(count, 0, -1):
            env = {}
            md.parse("\n".join(lines[start - 1:count]) + "\n", env)
            if label in env.get("references", {}):
                return start
        return count
    return 1


def _fence_is_closed(lines: Sequence[str], token) -> bool:
    """Whether a fence token's own last line is its closer.

    markdown-it closes an unterminated fence at the end of its container and
    reports no error, so a fence that runs on is indistinguishable from one
    that closed unless the source is consulted. A one-line fence never closed;
    otherwise the last line of the token's span has to be a bare run of the
    opening character, at least as long as the opener.
    """
    if not token.map or token.map[1] - token.map[0] < 2:
        return False
    closer = lines[token.map[1] - 1].strip()
    marker = (token.markup or "`")[0]
    return bool(closer) and set(closer) == {marker} and len(closer) >= len(token.markup)


def _structure_findings(name: str, lines: Sequence[str], tokens: Sequence) -> List[str]:
    """The shape rule, derived from the parse instead of from line prefixes.

    `_entry_count` counts lines beginning `- ` and the balance guard trusts
    that count to prove the cut lost nothing. So the top level has to be one
    `-` bullet list whose items start at column 0, or the arithmetic and the
    document disagree and a lossy cut reports as a clean one. Asking the
    parser rather than the first two characters is what now catches an ordered
    list and a bare table, which the prefix test waved through.
    """
    findings: List[str] = []
    # `nesting >= 0` is openers *and* leaf blocks. A fenced code block is a
    # leaf — `nesting == 0`, no closing token — so counting openers alone
    # counted a column-0 fence as no block at all, which is the shape #923
    # named as the likely accidental trigger.
    top = [t for t in tokens if t.level == 0 and t.nesting >= 0]
    if len(top) != 1 or top[0].type != "bullet_list_open":
        at = top[1].map[0] + 1 if len(top) > 1 and top[1].map else 1
        return [_finding(name, at,
                         "a fragment whose top level is not a single `- ` bullet list",
                         lines[at - 1] if at <= len(lines) else "")]
    if top[0].markup != "-":
        return [_finding(name, (top[0].map[0] + 1) if top[0].map else 1,
                         "a list marked `{0}`, which `_entry_count` does not count"
                         .format(top[0].markup),
                         lines[top[0].map[0]] if top[0].map else "")]
    for token in tokens:
        if token.type == "list_item_open" and token.level == 1 and token.map:
            if not lines[token.map[0]].startswith(_BULLET):
                findings.append(_finding(
                    name, token.map[0] + 1,
                    "a top-level list item that does not begin `- `",
                    lines[token.map[0]]))
    return findings


def scan_fragment_body(name: str, text: str) -> List[str]:
    """Findings for one fragment's content, each naming the file and the line.

    Raises `CannotValidate` when the parser is absent. It does not return an
    empty list in that case: an empty list means "looked, found nothing", and
    conflating that with "did not look" is the defect this tracker is full of.
    """
    md = _parser()
    lines = text.splitlines()
    env: Dict = {}
    tokens = md.parse(text, env)

    findings = _structure_findings(name, lines, tokens)

    if "\t" in text:
        at = next(i for i, line in enumerate(lines) if "\t" in line)
        findings.append(_finding(
            name, at + 1,
            "a tab, which the shipped CHANGELOG.md contains none of and which "
            "reaches a different column in every renderer",
            lines[at]))

    for token, at in _flatten(tokens):
        what = _REFUSABLE.get(token.type)
        if what is not None:
            findings.append(_finding(name, at + 1, what,
                                     lines[at] if at < len(lines) else ""))
        elif token.type == "fence" and not _fence_is_closed(lines, token):
            findings.append(_finding(
                name, at + 1,
                "a fenced code block that is never closed at the indent it "
                "opened, which swallows what follows it in CHANGELOG.md",
                lines[at] if at < len(lines) else ""))

    for label in env.get("references", {}):
        at = _line_of_reference(md, lines, label)
        findings.append(_finding(
            name, at,
            "a link ref definition of `[{0}]` — the first definition of a "
            "label is the one that resolves, and a fragment lands above the "
            "genuine block at the bottom of the file".format(label),
            lines[at - 1] if at <= len(lines) else ""))

    return sorted(set(findings), key=findings.index)

OK, SKIPPED, REFUSED = 0, 1, 2


class BadFragment(Exception):
    """A fragment this script will not guess about. The message names the file."""


@dataclass(frozen=True)
class Fragment:
    issue: int
    section: str
    slug: str
    path: Optional[Path] = None

    @property
    def sort_key(self) -> Tuple[int, int, str]:
        return (SECTIONS.index(self.section), self.issue, self.slug)


def parse_fragment_name(name: str) -> Fragment:
    """Parse a fragment filename, or refuse by name.

    Refusing rather than skipping is the whole point: a file the release tool
    silently passed over is an entry that never ships and that nobody is told
    about.
    """
    match = _NAME_RE.match(name)
    if not match:
        raise BadFragment(
            f"{name}: filename does not parse as <issue>.<section>[.<slug>].md "
            f"(e.g. 906.added.md, 878.fixed.second-entry.md)"
        )
    section = match.group(2)
    if section not in SECTIONS:
        raise BadFragment(
            f"{name}: unknown section {section!r} — expected one of: {', '.join(SECTIONS)}"
        )
    return Fragment(issue=int(match.group(1)), section=section, slug=match.group(3) or "")


#: How a body may name its own issue: `#1192`, or a tracker URL ending in the
#: number. Both are forms an author writes on purpose. A bare `1192` is not —
#: v0.32.0's #1130 fragment was findable only because it happened to cite
#: `tests/test_preset_git_splitlines_register_1130.py`, which no reader would
#: aim at and no author could be told to produce.
_SELF_REF = r"(?:#|/(?:issues|pull)/){0}(?![0-9])"


def self_reference_finding(name: str, text: str) -> Optional[str]:
    """One finding if the body never names the issue in its own filename (#1251).

    `changelog.d/<issue>.<section>.md` holds the number in exactly one
    structural place, and assembly writes the *body* and deletes the file. So
    the number survives the release only when the author typed it into the
    prose, which made findability a property of author habit: measured on the
    fragments as they stood at each release commit, **8 of 20 entries in
    v0.32.0** and **6 of 28 in v0.33.0** named every issue but their own, and
    only two of the twenty had a `test_the_change_is_findable` to say so.

    Refusing here rather than appending a reference during assembly is a
    choice about where the rule lives. An append needs an "is it already
    there?" test, and that test cannot tell a self-citation from a coincidence
    — v0.32.0's #1197 was findable only because a *different* fragment in the
    same release mentioned it. Refusing costs the author one `(#N)` in a PR
    instead of a release-time repair across thirteen legs, and it is the form
    `changelog.d/README.md` has documented all along.

    Returns `None` for a name that does not parse: `collect` already reports
    that from `parse_fragment_name`, and a second complaint about one file
    would give the write-time validator a different count from `--check`.
    """
    try:
        issue = parse_fragment_name(name).issue
    except BadFragment:
        return None
    number = str(issue)
    if re.search(_SELF_REF.format(number), text):
        return None
    lines = text.splitlines()
    at = next((i + 1 for i, line in enumerate(lines) if line.strip()), 1)
    return (
        "{0}:{1}: the entry never names #{2} — the issue number is in the "
        "filename, and the release consumes the file, so nothing carries it "
        "into CHANGELOG.md. Write `(#{2})` into the entry, the way "
        "changelog.d/README.md's example does; a link to the issue counts "
        "too. 8 of 20 entries in v0.32.0 and 6 of 28 in v0.33.0 shipped "
        "naming every issue but their own (#1251). Line: {3}"
        .format(name, at, number, lines[at - 1] if at <= len(lines) else ""))


def collect(directory: Path) -> List[Fragment]:
    """Every fragment in `directory`, sorted deterministically.

    All findings are gathered before raising: a release cut is a one-shot
    operation and reporting one bad name per run turns it into a queue.
    """
    if not directory.is_dir():
        raise BadFragment(f"{directory}: fragment directory does not exist")

    fragments: List[Fragment] = []
    findings: List[str] = []
    for path in sorted(directory.iterdir()):
        if path.is_dir() or path.name in _IGNORED or path.name.startswith("."):
            continue
        try:
            frag = parse_fragment_name(path.name)
        except BadFragment as exc:
            findings.append(str(exc))
            continue
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            findings.append(f"{path.name}: fragment is empty — an entry nobody would ever read")
            continue
        # Ahead of the body scan, which is the arm that needs `markdown-it-py`:
        # this finding needs no parser, and a definite refusal must not be lost
        # behind a `CannotValidate` raised by the check after it. It does not
        # `continue`, though — preempting the shape scan would answer a
        # malformed fragment with a note about its issue number and say nothing
        # about the malformation, which is one round-trip per finding for the
        # author and the reason `collect` gathers rather than stopping.
        self_ref = self_reference_finding(path.name, text)
        if self_ref is not None:
            findings.append(self_ref)
        try:
            body_findings = scan_fragment_body(path.name, text)
        except CannotValidate:
            # A refusal that needed no parser outranks "could not look". The
            # alternative loses the definite answer to report the absent one,
            # which is the shape `validators/changelog-fragment` already names.
            if self_ref is None:
                raise
            continue
        if self_ref is not None or body_findings:
            findings.extend(body_findings)
            continue
        fragments.append(Fragment(frag.issue, frag.section, frag.slug, path))

    if findings:
        raise BadFragment("\n".join(findings))
    return sorted(fragments, key=lambda f: f.sort_key)


def _trim(block: List[str]) -> List[str]:
    """Drop leading and trailing blank lines, keep the ones in the middle."""
    while block and not block[0].strip():
        block.pop(0)
    while block and not block[-1].strip():
        block.pop()
    return block


def _subsections(body: Sequence[str]) -> Tuple[List[str], List[Tuple[str, List[str]]]]:
    """Split a section body into loose preamble and `### Heading` -> its lines.

    Indented continuation paragraphs stay with the entry above them: nothing is
    re-wrapped or re-parsed, lines are carried across verbatim. Entries in this
    changelog run to several paragraphs, and a fold that kept only the bullet
    would be loss reported as success.
    """
    preamble: List[str] = []
    sections: List[Tuple[str, List[str]]] = []
    for line in body:
        if line.startswith("### "):
            sections.append((line.strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return _trim(list(preamble)), [(title, _trim(block)) for title, block in sections]


def _merge_by_title(sections: Sequence[Tuple[str, List[str]]]) -> dict:
    """Fold same-named `###` blocks together, keyed case-insensitively.

    An `[Unreleased]` section that already carries two `### Fixed` headings is
    the live bug (#911), and #839 is why that matters: a duplicated heading
    reparents everything between the two copies. Emitting both again would
    carry the defect into a tagged release, so they merge here.
    """
    merged: dict = {}
    for title, block in sections:
        key = title.lower()
        if key in merged:
            if block:
                merged[key][1].extend([""] + block)
        else:
            merged[key] = [title, list(block)]
    return merged


def _entry_count(lines: Sequence[str]) -> int:
    """Top-level `- ` bullets. Continuation paragraphs indent, so they do not count."""
    return sum(1 for line in lines if line.startswith("- "))


def render(fragments: Sequence[Fragment], version: str, date: str,
           residue_preamble: Sequence[str] = (),
           residue_sections: Sequence[Tuple[str, List[str]]] = ()
           ) -> Tuple[str, List[str]]:
    """The release section as text, and the heading lines it wrote.

    Sections in Keep a Changelog order; within each, the folded `[Unreleased]`
    residue first (it has been pending longer), then the fragments in issue
    order. One heading per section whichever side supplied it.

    The second return value is the point of the signature: `_verify_written`
    re-parses the assembled file and needs to know which headings this
    function is *entitled* to have added, so that anything else in the result
    is a finding. Deriving that list by pattern-matching the output would put
    the verifier back on the same footing as the guard it exists to backstop.
    """
    out = ["## [{0}] - {1}".format(version, date), ""]
    emitted = [out[0]]
    if any(line.strip() for line in residue_preamble):
        out.extend(residue_preamble)
        out.append("")

    merged = _merge_by_title(residue_sections)
    used = set()
    for section in SECTIONS:
        title = "### {0}".format(section.capitalize())
        residue = merged.get(title.lower())
        chosen = [f for f in fragments if f.section == section]
        if not residue and not chosen:
            continue
        used.add(title.lower())
        out.append(title)
        emitted.append(title)
        out.append("")
        if residue and residue[1]:
            out.extend(residue[1])
            out.append("")
        for frag in chosen:
            assert frag.path is not None
            out.append(frag.path.read_text(encoding="utf-8").strip("\n").rstrip())
            out.append("")

    # Headings the spec does not list are content, not a parse failure. They keep
    # their own order, after the six known ones.
    for key, (title, block) in merged.items():
        if key in used or not block:
            continue
        out.append(title)
        emitted.append(title)
        out.append("")
        out.extend(block)
        out.append("")
    return "\n".join(out), emitted


def _document_facts(text: str) -> Tuple[Counter, Dict[str, str], int]:
    """(heading multiset, label -> destination, raw-HTML count) of a document.

    The three properties a fragment can forge, read off a real parse of the
    whole file rather than inferred from the fragment that went into it.
    """
    md = _parser()
    env: Dict = {}
    flat = [token for token, _ in _flatten(md.parse(text, env))]
    headings: Counter = Counter()
    for index, token in enumerate(flat):
        if token.type == "heading_open":
            title = flat[index + 1].content if index + 1 < len(flat) else ""
            headings[(token.tag, title)] += 1
    refs = {label: value.get("href")
            for label, value in env.get("references", {}).items()}
    raw = sum(1 for token in flat if token.type in ("html_block", "html_inline"))
    return headings, refs, raw


def _headings(text: str) -> List[Tuple[int, str, str]]:
    """(line index, tag, title) for every heading the parser actually sees.

    `line.startswith("## [")` was the old test and #936 disproved it: a
    fenced example of a release heading is inert to a reader and was an
    anchor to this file. `changelog.d/README.md` prescribes exactly that fence
    as *the* way to quote a heading in an entry, so CHANGELOG.md acquires such
    lines by design, not by attack.
    """
    md = _parser()
    flat = [token for token, _ in _flatten(md.parse(text, {}))]
    found = []
    for index, token in enumerate(flat):
        if token.type == "heading_open" and token.map:
            found.append((token.map[0], token.tag,
                          flat[index + 1].content if index + 1 < len(flat) else ""))
    return found


def _inert_lines(text: str) -> Set[int]:
    """Line indices inside a code block or raw HTML block, per the parser.

    Every positional scanner in this file used to read these lines as live.
    They are the lines a reader's parser will not act on, so they are the
    lines a release must not act on either.
    """
    inert: Set[int] = set()
    for token, _ in _flatten(_parser().parse(text, {})):
        if token.type in ("fence", "code_block", "html_block") and token.map:
            inert.update(range(token.map[0], token.map[1]))
    return inert


def _crowded_headings(text: str) -> Set[str]:
    """Titles of headings written directly against the line above them (#1113).

    CommonMark lets an ATX heading interrupt a paragraph, so GitHub renders
    one of these correctly and nothing looks wrong; it is only wrong in the
    source, and only to a stricter parser, which folds the heading into the
    paragraph before it. The artefact that breaks is the one users read to
    decide whether to upgrade.

    Keyed by title rather than by line, because the caller subtracts the
    before-set from the after-set. The four instances already in the file
    shipped inside tags and GitHub release notes; repairing them would make
    CHANGELOG.md stop matching what was published, so they are carried
    forward and only a *new* one is a finding.

    Positional on purpose: the blank line is a property of the bytes, which is
    what the stricter parser reads. The *set of headings* still comes from the
    parser, so a fenced example of a release heading is not one of these.
    """
    lines = text.splitlines()
    return {title for index, _, title in _headings(text)
            if index and lines[index - 1].strip()}


def _section_lines(section: str) -> List[str]:
    """A rendered release section as lines, ending in exactly one blank.

    `render` builds its list ending in `""` and joins it, so the text ends in
    a newline — and `str.splitlines()` drops the empty field that newline
    produces. The section's last body line then landed directly against the
    `## [x.y.z]` heading it was spliced above, on every release since 0.25.0.
    `split("\n")` keeps that field; the normalisation below states the
    invariant the splice depends on rather than inheriting it from `render`.
    """
    lines = section.split("\n")
    while len(lines) > 1 and not lines[-1] and not lines[-2]:
        lines.pop()
    if not lines or lines[-1]:
        lines.append("")
    return lines


def _anchor(headings: Sequence[Tuple[int, str, str]]) -> int:
    """Where the new release section goes: above the newest existing release.

    The first `h2` whose title opens `[` and is not `[Unreleased]`. Everything
    between the `[Unreleased]` heading and this line is residue that gets
    folded into the release being cut — `[Unreleased]` means "goes out next",
    so it does.
    """
    for index, tag, title in headings:
        if tag == "h2" and title.startswith("[") and not title.startswith("[Unreleased]"):
            return index
    raise BadFragment(
        "CHANGELOG.md has no `## [x.y.z]` release heading to insert above — "
        "refusing rather than guessing where a release section belongs"
    )


def _unreleased_span(lines: Sequence[str], headings: Sequence[Tuple[int, str, str]],
                     anchor: int) -> Tuple[Optional[int], List[str]]:
    """The `## [Unreleased]` heading's index and its body, above `anchor`."""
    for index, tag, title in headings:
        if index < anchor and tag == "h2" and title.startswith("[Unreleased]"):
            return index, list(lines[index + 1:anchor])
    return None, []


def _link_ref_block(lines: Sequence[str], inert: Set[int]) -> Optional[Tuple[int, int]]:
    """The trailing run of link-reference definitions, inclusive, or None.

    The link refs of a Keep a Changelog document are one block at the bottom.
    Anything above it that looks like one is prose — a quoted example, a
    previous bad cut's residue, an entry about link refs — and prose is not
    where a release writes.

    Fenced lines are stepped over rather than stopped at. An entry that ends
    the file with a fenced example of a link-ref block used to end the walk on
    the closing fence, so the block was never found and the release advanced
    no link at all while reporting `links none — ... left alone`: a receipt
    that named the absence and not the reason for it.
    """
    index = len(lines) - 1
    while index >= 0 and (not lines[index].strip() or index in inert):
        index -= 1
    end = index
    while index >= 0 and index not in inert and _LINK_REF_RE.match(lines[index]):
        index -= 1
    return (index + 1, end) if index + 1 <= end else None


def _rewrite_links(lines: List[str], version: str
                   ) -> Optional[Tuple[str, List[str]]]:
    """Point `[Unreleased]` at the new tag and add the new version's link ref.

    Scoped to the bottom link-ref block (#923): this used to return on its
    first match anywhere in the file, and fragment bodies land near the top, so
    one `[Unreleased]: .../compare/v...HEAD` line inside an entry decided the
    base URL of the tag ref the release shipped — durably, since the line is
    still there and still matched first on the next cut.

    Returns the summary and the definition lines it wrote, which is what
    `_verify_written` compares the released file's own link table against.
    """
    span = _link_ref_block(lines, _inert_lines("\n".join(lines)))
    if span is None:
        return None
    start, end = span
    for index in range(start, end + 1):
        line = lines[index]
        match = _UNRELEASED_LINK_RE.match(line)
        if not match:
            continue
        base = match.group("base")
        lines[index] = "[Unreleased]: {0}/compare/v{1}...HEAD".format(base, version)
        lines.insert(index + 1, "[{0}]: {1}/releases/tag/v{0}".format(version, base))
        return ("[Unreleased] → compare/v{0}...HEAD, added [{0}] tag ref".format(version),
                [lines[index], lines[index + 1]])
    return None


# Versions with a `## [x.y.z]` section and no tag anywhere — nothing was ever
# pushed for them, so there is no release page to link to and a
# `releases/tag/vX.Y.Z` URL invented for one is a 404 that renders as a working
# link. This is the audit's third state made explicit: not "ok", not a finding,
# but "there is no answer to give". It is closed by construction — every version
# from 0.20.0 on is cut by the assembler, which writes the ref as it goes — and
# `tests/test_changelog_link_refs_918.py` refuses anything at or above that floor
# being added here.
UNTAGGED_RELEASES = frozenset({
    "0.19.0", "0.18.0", "0.17.0", "0.16.0", "0.15.0", "0.14.0", "0.11.0",
})

_COMPARE_HREF_RE = re.compile(r"/compare/v(?P<version>\d+\.\d+\.\d+)\.\.\.HEAD$")


def release_versions(text: str) -> List[str]:
    """Every `## [x.y.z]` release version, newest first, off a real parse.

    A parse and not a line prefix, for the reason #936 cost three rounds: this
    file quotes release headings inside fenced blocks by house style, so the
    characters `## [` appear in it without a heading being there.
    """
    versions = []
    for _, tag, title in _headings(text):
        if tag != "h2" or not title.startswith("[") or "]" not in title:
            continue
        label = title[1:title.index("]")]
        if _VERSION_RE.match(label):
            versions.append(label)
    return versions


def audit_link_refs(text: str,
                    untagged: Optional[AbstractSet[str]] = None) -> List[str]:
    """What the link-ref table at the bottom disagrees with the file about.

    The assembler writes one definition per cut, which keeps the *next* release
    honest and says nothing about the state it inherited — `[0.24.0]` and
    `[0.25.0]` shipped with none, and `[Unreleased]` sat two tags behind twice
    (#918). Both are the same defect: a link that resolves, returns a real page,
    and answers a different question than the one the reader asked.

    Raises rather than returning `[]` when there is no release heading at all.
    An empty finding list from a document that could not be audited is the
    absence-read-as-an-all-clear this file exists to not do.
    """
    declared = UNTAGGED_RELEASES if untagged is None else untagged
    versions = release_versions(text)
    if not versions:
        raise CannotValidate(
            "no `## [x.y.z]` release heading was found, so there is nothing to "
            "audit the link refs against — 0 findings here would read as a "
            "clean table rather than as a table nobody looked at.")
    _, refs, _ = _document_facts(text)

    findings: List[str] = []
    for version in versions:
        href = refs.get(version.upper())
        if version in declared and href:
            findings.append(
                "[{0}] is declared as never tagged but has a link ref ({1}) — "
                "one of the two is wrong, and a `releases/tag/v{0}` for a tag "
                "that was never pushed is a 404 that reads as a working link"
                .format(version, href))
        elif version not in declared and not href:
            findings.append(
                "`## [{0}]` has no link ref, so it renders as literal bracketed "
                "text instead of a link to the release — add "
                "`[{0}]: <repo>/releases/tag/v{0}` to the block at the bottom"
                .format(version))

    present = set(versions)
    for version in sorted(declared - present):
        findings.append(
            "[{0}] is declared as never tagged but has no `## [{0}]` section in "
            "the file — a stale declaration is where a genuinely missing ref "
            "gets filed away without anyone deciding to".format(version))

    unreleased = refs.get("UNRELEASED")
    if not unreleased:
        findings.append(
            "[Unreleased] has no link ref — the heading a reader clicks to see "
            "what is pending links nowhere")
    else:
        match = _COMPARE_HREF_RE.search(unreleased)
        if not match:
            findings.append(
                "[Unreleased] does not resolve to a `compare/vX.Y.Z...HEAD` "
                "link: {0}".format(unreleased))
        elif match.group("version") != versions[0]:
            findings.append(
                "[Unreleased] compares from v{0} but the newest release section "
                "is [{1}] — that link resolves and shows everything released "
                "since v{0} as unreleased work".format(
                    match.group("version"), versions[0]))
    return findings


def check_links(changelog: Path) -> int:
    """`--check-links`: audit the table, and say which of the three it did."""
    try:
        text = changelog.read_text(encoding="utf-8")
    except OSError as exc:
        _receipt("skipped", "cannot read {0}: {1} — nothing was audited"
                 .format(changelog, exc))
        return SKIPPED
    try:
        findings = audit_link_refs(text)
    except CannotValidate as exc:
        _receipt("skipped", "{0}".format(exc))
        return SKIPPED
    if findings:
        _receipt("refused", "{0} finding(s) in {1}'s link ref table"
                 .format(len(findings), changelog.name), findings)
        return REFUSED
    versions = release_versions(text)
    _receipt("ok", "{0} release section(s) in {1}, parsed with markdown-it-py "
                   "{2}: each has a link ref or is declared untagged, and "
                   "[Unreleased] compares from v{3}"
             .format(len(versions), changelog.name, _MD_VERSION, versions[0]),
             ["untagged  " + ", ".join(sorted(UNTAGGED_RELEASES & set(versions)))]
             if UNTAGGED_RELEASES & set(versions) else [])
    return OK


def _verify_written(before: str, after: str, emitted: Sequence[str],
                    written_refs: Sequence[str]) -> List[str]:
    """Re-parse the file about to be written and report what it gained.

    The second layer, and the reason there is one: a fragment is validated
    alone and inserted into a document, and one guard over this file has now
    been wrong three times running. This does not consult the fragments at
    all. It asks the parser what the assembled document *is*, and refuses
    unless its heading table is the old one plus exactly the headings `render`
    reports writing, its link-reference table is the old one plus exactly the
    definitions `_rewrite_links` reports writing, and it gained no raw HTML.

    That holds whatever the per-fragment guard missed, which is the property
    the previous three rounds each shipped a receipt for without having.
    """
    before_headings, before_refs, before_raw = _document_facts(before)
    after_headings, after_refs, after_raw = _document_facts(after)
    allowed, _, _ = _document_facts("\n".join(emitted) + "\n")

    expected_refs = dict(before_refs)
    if written_refs:
        _, added, _ = _document_facts("\n".join(written_refs) + "\n")
        expected_refs.update(added)

    findings: List[str] = []
    surplus = after_headings - (before_headings + allowed)
    if surplus:
        findings.append(
            "re-parse of the assembled file found {0} heading(s) this release "
            "did not write: {1}".format(
                sum(surplus.values()),
                ", ".join("<{0}>{1}".format(tag, title[:60])
                          for tag, title in sorted(surplus))))
    if after_refs != expected_refs:
        differing = sorted(set(after_refs) ^ set(expected_refs)) or sorted(
            label for label in after_refs if after_refs[label] != expected_refs.get(label))
        pre_existing = all(after_refs.get(label) == before_refs.get(label)
                           for label in differing)
        findings.append(
            "re-parse of the assembled file found a link ref table this release "
            "did not write — label(s) {0}. First definition of a label wins, so a "
            "definition earlier in the file beats the block at the bottom that "
            "this release rewrites: {1}. {2}".format(
                ", ".join(differing),
                "; ".join("[{0}] resolves to {1}, this release wrote {2}".format(
                    label, after_refs.get(label, "nothing"),
                    expected_refs.get(label, "nothing")) for label in differing),
                "That earlier definition is already in CHANGELOG.md and no "
                "fragment introduced it — fix the file, then cut."
                if pre_existing else
                "A fragment consumed by this run introduced it."))
    if after_raw > before_raw:
        findings.append(
            "re-parse of the assembled file found {0} new raw HTML token(s), "
            "which render as structure a reader will trust".format(after_raw - before_raw))
    crowded = sorted(_crowded_headings(after) - _crowded_headings(before))
    if crowded:
        findings.append(
            "the assembled file writes {0} heading(s) with no blank line above "
            "them, which a stricter Markdown parser folds into the paragraph "
            "before rather than rendering as a heading: {1}. The ones already "
            "in CHANGELOG.md are carried forward untouched — they shipped in "
            "tags (#1113)".format(len(crowded), ", ".join(crowded)))
    return findings

def _receipt(state: str, summary: str, details: Sequence[str] = ()) -> None:
    print("assemble    : {0:<11} ({1})".format(state, summary))
    for line in details:
        print("  {0}".format(line))


def assemble(changelog: Path, directory: Path, version: str, date: str,
             dry_run: bool = False, keep: bool = False) -> int:
    if not _VERSION_RE.match(version):
        _receipt("refused", "--version {0!r} is not x.y.z".format(version))
        return REFUSED

    try:
        fragments = collect(directory)
    except CannotValidate as exc:
        _receipt("skipped", "{0} CHANGELOG.md untouched, nothing consumed".format(exc))
        return SKIPPED
    except BadFragment as exc:
        findings = str(exc).splitlines()
        _receipt("refused", "{0} finding(s) — CHANGELOG.md untouched, nothing consumed"
                 .format(len(findings)),
                 ["{0}/{1}".format(directory.name, line) for line in findings])
        return REFUSED

    if not fragments:
        _receipt("skipped", "no fragments in {0}/ — nothing to assemble; "
                            "CHANGELOG.md untouched".format(directory.name))
        return SKIPPED

    text = changelog.read_text(encoding="utf-8")
    lines = text.splitlines()

    try:
        headings = _headings(text)
    except CannotValidate as exc:
        _receipt("skipped", "{0} CHANGELOG.md untouched".format(exc))
        return SKIPPED

    # A *heading*, not the substring, and not a line that looks like one either
    # (#936). Entries in this file quote release headings — #839's whole
    # subject is one, and changelog.d/README.md prescribes a fenced block as
    # the way to do it — so `"## [x]" in text` and `line.startswith("## [")`
    # both answer a question about characters when the question is about
    # structure. The parser is asked instead.
    if any(tag == "h2" and title.startswith("[{0}]".format(version))
           for _, tag, title in headings):
        _receipt("refused", "CHANGELOG.md already has a `## [{0}]` section — "
                            "assembling again would duplicate a release heading".format(version))
        return REFUSED

    try:
        anchor = _anchor(headings)
    except BadFragment as exc:
        _receipt("refused", str(exc))
        return REFUSED

    # `[Unreleased]` means "goes out in the next release", so it goes out in it.
    # Leaving it behind strands the entries twice over: the tag ships silently
    # omitting work that is in the tag, and the work still reads as pending.
    unreleased_at, residue_body = _unreleased_span(lines, headings, anchor)
    preamble, residue_sections = _subsections(residue_body)
    folded = _entry_count(residue_body)

    section, emitted = render(fragments, version, date, preamble, residue_sections)

    # Arithmetic, not trust: every entry on either side has to be in the result.
    # A merge that dropped one would otherwise be indistinguishable from a clean
    # run, which is the whole failure mode this file is built against.
    expected = folded + sum(
        _entry_count(f.path.read_text(encoding="utf-8").splitlines())
        for f in fragments if f.path)
    produced = _entry_count(section.splitlines())
    if produced != expected:
        _receipt("refused", "entry count does not balance: {0} folded + fragments = {1} "
                            "expected, {2} produced — refusing to write a lossy changelog"
                 .format(folded, expected, produced))
        return REFUSED

    if unreleased_at is None:
        body = list(lines[:anchor]) + _section_lines(section) + list(lines[anchor:])
    else:
        body = (list(lines[:unreleased_at + 1]) + [""] + _section_lines(section)
                + list(lines[anchor:]))
    rewritten = _rewrite_links(body, version)
    links, written_refs = rewritten if rewritten else (None, [])

    assembled = "\n".join(body) + "\n"
    try:
        structural = _verify_written(text, assembled, emitted, written_refs)
    except CannotValidate as exc:
        _receipt("skipped", "{0} CHANGELOG.md untouched".format(exc))
        return SKIPPED
    if structural:
        _receipt("refused", "{0} finding(s) in the assembled file — CHANGELOG.md "
                            "untouched, nothing consumed".format(len(structural)),
                 structural)
        return REFUSED

    details = [
        "consumed  " + ", ".join(f.path.name for f in fragments if f.path),
        "sections  " + ", ".join(
            "{0} ({1})".format(name.capitalize(), sum(1 for f in fragments if f.section == name))
            for name in SECTIONS if any(f.section == name for f in fragments)),
    ]
    if links:
        details.append("links     " + links)
    else:
        details.append("links     none — no `[Unreleased]: .../compare/vX...HEAD` line found "
                       "in the trailing definition block, so the link refs were left alone")
    if folded:
        details.append(
            "folded    {0} entr{1} from `## [Unreleased]` into [{2}], above the fragments. "
            "The heading stays as the compare-link anchor; its body is now empty."
            .format(folded, "y" if folded == 1 else "ies", version))
    else:
        details.append("folded    0 — `## [Unreleased]` was already empty")
    details.append(
        "verified  the assembled file was re-parsed with markdown-it-py {0}: its "
        "headings are the ones already there plus the {1} this run wrote, its link "
        "ref table is the one already there plus what this run wrote, and it gained "
        "no raw HTML".format(_MD_VERSION, len(emitted)))

    if dry_run:
        _receipt("ok", "dry-run: {0} fragment(s) would become `## [{1}] - {2}`; "
                       "nothing written".format(len(fragments), version, date), details)
        return OK

    changelog.write_text(assembled, encoding="utf-8")
    if not keep:
        for frag in fragments:
            if frag.path:
                frag.path.unlink()
        details.append("removed   {0} fragment file(s) from {1}/"
                       .format(len(fragments), directory.name))
    else:
        details.append("kept      --keep: {0} fragment file(s) left in {1}/ — they will ship "
                       "twice if the next release also consumes them"
                       .format(len(fragments), directory.name))

    _receipt("ok", "{0} fragment(s) → `## [{1}] - {2}` in {3}"
             .format(len(fragments), version, date, changelog.name), details)
    return OK


def check(directory: Path) -> int:
    try:
        fragments = collect(directory)
    except CannotValidate as exc:
        # Three states, applied to the gate itself. `--check` is what a
        # reviewer trusts *instead of* reading the fragment, so a run that
        # established nothing has to say nothing was established — and exit
        # non-zero, because a green CI leg that validated nothing is the same
        # false assurance three rounds of this file already shipped.
        _receipt("skipped", str(exc))
        return SKIPPED
    except BadFragment as exc:
        findings = str(exc).splitlines()
        _receipt("refused", "{0} fragment(s) will not assemble".format(len(findings)),
                 ["{0}/{1}".format(directory.name, line) for line in findings])
        return REFUSED
    if not fragments:
        _receipt("skipped", "{0}/ holds 0 fragments — nothing to validate"
                 .format(directory.name))
        return OK
    # The receipt states what was established and names what established it,
    # which the last three did not. "no body writes at column 0" stayed
    # literally true through three bypasses; "none can open a heading or a
    # link ref, at any indent or nesting" was true of the scanner's own model
    # of CommonMark and false of CommonMark. This claim is checkable by the
    # person reading it: it is what markdown-it-py saw.
    _receipt("ok", "{0} fragments, all names parse, each body names the issue "
                   "in its own filename; each body parsed with "
                   "markdown-it-py {1}, whose token stream holds no heading, no "
                   "link ref definition and no raw HTML at any depth, whose "
                   "fences all close inside the fragment, and whose top level is "
                   "one `- ` bullet list"
             .format(len(fragments), _MD_VERSION),
             ["{0}  {1}".format(f.path.name if f.path else "?", f.section) for f in fragments])
    return OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", help="the version being cut, x.y.z")
    parser.add_argument("--date", default=datetime.date.today().isoformat(),
                        help="release date, YYYY-MM-DD (default: today)")
    parser.add_argument("--changelog", default=str(REPO / "CHANGELOG.md"))
    parser.add_argument("--dir", dest="directory", default=str(REPO / "changelog.d"))
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--keep", action="store_true", help="do not delete consumed fragments")
    parser.add_argument("--check", action="store_true",
                        help="validate every fragment name and body; write nothing")
    parser.add_argument("--check-links", dest="check_links", action="store_true",
                        help="audit CHANGELOG.md's link ref table; write nothing")
    parser.add_argument("--count", action="store_true",
                        help="print the fragment count as a bare integer, and nothing else")
    args = parser.parse_args(list(argv) if argv is not None else None)

    directory = Path(args.directory)

    if args.count:
        try:
            print(len(collect(directory)))
        except CannotValidate as exc:
            # Not a count of 0 on stdout. A caller piping this into arithmetic
            # would read "nothing pending" from "could not look".
            print(exc, file=sys.stderr)
            return SKIPPED
        except BadFragment as exc:
            print(exc, file=sys.stderr)
            return REFUSED
        return OK

    if args.check_links:
        return check_links(Path(args.changelog))

    if args.check:
        return check(directory)

    if not args.version:
        _receipt("refused", "--version is required to assemble "
                            "(or pass --check / --count for the read-only modes)")
        return REFUSED

    return assemble(Path(args.changelog), directory, args.version, args.date,
                    dry_run=args.dry_run, keep=args.keep)

if __name__ == "__main__":
    raise SystemExit(main())
