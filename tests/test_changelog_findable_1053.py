"""The third instance of "a test asserts its own changelog.d fragment" (#1053).

`assemble_changelog.py` **consumes** fragments by design, so a test asserting a
bare `changelog.d/<n>.<section>.md` path fails on the first release after it is
merged — on every platform at once, blocking the release, and saying nothing
about the release. #941 hit it on the v0.26.0 tag; #953 hit it on v0.27.0. Both
were fixed where they were found and nowhere else, which is why there is a
third.

Two halves, because detection alone does not stop a fourth author writing the
reasonable-looking wrong thing:

- `assert_change_is_findable(issue)` makes the correct form the *easy* form —
  one call, the fragment-or-CHANGELOG rule implemented once, and a failure
  message that names both accepted states.
- `fragment_existence_assertions` refuses the wrong form at test time, so the
  next instance is caught by CI rather than by a release.

Would these pass if the code did nothing? No. The detector tests feed it the
literal source #941 and #953 shipped and require a finding naming the line; the
helper tests build throwaway trees in each of the three states and require the
right verdict in each. The repo-wide sweep additionally fails if it scans no
files at all, because a guard that looked at nothing must not read as clean.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from _changelog_findable import (assert_change_is_findable,
                                 fragment_existence_assertions, scan_test_tree,
                                 suite_modules)

REPO = Path(__file__).resolve().parents[1]

# The two shapes that actually shipped, verbatim in substance.
SHAPE_941 = """
def test_a_changelog_fragment_exists() -> None:
    assert (ROOT / "changelog.d" / "941.added.md").is_file()
"""

SHAPE_953 = """
def test_dashboard_is_registered_and_documented():
    assert (root / "changelog.d" / "953.added.md").is_file()
"""

SHAPE_GLOB = """
def test_it_is_documented():
    assert list((ROOT / "changelog.d").glob("1053.*.md"))
"""

SHAPE_GLOB_VIA_NAME = """
def test_it_is_documented():
    fragments = list((ROOT / "changelog.d").glob("1053.*.md"))
    assert fragments
"""

SHAPE_EXISTS = """
def test_it_is_documented():
    assert (ROOT / "changelog.d" / "1053.fixed.md").exists(), "no fragment"
"""

# The directory is bound on one line and looked up on the next, so neither
# statement alone carries both halves of the shape.
SHAPE_SPLIT_STATEMENT = """
def test_it_is_documented():
    frag_dir = ROOT / "changelog.d"
    assert frag_dir.glob("1053.*.md")
"""

SHAPE_SPLIT_IS_FILE = """
def test_it_is_documented():
    frag_dir = ROOT / "changelog.d"
    assert (frag_dir / "1053.added.md").is_file()
"""

# Binds a name from a path that names the directory and looks nothing up.
# `tests/test_changelog_fragment_whitelist_934.py` does exactly this and its
# assertion is correct and release-proof; a detector that merged the two taint
# sets would refuse it.
READS_THE_README = """
def test_the_readme_prescribes_the_indent():
    readme = (REPO / "changelog.d" / "README.md").read_text(encoding="utf-8")
    assert "four-space" not in readme, "still prescribes the old injection"
"""

# The assembler's own suites, in shape: a module-level helper names the
# directory under `tmp_path`, and the tests then assert what a refused or a
# `--keep` run left there. Nothing in that survives a release badly, because
# nothing in it is the repository's own `changelog.d`. Seven real assertions
# across four files have this shape, and a scope-insensitive detector refuses
# every one of them.
ASSEMBLER_FIXTURE = """
def test_readme_is_ignored_rather_than_refused(tmp_path, capsys):
    changelog, frag_dir = _repo(tmp_path, {
        "README.md": "# changelog.d\\n\\nHow this works.\\n",
        "906.added.md": "- x\\n",
    })
    assert (frag_dir / "README.md").exists(), "the directory's doc must survive"


def _repo(tmp_path, fragments):
    root = tmp_path / "repo"
    frag_dir = root / "changelog.d"
    frag_dir.mkdir(parents=True)
    return root / "CHANGELOG.md", frag_dir


def test_a_refused_run_consumes_nothing(tmp_path, capsys):
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- x\\n"})
    assert (frag_dir / "906.added.md").exists(), "a refused run must consume nothing"
"""

ACCEPTED_941 = """
def test_a_changelog_fragment_exists() -> None:
    fragments = list((ROOT / "changelog.d").glob("941.*.md"))
    if fragments:
        return
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "941" in changelog, (
        "#941 is neither a pending changelog.d/941.<section>.md fragment nor "
        "an entry in CHANGELOG.md")
"""

UNRELATED = """
def test_the_directory_is_mentioned(capsys):
    out = capsys.readouterr().out
    assert "changelog.d" in out
    assert (ROOT / "docs" / "presets" / "git.md").is_file()
