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
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"


def test_readme_stays_an_order_of_magnitude_smaller_than_the_docs_it_points_into() -> None:
    lines = README.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 250, (
        f"README.md is {len(lines)} lines -- #2142 moved the mechanism-heavy "
        "sections into docs/ so the front page stays a pitch. A README this "
        "long again means a section grew back into a paragraph and belongs "
        "in docs/ instead."
    )


def test_every_local_link_the_readme_makes_resolves_to_a_real_file() -> None:
    text = README.read_text(encoding="utf-8")
    missing = []
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#")):
            continue
        path = target.split("#", 1)[0]
        if not path or (ROOT / path).is_file():
            continue
        missing.append(target)
    assert not missing, (
        "README.md links to a path that does not exist on disk: " + ", ".join(missing)
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
