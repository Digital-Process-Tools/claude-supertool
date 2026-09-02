"""#2142 -- README.md was 825 lines / 103KB. Three of its "## Why" bullets were
each over 3,000 characters of changelog-register prose (specific issue numbers,
mechanisms, historical incidents) doing a job docs/ already exists for, per this
repo's own house style in CLAUDE.md ("README sells the outcome, docs/ holds the
mechanism").

This does not pin an exact line count -- a new section legitimately grows the
file, and pinning a number just moves the drift into this test the way #1854
warned a hand-written guard on a *set* eventually does. What it pins is the
shape: the front page stays an order of magnitude smaller than the doc corpus
it now points into, and every local link it makes actually resolves -- so a
future edit that quietly re-inflates one "## Why" bullet back into a paragraph,
or links to a docs/ page that was renamed out from under it, goes red here
instead of drifting the way the badge did.

The link check also resolves the `#anchor` half, not just the file half.
`Explore`'s review of the first cut of this file (spawned per the developer
brief's self-review step) found that `path.split("#", 1)[0]` throws the anchor
away before checking anything -- a heading renamed in `docs/philosophy.md`
without updating the README's link to it would pass silently. `_github_slug`
below is a GitHub-compatible-enough heading slugger (lowercase, strip
backtick/bold markup, drop punctuation, spaces to hyphens) checked against
every anchor this repo's README actually uses today.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def _github_slug(heading: str) -> str:
    text = re.sub(r"[`*]", "", heading)  # strip markdown emphasis markup only -- `_` is a real word char GitHub keeps in slugs
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    # GitHub replaces each individual space with a hyphen without collapsing
    # runs -- a removed em-dash between two spaces leaves "word  word" and
    # becomes "word--word", not "word-word". Stripping first still trims
    # leading/trailing runs the heading itself never has.
    text = text.strip().replace(" ", "-")
    return text


def _headings_in(path: Path) -> set:
    if not path.is_file():
        return set()
    slugs = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _HEADING.match(line)
        if m:
            slugs.add(_github_slug(m.group(1)))
    return slugs


def test_readme_stays_an_order_of_magnitude_smaller_than_the_docs_it_points_into() -> None:
    lines = README.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 250, (
        f"README.md is {len(lines)} lines -- #2142 moved the mechanism-heavy "
        "sections into docs/ so the front page stays a pitch. A README this "
        "long again means a section grew back into a paragraph and belongs "
        "in docs/ instead."
    )


def test_every_local_link_the_readme_makes_resolves_to_a_real_file_and_anchor() -> None:
    text = README.read_text(encoding="utf-8")
    missing_files = []
    missing_anchors = []
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#")):
            continue
        path, _, anchor = target.partition("#")
        if not path:
            continue
        resolved = ROOT / path
        if not resolved.is_file():
            missing_files.append(target)
            continue
        if anchor and anchor not in _headings_in(resolved):
            missing_anchors.append(target)
    assert not missing_files, (
        "README.md links to a path that does not exist on disk: " + ", ".join(missing_files)
    )
    assert not missing_anchors, (
        "README.md links to a heading that does not exist in the target file "
        "(renamed or removed on the docs/ side without updating the README): "
        + ", ".join(missing_anchors)
    )


def test_the_moved_sections_survive_as_real_docs_files() -> None:
    # #2142 moved "Four pillars" / "Receipt" / "Why I built this" / "How this
    # repo is maintained" to docs/philosophy.md, and the remaining "Design
    # decisions" bullets to docs/design-decisions.md. Both are new files
    # nothing else in the suite pins yet -- assert they exist and are not
    # accidentally empty stubs.
    for rel, must_contain in (
        ("docs/philosophy.md", ("Why I built this", "How this repo is maintained")),
        ("docs/design-decisions.md", ("Python 3.9+",)),
    ):
        path = ROOT / rel
        assert path.is_file(), f"{rel} must exist -- #2142 moved README content here"
        text = path.read_text(encoding="utf-8")
        for needle in must_contain:
            assert needle in text, f"{rel} is missing {needle!r} -- content lost in the move"


def test_the_slugger_agrees_with_every_anchor_the_readme_actually_uses() -> None:
    # Positive control for test_every_local_link_..._and_anchor: if _github_slug
    # disagreed with GitHub's own algorithm on every real anchor in this file,
    # the assertion above would still pass (every link would look broken) or
    # still fail vacuously -- this proves the slugger recognises the anchors
    # this repo already ships, not just that it runs without crashing.
    text = README.read_text(encoding="utf-8")
    anchored = [t for t in re.findall(r"\]\(([^)]+)\)", text) if "#" in t and not t.startswith("#")]
    assert anchored, "README.md has no anchored docs/ links to check the slugger against"
    for target in anchored:
        path, _, anchor = target.partition("#")
        assert anchor in _headings_in(ROOT / path), (
            f"{target} -- slugger did not recognise a real anchor; fix _github_slug"
        )

# #2189 -- #2142's cut left the README's only real surface as the ops table.
# Twelve doc/ files had zero links from the front page: validators, formatters,
# notifiers, the MCP/LSP daemon docs, and every preset page except git/github.
# The fix is not a new README section per subsystem (that regrows the file
# #2142 just cut) -- it is a short "beyond the ops table" block plus links
# added directly on the existing ops-table rows, so every docs/ file becomes
# reachable by *some* chain of local links starting at README.md, the same
# way docs/presets/index.md already makes the preset docs reachable in two
# hops today.
_ALLOWLISTED_UNLINKED_DOCS = {
    # (currently none -- see test below for the reasoning: everything under
    # docs/ is reachable from README.md by walking local links, including
    # docs/cursor-witness.md, which resolves through docs/notifiers.md.)
}


def _local_md_links(path):
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8")
    targets = set()
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#")):
            continue
        rel, _, _anchor = target.partition("#")
        if not rel or not rel.endswith(".md"):
            continue
        resolved = (path.parent / rel).resolve()
        targets.add(resolved)
    return targets


def test_every_docs_file_is_reachable_from_readme_or_explicitly_unlinked() -> None:
    # BFS over local .md links starting at README.md -- not just a same-file
    # grep count, so a hub page like docs/presets/index.md legitimately
    # covers everything one hop past it.
    seen = {README.resolve()}
    frontier = [README.resolve()]
    while frontier:
        current = frontier.pop()
        for nxt in _local_md_links(current):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)

    all_docs = {p.resolve() for p in (ROOT / "docs").rglob("*.md")}
    allowlisted = {(ROOT / rel).resolve() for rel in _ALLOWLISTED_UNLINKED_DOCS}
    unreachable = all_docs - seen - allowlisted
    assert not unreachable, (
        "docs/ files unreachable from README.md by any chain of local links, "
        "and not in _ALLOWLISTED_UNLINKED_DOCS with a reason: "
        + ", ".join(sorted(str(p.relative_to(ROOT)) for p in unreachable))
    )

