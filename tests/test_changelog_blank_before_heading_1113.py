"""A release section ends with a blank line before the heading below it (#1113).

`render` builds the section as a list ending in `""`, and `assemble` passed the
joined text through `str.splitlines()` — which drops the empty field a terminal
newline produces. So the section's last body line landed directly against the
previous `## [x.y.z]` heading, on every release since 0.25.0 (0.25.0, 0.26.0,
0.27.0, 0.28.0, and 0.29.0 which was tagged while this was being fixed).

CommonMark lets an ATX heading interrupt a paragraph, so GitHub renders it
correctly and nothing looked wrong. A stricter parser folds that heading into
the preceding paragraph, and the artefact that breaks is the one users read to
decide whether to upgrade.

Would these pass if the code did nothing? No — the three assembly tests read the
assembled document back and fail on the assembler as it stood before this fix.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import List, Tuple

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "assemble_changelog.py"

_spec = importlib.util.spec_from_file_location("assemble_changelog", SCRIPT)
assert _spec is not None and _spec.loader is not None
asm = importlib.util.module_from_spec(_spec)
sys.modules["assemble_changelog"] = asm
_spec.loader.exec_module(asm)

CHANGELOG = """# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed

- **A pre-existing unreleased entry** ([#893](https://example/893)). Body.

## [0.23.0] - 2026-08-05

### Added

- **Shipped op A** ([#800](https://example/800)). Body.

[Unreleased]: https://github.com/Digital-Process-Tools/claude-supertool/compare/v0.23.0...HEAD
[0.23.0]: https://github.com/Digital-Process-Tools/claude-supertool/releases/tag/v0.23.0
"""

EMPTY_UNRELEASED = CHANGELOG.replace(
    """### Changed

- **A pre-existing unreleased entry** ([#893](https://example/893)). Body.

""", "")

# No `## [Unreleased]` heading at all — the other splice in `assemble`, which
# inserts against `lines[:anchor]` with no `[""]` of its own.
NO_UNRELEASED = EMPTY_UNRELEASED.replace("## [Unreleased]\n\n", "")

FIX_FRAGMENT = "- **A fix** ([#1113](https://example/1113)). Body.\n"
ADD_FRAGMENT = "- **A second change** ([#1053](https://example/1053)). Body.\n"


def _repo(tmp_path: Path, fragments: dict, changelog: str = CHANGELOG) -> Tuple[Path, Path]:
    root = tmp_path / "repo"
    frag_dir = root / "changelog.d"
    frag_dir.mkdir(parents=True)
    # A fixture body says nothing about issue numbers, and #1251 requires
    # the entry to name its own. The helper asks the assembler what counts.
    from _changelog_fragment_fixture import with_self_reference
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    for name, body in fragments.items():
        (frag_dir / name).write_text(
            with_self_reference(name, body), encoding="utf-8")
    return root / "CHANGELOG.md", frag_dir


def crowded_headings(text: str) -> List[str]:
    """Every `## ` heading whose immediately preceding line is not blank.

    Deliberately positional and deliberately dumb: this is the property a
    stricter Markdown parser reads off the raw bytes, so reading it off the
    raw bytes is the honest check.
    """
    lines = text.splitlines()
    return [line for index, line in enumerate(lines)
            if line.startswith("## ") and index and lines[index - 1].strip()]


def test_assembled_release_is_separated_from_the_heading_below_it(tmp_path, capsys):
    changelog, frag_dir = _repo(tmp_path, {"1113.fixed.md": FIX_FRAGMENT})
    code = asm.main(["--version", "0.24.0", "--date", "2026-08-08",
                     "--changelog", str(changelog), "--dir", str(frag_dir)])
    capsys.readouterr()
    assert code == asm.OK
    text = changelog.read_text(encoding="utf-8")
    assert crowded_headings(text) == [], (
        "the assembler wrote a `## ` heading with no blank line above it: "
        + repr(crowded_headings(text)))


def test_it_holds_when_unreleased_is_present_but_empty(tmp_path, capsys):
    """The heading is still there, so this is the same splice with nothing to
    fold — not the `unreleased_at is None` branch, which is below."""
    changelog, frag_dir = _repo(tmp_path, {"1113.fixed.md": FIX_FRAGMENT},
                                changelog=EMPTY_UNRELEASED)
    code = asm.main(["--version", "0.24.0", "--date", "2026-08-08",
                     "--changelog", str(changelog), "--dir", str(frag_dir)])
    capsys.readouterr()
    assert code == asm.OK
    text = changelog.read_text(encoding="utf-8")
    assert crowded_headings(text) == [], repr(crowded_headings(text))


def test_it_holds_with_no_unreleased_heading_at_all(tmp_path, capsys):
    """`unreleased_at is None` — the branch that splices with no `[""]` of its
    own, and the one a file predating `## [Unreleased]` would take."""
    changelog, frag_dir = _repo(tmp_path, {"1113.fixed.md": FIX_FRAGMENT},
                                changelog=NO_UNRELEASED)
    assert "## [Unreleased]" not in NO_UNRELEASED
    code = asm.main(["--version", "0.24.0", "--date", "2026-08-08",
                     "--changelog", str(changelog), "--dir", str(frag_dir)])
    capsys.readouterr()
    assert code == asm.OK
    text = changelog.read_text(encoding="utf-8")
    assert crowded_headings(text) == [], repr(crowded_headings(text))


def test_two_consecutive_releases_stay_separated(tmp_path, capsys):
    """Assembling onto a file this tool already wrote must not stack blanks
    either — one blank line, not zero and not two."""
    changelog, frag_dir = _repo(tmp_path, {"1113.fixed.md": FIX_FRAGMENT})
    asm.main(["--version", "0.24.0", "--date", "2026-08-08",
              "--changelog", str(changelog), "--dir", str(frag_dir)])
    (frag_dir / "1053.added.md").write_text(ADD_FRAGMENT, encoding="utf-8")
    code = asm.main(["--version", "0.25.0", "--date", "2026-08-09",
                     "--changelog", str(changelog), "--dir", str(frag_dir)])
    capsys.readouterr()
    assert code == asm.OK
    lines = changelog.read_text(encoding="utf-8").splitlines()
    assert crowded_headings("\n".join(lines)) == []
    for index, line in enumerate(lines):
        if line.startswith("## ") and index >= 2:
            assert lines[index - 2].strip(), (
                "two blank lines above " + repr(line) + " — one is the rule")


def test_the_hand_maintained_heading_is_not_among_the_historical_instances():
    """The four historical instances are left alone, deliberately.

    Repairing them would rewrite text already published in tags and GitHub
    release notes, so the file would stop matching what shipped. The generator
    is fixed forward. `## [Unreleased]` is the one heading a contributor writes
    under by hand rather than the assembler, so it is the one that must be
    clean today as well as tomorrow.
    """
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    stale = crowded_headings(text)
    assert all("Unreleased" not in line for line in stale), repr(stale)
