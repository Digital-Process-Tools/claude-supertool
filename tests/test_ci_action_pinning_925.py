"""Every third-party action is pinned to a commit sha, not a moving tag (#925).

`actions/checkout@v7` is a *branch-like* ref: the `v7` tag is repointed by the
publisher on every v7.x release. So the code that runs in this repo's CI, with
this repo's `GITHUB_TOKEN`, can change between one run and the next with no
commit here and no pull request anywhere. That is not hypothetical: the
tj-actions/changed-files compromise of March 2025 worked exactly this way, by
retagging every existing major.

`.github/dependabot.yml` reads as coverage for this and is not -- it will never
bump `v7` to `v7`, so a retag is live before any PR exists. Pinning to a sha
*inverts* that: the sha cannot move, and Dependabot does bump sha-pinned actions
(rewriting both the sha and the trailing version comment), so the maintenance
cost this buys is a reviewable weekly PR rather than a standing manual chore.

The trailing `# vX.Y.Z` comment is required, not decoration: a bare 40-hex ref
is unreadable, and without it nobody can tell a current pin from one three
majors behind by looking. It is a comment, so it is checked for presence and
shape only -- every assertion about what actually *runs* is phrased against the
ref itself, per #731.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

#: `- uses: owner/repo@ref` or `  uses: owner/repo@ref`, with anything after it.
#: A commented-out line cannot match: `#` is neither whitespace nor `-`.
_USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s+(\S+)\s*(.*)$")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: The version the sha is claimed to be, e.g. `# v7.0.1`.
_VERSION_COMMENT_RE = re.compile(r"#\s*v\d+\.\d+\.\d+(?:\s|$)")


def _uses_lines() -> list[tuple[str, int, str, str]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _USES_RE.match(line)
            if match:
                found.append((path.name, number, match.group(1), match.group(2)))
    return found


def test_the_discovery_actually_finds_the_action_references() -> None:
    """A parser that finds nothing renders every guard below green while
    checking no action at all -- the #557 shape, and the only way this file
    can be wrong in a direction that matters."""
    found = _uses_lines()
    names = sorted({name for name, _, _, _ in found})
    assert len(names) >= 3, (
        f"expected `uses:` references in at least three workflow files, found "
        f"{names} -- the parser or the workflow layout changed and this file "
        "is now checking nothing")
    assert len(found) >= 8, (
        f"only {len(found)} `uses:` references found under {WORKFLOWS} -- too "
        "few to be the real set")


def test_every_action_is_pinned_to_a_commit_sha() -> None:
    for name, number, ref, _ in _uses_lines():
        _, _, version = ref.partition("@")
        assert version, f"{name}:{number}: `uses: {ref}` declares no ref at all"
        assert _SHA_RE.match(version), (
            f"{name}:{number}: `uses: {ref}` is pinned to the moving ref "
            f"{version!r}, not to a 40-character commit sha. A publisher "
            "repointing that tag runs new code with this workflow's token on "
            "the very next run, and Dependabot cannot see it happen.")


def test_every_sha_pin_says_which_version_it_is() -> None:
    for name, number, ref, trailing in _uses_lines():
        assert _VERSION_COMMENT_RE.search(trailing), (
            f"{name}:{number}: `uses: {ref}` carries no `# vX.Y.Z` comment. A "
            "bare sha is unreadable: nobody can tell a current pin from a "
            "three-majors-stale one, and Dependabot writes this comment when "
            "it bumps, so an absent one goes stale the first time it does.")