"""


# ── the detector ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("source,label", [
    (SHAPE_941, "941 is_file"),
    (SHAPE_953, "953 is_file"),
    (SHAPE_GLOB, "glob asserted inline"),
    (SHAPE_GLOB_VIA_NAME, "glob bound to a name, then asserted"),
    (SHAPE_EXISTS, "exists() with a message"),
    (SHAPE_SPLIT_STATEMENT, "directory bound on one line, globbed on the next"),
    (SHAPE_SPLIT_IS_FILE, "directory bound on one line, is_file() on the next"),
])
def test_the_detector_refuses_every_shipped_and_near_shape(source, label):
    findings = fragment_existence_assertions(source, "tests/test_example.py")
    assert len(findings) == 1, (label, findings)
    assert "tests/test_example.py:" in findings[0]
    assert "assert_change_is_findable" in findings[0], (
        "a finding that does not name the accepted form sends the author "
        "hunting: " + findings[0])


@pytest.mark.parametrize("source,label", [
    (ACCEPTED_941, "#941's accepted fragment-or-CHANGELOG shape"),
    (UNRELATED, "an unrelated existence assertion and a substring check"),
    (READS_THE_README, "a name bound to a changelog.d path that looks nothing up"),
    (ASSEMBLER_FIXTURE, "a tmp_path fixture directory bound by a sibling helper"),
    ("", "an empty module"),
])
def test_the_detector_accepts_what_survives_a_release(source, label):
    assert fragment_existence_assertions(source, "tests/test_example.py") == [], label


def test_a_finding_carries_the_line_of_the_assert():
    findings = fragment_existence_assertions(SHAPE_941, "tests/t.py")
    line = int(findings[0].split(":")[1])
    assert SHAPE_941.splitlines()[line - 1].strip().startswith("assert ")


def test_the_detector_does_not_read_the_assertion_message():
    """#941's accepted form names `changelog.d/941.<section>.md` in its own
    failure message. Scanning the message rather than the asserted expression
    would refuse the very shape this issue is prescribing."""
    assert "changelog.d" in ACCEPTED_941
    assert fragment_existence_assertions(ACCEPTED_941, "tests/t.py") == []


# ── the sweep ────────────────────────────────────────────────────────────

def test_the_suite_holds_no_instance_of_the_class():
    findings = scan_test_tree(REPO)
    assert findings == [], "\n".join(findings)


def test_the_sweep_would_notice_if_it_scanned_nothing():
    """An empty scan is the defect this repo files most: a tool's absence read
    as an absence in the world. It has to be a hard failure, not a pass."""
    with pytest.raises(AssertionError, match="scanned no test files"):
        scan_test_tree(REPO / "docs")


def test_the_sweep_covers_every_test_module():
    found = {path.name for path in suite_modules(REPO)}
    assert Path(__file__).name in found
    assert "test_changelog_blank_before_heading_1113.py" in found
    # 474 at the time of writing. The floor is close to the real number on
    # purpose: `> 100` also passes with three quarters of the suite silently
    # unscanned, which is the shape of the bug this test exists to catch.
    # Raise it when it starts failing for the boring reason.
    assert len(found) >= 450, len(found)


# ── the helper ───────────────────────────────────────────────────────────

def _tree(tmp_path: Path, fragments=(), changelog: str = "# Changelog\n") -> Path:
    root = tmp_path / "repo"
    (root / "changelog.d").mkdir(parents=True)
    for name in fragments:
        (root / "changelog.d" / name).write_text("- entry\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return root


def test_the_helper_accepts_a_pending_fragment(tmp_path):
    root = _tree(tmp_path, fragments=["1053.added.md"])
    assert_change_is_findable(1053, root)


def test_the_helper_accepts_any_section_and_any_slug(tmp_path):
    root = _tree(tmp_path, fragments=["1053.fixed.the-slug.md"])
    assert_change_is_findable(1053, root)


def test_the_helper_accepts_a_released_entry_after_the_fragment_is_consumed(tmp_path):
    root = _tree(tmp_path, changelog="# Changelog\n\n- entry ([#1053](x)). Body.\n")
    assert_change_is_findable(1053, root)


def test_the_helper_does_not_accept_another_issues_fragment(tmp_path):
    """A `return` for anything, or a glob loose enough to match a neighbour,
    both pass every accepting test above. This is what rules them out."""
    root = _tree(tmp_path, fragments=["999.added.md", "10530.added.md"])
    with pytest.raises(AssertionError, match="#1053"):
        assert_change_is_findable(1053, root)


def test_the_helper_refuses_when_the_change_is_findable_in_neither(tmp_path):
    root = _tree(tmp_path)
    with pytest.raises(AssertionError) as excinfo:
        assert_change_is_findable(1053, root)
    message = str(excinfo.value)
    assert "1053" in message
    assert "changelog.d" in message
    assert "CHANGELOG.md" in message


def test_the_helper_survives_a_missing_changelog(tmp_path):
    """A release tool that cannot find CHANGELOG.md must fail with a sentence,
    not a FileNotFoundError — the platform where that raises first is not the
    one this is written on."""
    root = tmp_path / "bare"
    (root / "changelog.d").mkdir(parents=True)
    with pytest.raises(AssertionError):
        assert_change_is_findable(1053, root)


def test_this_prs_own_changes_are_findable():
    """The trap named in #1053: whatever this builds, this PR ships fragments
    too — so these two calls are the first users of the helper, and they are
    the instance that would have been written the broken way."""
    assert_change_is_findable(1053)
    assert_change_is_findable(1113)
