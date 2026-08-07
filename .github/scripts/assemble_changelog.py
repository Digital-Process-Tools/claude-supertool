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

Stdlib only, deliberately. `towncrier` and `scriv` both solve this and both are
dependencies; this repo ships one file and no install step.

    python3 .github/scripts/assemble_changelog.py --version 0.24.0
    python3 .github/scripts/assemble_changelog.py --check     # CI: do all names parse?
    python3 .github/scripts/assemble_changelog.py --count     # exact fragment count

Exit codes: 0 ok, 1 skipped (nothing to do, stated), 2 refused (a finding).
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[2]

#: Keep a Changelog 1.1.0, in the order the spec lists them. The order is data,
#: not a sort: "Added" before "Fixed" is a convention readers rely on, and
#: alphabetical would put Security second.
SECTIONS = ("added", "changed", "deprecated", "removed", "fixed", "security")

#: `<issue>.<section>[.<slug>].md`. The slug exists so one issue can file two
#: entries in one section without the two PRs colliding on a path again.
_NAME_RE = re.compile(r"^(\d+)\.([a-z]+)(?:\.([A-Za-z0-9][A-Za-z0-9._-]*))?\.md$")

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: Not fragments, and not mistakes either — refusing these would make the
#: directory unable to document itself.
_IGNORED = {"README.md", ".gitkeep", ".gitignore"}

_UNRELEASED_LINK_RE = re.compile(
    r"^\[Unreleased\]:\s*(?P<base>\S+?)/compare/v(?P<prev>[0-9][^.\s]*(?:\.[^.\s]+)*)\.\.\.HEAD\s*$"
)

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
        if not path.read_text(encoding="utf-8").strip():
            findings.append(f"{path.name}: fragment is empty — an entry nobody would ever read")
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
           residue_sections: Sequence[Tuple[str, List[str]]] = ()) -> str:
    """The release section, as text.

    Sections in Keep a Changelog order; within each, the folded `[Unreleased]`
    residue first (it has been pending longer), then the fragments in issue
    order. One heading per section whichever side supplied it.
    """
    out = ["## [{0}] - {1}".format(version, date), ""]
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
        out.append("")
        out.extend(block)
        out.append("")
    return "\n".join(out)


def _anchor(lines: Sequence[str]) -> int:
    """Where the new release section goes: above the newest existing release.

    The first `## [` heading that is *not* `[Unreleased]`. Everything between
    the `[Unreleased]` heading and this line is residue that gets folded into
    the release being cut — `[Unreleased]` means "goes out next", so it does.
    """
    for index, line in enumerate(lines):
        if line.startswith("## [") and not line.startswith("## [Unreleased]"):
            return index
    raise BadFragment(
        "CHANGELOG.md has no `## [x.y.z]` release heading to insert above — "
        "refusing rather than guessing where a release section belongs"
    )


def _unreleased_span(lines: Sequence[str], anchor: int) -> Tuple[Optional[int], List[str]]:
    """The `## [Unreleased]` heading's index and its body, above `anchor`."""
    for index, line in enumerate(lines[:anchor]):
        if line.startswith("## [Unreleased]"):
            return index, list(lines[index + 1:anchor])
    return None, []


def _rewrite_links(lines: List[str], version: str) -> Optional[str]:
    """Point `[Unreleased]` at the new tag and add the new version's link ref."""
    for index, line in enumerate(lines):
        match = _UNRELEASED_LINK_RE.match(line)
        if not match:
            continue
        base = match.group("base")
        lines[index] = "[Unreleased]: {0}/compare/v{1}...HEAD".format(base, version)
        lines.insert(index + 1, "[{0}]: {1}/releases/tag/v{0}".format(version, base))
        return "[Unreleased] → compare/v{0}...HEAD, added [{0}] tag ref".format(version)
    return None


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
    # A *heading*, not the substring. Entries in this file quote headings
    # (#839's whole subject is one), and `"## [x]" in text` cannot tell an
    # entry about a release from the release — #731, one file over.
    if any(ln.startswith("## [{0}]".format(version)) for ln in lines):
        _receipt("refused", "CHANGELOG.md already has a `## [{0}]` section — "
                            "assembling again would duplicate a release heading".format(version))
        return REFUSED

    try:
        anchor = _anchor(lines)
    except BadFragment as exc:
        _receipt("refused", str(exc))
        return REFUSED

    # `[Unreleased]` means "goes out in the next release", so it goes out in it.
    # Leaving it behind strands the entries twice over: the tag ships silently
    # omitting work that is in the tag, and the work still reads as pending.
    unreleased_at, residue_body = _unreleased_span(lines, anchor)
    preamble, residue_sections = _subsections(residue_body)
    folded = _entry_count(residue_body)

    section = render(fragments, version, date, preamble, residue_sections)

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
        body = list(lines[:anchor]) + section.splitlines() + list(lines[anchor:])
    else:
        body = (list(lines[:unreleased_at + 1]) + [""] + section.splitlines()
                + list(lines[anchor:]))
    links = _rewrite_links(body, version)

    details = [
        "consumed  " + ", ".join(f.path.name for f in fragments if f.path),
        "sections  " + ", ".join(
            "{0} ({1})".format(name.capitalize(), sum(1 for f in fragments if f.section == name))
            for name in SECTIONS if any(f.section == name for f in fragments)),
    ]
    if links:
        details.append("links     " + links)
    else:
        details.append("links     none — no `[Unreleased]: .../compare/vX...HEAD` line found, "
                       "so the link refs were left alone")
    if folded:
        details.append(
            "folded    {0} entr{1} from `## [Unreleased]` into [{2}], above the fragments. "
            "The heading stays as the compare-link anchor; its body is now empty."
            .format(folded, "y" if folded == 1 else "ies", version))
    else:
        details.append("folded    0 — `## [Unreleased]` was already empty")

    if dry_run:
        _receipt("ok", "dry-run: {0} fragment(s) would become `## [{1}] - {2}`; "
                       "nothing written".format(len(fragments), version, date), details)
        return OK

    changelog.write_text("\n".join(body) + "\n", encoding="utf-8")
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
    except BadFragment as exc:
        findings = str(exc).splitlines()
        _receipt("refused", "{0} fragment(s) will not assemble".format(len(findings)),
                 ["{0}/{1}".format(directory.name, line) for line in findings])
        return REFUSED
    if not fragments:
        _receipt("skipped", "{0}/ holds 0 fragments — nothing to validate"
                 .format(directory.name))
        return OK
    _receipt("ok", "{0} fragments, all names parse".format(len(fragments)),
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
                        help="validate every fragment name; write nothing")
    parser.add_argument("--count", action="store_true",
                        help="print the fragment count as a bare integer, and nothing else")
    args = parser.parse_args(list(argv) if argv is not None else None)

    directory = Path(args.directory)

    if args.count:
        try:
            print(len(collect(directory)))
        except BadFragment as exc:
            print(exc, file=sys.stderr)
            return REFUSED
        return OK

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
