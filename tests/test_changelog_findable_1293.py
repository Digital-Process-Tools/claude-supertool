"""A repository file must not name a fragment the next release will delete.

`assemble_changelog.py` consumes `changelog.d/<n>.<section>.md` at the tag, so
any reference keyed to a fragment path is green for exactly the window between
the PR that writes it and the release that consumes it, and red forever after.
The window is invisible from inside the PR that adds it, because the file is
present the whole time that PR's CI runs.

Three instances have shipped. #941 reddened five legs on v0.26.0 and #953
thirteen of twenty on v0.27.0; both were an `assert` on an existence call
against the checkout's own fragment, and both are closed by the AST detector in
`test_changelog_findable_1053.py`. #1231 was not an assertion at all --- a
module-level tuple of swept paths held the fragment and a `read_text` in a loop
resolved it, so the detector could not see it, and the v0.33.0 release commit
went red on 13 of 22 legs for a reason unrelated to what the test guarded.

So the shape is not "an assert" and not "a Python expression": it is *any file
in the repository naming a fragment that is currently pending*. That is what
this checks, over every tracked text file, in any language --- a doc example, a
workflow comment, a jit-context citation, a fixture. It is keyed to the
fragments actually on disk, which is what makes it precise: a reference to an
already-consumed number (`906.added.md` in a doc example, `999.fixed.md` in a
`tmp_path` fixture) names nothing the next tag can delete and is not a finding.
185 lines across 19 tracked files do that, and every one of them is correct.

Would these pass if the code did nothing? No. The detector tests feed it #1231's
shipped tuple verbatim and require a finding on its line; the negative tests put
the same text beside an *absent* fragment and require silence, so a detector
that flagged every fragment-shaped literal fails them. The live sweep skips
rather than passes when there is nothing pending or when git cannot list the
checkout, because a scan that looked at nothing must not read as a clean sheet.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from _changelog_findable import (pending_fragment_references,
                                 pending_fragments, tracked_files)

REPO = Path(__file__).resolve().parents[1]

# `_FIGURE_BEARING` as `tests/test_ops_roster_1231.py` shipped it, plus the loop
# that resolved it against the checkout. Neither line is an `assert`, and
# `read_text` is not an existence call, which is why the #1053 detector is blind
# to it. Assembled rather than written whole so this module is not itself a
# reference to a fragment path. For the same reason the fixtures below use
# numbers this repo has not reached: a fixture literal equal to *this* PR's own
# issue number would be flagged by the guard under test, which is the one false
# positive it has, and the finding names the remedy.
SHIPPED_1231 = (
    "_FIGURE_BEARING = (\n"
    '    "_supertool.py", "README.md",\n'
    '    "changelog.d/' "1231.added.md\",\n"
    ")\n"
    "\n"
    "def test_quoted_byte_figures_are_this_checkouts():\n"
    "    for rel in _FIGURE_BEARING:\n"
    '        text = (REPO_ROOT / rel).read_text(encoding="utf-8")\n'
)

# #941's shape, which the #1053 detector also refuses. Both guards seeing it is
# the point: this one is keyed to the file on disk rather than to the syntax.
SHIPPED_941 = (
    "def test_a_changelog_fragment_exists() -> None:\n"
    '    assert (ROOT / "changelog.d" / "' "941.added.md\").is_file()\n"
)


def _tree(tmp_path: Path, pending=(), files=None) -> Path:
    """A checkout with `pending` fragments and `files` elsewhere in the tree."""
    root = tmp_path / "repo"
    (root / "changelog.d").mkdir(parents=True)
    (root / "changelog.d" / "README.md").write_text(
        "# changelog.d\n", encoding="utf-8")
    for name in pending:
        (root / "changelog.d" / name).write_text("- entry\n", encoding="utf-8")
    for rel, body in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _scan(root: Path):
    files = sorted(path.relative_to(root)
                   for path in root.rglob("*") if path.is_file())
    return pending_fragment_references(root, files)


# -- what is pending -------------------------------------------------------

def test_the_readme_is_not_a_fragment(tmp_path):
    """It is the only permanent file in the directory, and the assembler
    already exempts it. A guard that called it pending would name it on every
    run and never go quiet."""
    assert pending_fragments(_tree(tmp_path)) == []


def test_every_section_and_a_slug_are_pending(tmp_path):
    root = _tree(tmp_path, pending=[
        "1.security.md", "2.added.md", "3.changed.md", "4.deprecated.md",
        "5.removed.md", "6.fixed.md", "7.fixed.a-slug.md"])
    assert len(pending_fragments(root)) == 7


def test_a_file_that_is_not_a_fragment_is_not_pending(tmp_path):
    root = _tree(tmp_path)
    (root / "changelog.d" / "notes.md").write_text("x", encoding="utf-8")
    (root / "changelog.d" / "8.improved.md").write_text("x", encoding="utf-8")
    assert pending_fragments(root) == []


def test_no_changelog_d_at_all_is_empty_not_an_error(tmp_path):
    assert pending_fragments(tmp_path / "bare") == []


# -- the detector ----------------------------------------------------------

def test_the_shape_that_reddened_the_release_is_a_finding(tmp_path):
    root = _tree(tmp_path, pending=["1231.added.md"],
                 files={"tests/test_ops_roster_1231.py": SHIPPED_1231})
    findings = _scan(root)
    assert len(findings) == 1, findings
    assert "tests/test_ops_roster_1231.py:3" in findings[0], findings[0]
    assert "1231.added.md" in findings[0], findings[0]


def test_the_assertion_shape_is_a_finding_too(tmp_path):
    root = _tree(tmp_path, pending=["941.added.md"],
                 files={"tests/test_x.py": SHIPPED_941})
    assert len(_scan(root)) == 1


def test_the_same_text_beside_a_consumed_fragment_is_silent(tmp_path):
    """The discriminator, and the reason this is keyed to disk rather than to
    syntax: `changelog.d/1231.added.md` in a comment or a doc example names
    nothing the next tag deletes. A detector that flagged every
    fragment-shaped literal would refuse 185 correct lines in this repo."""
    root = _tree(tmp_path, files={"tests/test_ops_roster_1231.py": SHIPPED_1231})
    assert _scan(root) == []


def test_a_hermetic_fixture_naming_another_number_is_silent(tmp_path):
    root = _tree(tmp_path, pending=["4242.fixed.md"], files={
        "tests/test_changelog_fragments_906.py":
            'changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- x"})'})
    assert _scan(root) == []


def test_the_windows_spelling_is_a_finding(tmp_path):
    """A backslash-spelled literal is still a path, and matching on the bare
    filename is what makes the separator irrelevant."""
    root = _tree(tmp_path, pending=["953.added.md"], files={
        "docs/x.md": r"see changelog.d\953.added.md for the entry"})
    assert len(_scan(root)) == 1


def test_a_longer_number_ending_in_the_pending_one_is_not_a_finding(tmp_path):
    """`1.added.md` occurs inside `21.added.md`, so a bare substring test would
    report a correct reference to a consumed fragment as a pending one. The
    match carries a left digit boundary for exactly this."""
    root = _tree(tmp_path, pending=["1.added.md"], files={
        "docs/x.md": "see changelog.d/21.added.md for the entry"})
    assert _scan(root) == []


def test_the_number_it_actually_names_is_still_a_finding(tmp_path):
    """The other half of the boundary: a guard tightened until it matches
    nothing is the absence this repo files most."""
    root = _tree(tmp_path, pending=["21.added.md"], files={
        "docs/x.md": "see changelog.d/21.added.md for the entry"})
    assert len(_scan(root)) == 1


def test_a_bare_filename_with_no_directory_is_still_a_finding(tmp_path):
    """#1231's tuple could as easily have been written without the directory,
    and the file it resolves to is the same one the tag deletes."""
    root = _tree(tmp_path, pending=["1231.added.md"], files={
        "tests/t.py": 'SWEPT = ("README.md", "1231.added.md")'})
    assert len(_scan(root)) == 1


def test_the_fragment_directory_is_not_scanned(tmp_path):
    """A fragment naming a sibling fragment is harmless: the tag consumes both
    in the same commit. Scanning the directory would make every fragment that
    quotes a filename --- the README does it three times --- a finding."""
    root = _tree(tmp_path, pending=["4242.fixed.md", "4243.added.md"])
    (root / "changelog.d" / "4243.added.md").write_text(
        "- see 4242.fixed.md\n", encoding="utf-8")
    assert _scan(root) == []


def test_a_finding_names_the_line_and_the_remedy(tmp_path):
    root = _tree(tmp_path, pending=["1231.added.md"],
                 files={"tests/test_ops_roster_1231.py": SHIPPED_1231})
    finding = _scan(root)[0]
    line = int(finding.split(":")[1])
    assert "1231.added.md" in SHIPPED_1231.splitlines()[line - 1]
    assert "CHANGELOG.md" in finding, (
        "a finding that does not name where the prose lands permanently sends "
        "the author hunting: " + finding)


def test_every_reference_is_reported_not_just_the_first(tmp_path):
    root = _tree(tmp_path, pending=["1231.added.md"], files={
        "a.md": "changelog.d/1231.added.md\n\nand again 1231.added.md\n",
        "b.md": "1231.added.md\n"})
    assert len(_scan(root)) == 3


def test_a_binary_file_is_skipped_rather_than_crashing(tmp_path):
    root = _tree(tmp_path, pending=["1231.added.md"])
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00binary")
    assert _scan(root) == []


def test_a_file_git_lists_but_disk_does_not_have_is_skipped(tmp_path):
    """A deleted-but-staged path is a real state of a working tree, and a
    FileNotFoundError from the scanner would read as a product failure."""
    root = _tree(tmp_path, pending=["1231.added.md"])
    assert pending_fragment_references(root, [Path("gone.md")]) == []


# -- this checkout ---------------------------------------------------------

def test_git_missing_is_unknown_rather_than_an_empty_file_list(tmp_path):
    """Not a git repository, so there is no answer. `None` says so; `[]` would
    be the tool's absence read as an absence in the world, and every caller
    would report a clean sweep of nothing."""
    assert tracked_files(tmp_path) is None


def test_this_checkout_lists_its_own_files():
    files = tracked_files(REPO)
    if files is None:
        pytest.skip("git could not list this checkout")
    names = {path.as_posix() for path in files}
    assert "tests/_changelog_findable.py" in names
    # 880 tracked at the time of writing. A floor near the real number, because
    # `> 10` also passes with the listing silently truncated.
    assert len(files) >= 800, len(files)


def test_no_file_in_this_checkout_names_a_pending_fragment():
    pending = pending_fragments(REPO)
    if not pending:
        pytest.skip(
            "no fragment is pending, so the next tag deletes nothing and "
            "there is nothing this could be keyed to")
    files = tracked_files(REPO)
    if files is None:
        pytest.skip("git could not list this checkout")
    findings = pending_fragment_references(REPO, files)
    assert findings == [], "\n".join(findings)
