"""One fragment file per change, assembled at release (#906).

Every PR appended to the same twelve lines of `CHANGELOG.md`, so every merge
re-conflicted every other open PR. `changelog.d/` removes the shared path;
`.github/scripts/assemble_changelog.py` folds the fragments into a release
section when a version is cut.

The assertions here are about the *document the assembler produces* and about
*what it says it did* — not about its exit code alone. A release tool that
writes nothing and exits 0 is the defect this repo files most often: an absence
produced by the tool read as an absence in the world. So every state the
assembler can be in has to be stated in words, and each of those words is
pinned below.

Would these tests pass if the code did nothing? No: each one either reads back
a mutated `CHANGELOG.md` or asserts a specific refusal naming a specific file.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import List

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "assemble_changelog.py"

_spec = importlib.util.spec_from_file_location("assemble_changelog", SCRIPT)
assert _spec is not None and _spec.loader is not None
asm = importlib.util.module_from_spec(_spec)
sys.modules["assemble_changelog"] = asm
_spec.loader.exec_module(asm)


# ---------------------------------------------------------------------------
# Fixtures — a changelog with a *populated* `## [Unreleased]`, which is the
# state this repo is actually in at the boundary (#906 forbids migrating it).
# ---------------------------------------------------------------------------

CHANGELOG = (
    "# Changelog\n"
    "\n"
    "All notable changes to this project will be documented in this file.\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "### Changed\n"
    "\n"
    "- **A pre-existing unreleased entry** ([#893](https://example/893)). Body.\n"
    "\n"
    "## [0.23.0] - 2026-08-05\n"
    "\n"
    "### Added\n"
    "\n"
    "- **Shipped op A** ([#800](https://example/800)). Body.\n"
    "\n"
    "[Unreleased]: https://github.com/Digital-Process-Tools/claude-supertool/compare/v0.23.0...HEAD\n"
    "[0.23.0]: https://github.com/Digital-Process-Tools/claude-supertool/releases/tag/v0.23.0\n"
)


def _repo(tmp_path: Path, fragments: dict[str, str] | None = None,
          changelog: str = CHANGELOG) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    frag_dir = root / "changelog.d"
    frag_dir.mkdir(parents=True)
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    for name, body in (fragments or {}).items():
        (frag_dir / name).write_text(body, encoding="utf-8")
    return root / "CHANGELOG.md", frag_dir


def _run(capsys, *argv: str) -> tuple[int, str]:
    code = asm.main(list(argv))
    return code, capsys.readouterr().out


def _headings(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.startswith("## ")]


def _block_of(text: str, heading: str) -> List[str]:
    """Every line of one `## ` section, heading excluded."""
    out: List[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line.strip() == heading
            continue
        if inside:
            out.append(line)
    assert inside or out, f"no such section: {heading!r}"
    return out


def _entries(text: str, heading: str) -> List[str]:
    """The top-level `- ` bullets of one section. Continuation paragraphs indent."""
    return [ln for ln in _block_of(text, heading) if ln.startswith("- ")]


def _subheadings(text: str, heading: str) -> List[str]:
    return [ln.strip() for ln in _block_of(text, heading) if ln.startswith("### ")]


def _section_of(text: str, needle: str) -> str:
    """The `## ` heading a line ends up under — the question a release gets wrong."""
    current = "(no section)"
    for line in text.splitlines():
        if line.startswith("## "):
            current = line.strip()
        elif needle in line:
            return current
    raise AssertionError(f"not found in document: {needle!r}")


# ---------------------------------------------------------------------------
# Three states, never two
# ---------------------------------------------------------------------------

def test_zero_fragments_is_stated_and_writes_nothing(tmp_path, capsys) -> None:
    """No fragments is an outcome to report, not a release to cut silently."""
    changelog, frag_dir = _repo(tmp_path)
    before = changelog.read_text(encoding="utf-8")

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code != 0, "a release that assembled nothing must not report success"
    assert "skipped" in out.lower()
    assert "changelog.d" in out
    assert changelog.read_text(encoding="utf-8") == before, "wrote to an empty release"
    assert "## [0.24.0]" not in before


def test_ok_states_what_it_read_and_what_it_wrote(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **Fragments** ([#906](x)). Body.\n"})

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == 0
    assert "ok" in out.lower()
    assert "906.added.md" in out, "the receipt must name the fragments it consumed"
    assert "0.24.0" in out


# ---------------------------------------------------------------------------
# Refusals name the file
# ---------------------------------------------------------------------------

def test_unparsable_filename_is_refused_by_name(tmp_path, capsys) -> None:
    """Skipping a file nobody asked to skip is how an entry disappears."""
    changelog, frag_dir = _repo(tmp_path, {
        "906.added.md": "- **Kept** ([#906](x)). Body.\n",
        "notes-to-self.md": "- **Lost** something.\n",
    })
    before = changelog.read_text(encoding="utf-8")

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code != 0
    assert "notes-to-self.md" in out, "a refusal that does not name the file is unactionable"
    assert changelog.read_text(encoding="utf-8") == before
    assert (frag_dir / "906.added.md").exists(), "a refused run must consume nothing"


def test_unknown_section_is_refused_by_name(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {"906.improved.md": "- **X**. Body.\n"})

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code != 0
    assert "906.improved.md" in out
    assert "improved" in out
    assert "added" in out.lower(), "the refusal must list the sections that do exist"


def test_empty_fragment_is_refused_by_name(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {"906.fixed.md": "\n   \n"})

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code != 0
    assert "906.fixed.md" in out


def test_readme_is_ignored_rather_than_refused(tmp_path, capsys) -> None:
    """The directory documents itself; that file is not a fragment."""
    changelog, frag_dir = _repo(tmp_path, {
        "README.md": "# changelog.d\n\nHow this works.\n",
        "906.added.md": "- **Fragments** ([#906](x)). Body.\n",
    })

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == 0, out
    assert (frag_dir / "README.md").exists(), "the directory's own doc must survive a release"
    assert "How this works" not in changelog.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The document it produces
# ---------------------------------------------------------------------------

def test_pre_existing_unreleased_content_is_folded_into_the_release(tmp_path, capsys) -> None:
    """`[Unreleased]` means "ships in the next release" — so it ships in it.

    Leaving it in place strands it: the tag goes out silently omitting work that
    is in the tag, and the entries sit under `[Unreleased]` afterwards as though
    they were still pending.
    """
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **Fragments** ([#906](x)). Body.\n"})

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))
    text = changelog.read_text(encoding="utf-8")

    assert code == 0, out
    assert _section_of(text, "A pre-existing unreleased entry") == "## [0.24.0] - 2026-08-07"
    assert _section_of(text, "Fragments") == "## [0.24.0] - 2026-08-07"
    assert _section_of(text, "Shipped op A") == "## [0.23.0] - 2026-08-05"
    assert _headings(text) == sorted(set(_headings(text)), key=_headings(text).index), \
        "no heading may be emitted twice — the #839 failure, one layer up"


def test_unreleased_survives_as_an_empty_anchor(tmp_path, capsys) -> None:
    """The heading stays — the `[Unreleased]:` compare link points at it."""
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **Fragments** ([#906](x)). Body.\n"})

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    text = changelog.read_text(encoding="utf-8")

    assert code == 0, out
    assert "## [Unreleased]" in _headings(text)
    assert _entries(text, "## [Unreleased]") == []
    assert _subheadings(text, "## [Unreleased]") == []


def test_residue_and_a_fragment_in_the_same_section_merge_under_one_heading(
        tmp_path, capsys) -> None:
    """The assertion that matters: one `### Changed`, not two.

    A duplicated heading is the live failure in this repo (#911), and #839 is the
    precedent — a union that emits the heading twice reparents everything that
    sat between the two copies.
    """
    changelog, frag_dir = _repo(tmp_path, {
        "906.changed.md": "- **From a fragment** ([#906](x)). Body." + chr(10),
        "907.added.md": "- **An added one** ([#907](x)). Body." + chr(10),
    })

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))
    text = changelog.read_text(encoding="utf-8")
    release = "## [0.24.0] - 2026-08-07"

    assert code == 0, out
    subs = _subheadings(text, release)
    assert subs.count("### Changed") == 1, subs
    assert subs == ["### Added", "### Changed"], subs
    assert text.index("A pre-existing unreleased entry") < text.index("From a fragment"), \
        "folded residue goes above the fragment entries"


def test_no_entry_is_lost_in_the_fold(tmp_path, capsys) -> None:
    """residue + fragments == what the release holds. Arithmetic, not vibes."""
    changelog, frag_dir = _repo(tmp_path, {
        "906.changed.md": "- **One** ([#906](x)). Body." + chr(10),
        "907.added.md": "- **Two** ([#907](x)). Body." + chr(10),
        "908.fixed.md": "- **Three** ([#908](x)). Body." + chr(10),
    })
    residue = len(_entries(CHANGELOG, "## [Unreleased]"))
    assert residue == 1, "fixture drifted"

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))
    text = changelog.read_text(encoding="utf-8")

    assert code == 0, out
    assert len(_entries(text, "## [0.24.0] - 2026-08-07")) == residue + 3
    assert len(_entries(text, "## [0.23.0] - 2026-08-05")) == 1, "an older release was touched"


def test_an_indented_continuation_paragraph_is_carried_with_its_entry(
        tmp_path, capsys) -> None:
    """Entries here run several paragraphs. Folding a bullet without its prose is loss."""
    rich = CHANGELOG.replace(
        "- **A pre-existing unreleased entry** ([#893](https://example/893)). Body." + chr(10),
        "- **A pre-existing unreleased entry** ([#893](https://example/893)). Body." + chr(10)
        + chr(10)
        + "  **The part that would go missing.** Second paragraph, same entry." + chr(10),
    )
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **F** ([#906](x)). Body." + chr(10)},
                                changelog=rich)

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))
    text = changelog.read_text(encoding="utf-8")

    assert code == 0, out
    assert _section_of(text, "The part that would go missing") == "## [0.24.0] - 2026-08-07"


def test_a_residue_subsection_outside_keep_a_changelog_is_carried_not_dropped(
        tmp_path, capsys) -> None:
    """An unrecognised heading is content, not a parse failure. Dropping it is silent."""
    rich = CHANGELOG.replace(
        "## [0.23.0] - 2026-08-05",
        "### Notes" + chr(10) * 2
        + "- **Hand-written note** under a heading the spec does not list." + chr(10) * 2
        + "## [0.23.0] - 2026-08-05",
    )
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **F** ([#906](x)). Body." + chr(10)},
                                changelog=rich)

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))
    text = changelog.read_text(encoding="utf-8")

    assert code == 0, out
    assert _section_of(text, "Hand-written note") == "## [0.24.0] - 2026-08-07"
    assert "### Notes" in _subheadings(text, "## [0.24.0] - 2026-08-07")


def test_the_receipt_says_what_it_folded(tmp_path, capsys) -> None:
    """The fold is the surprising part of the run, so it is the loudest line."""
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **Fragments** ([#906](x)). Body."
                                           + chr(10)})

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == 0, out
    assert "folded" in out.lower()
    assert "1" in out, "the count of folded entries must be in the receipt"
    assert "Unreleased" in out


def test_an_already_empty_unreleased_says_so_rather_than_going_quiet(
        tmp_path, capsys) -> None:
    """Three states here too: folded N, or nothing to fold — never silence."""
    empty = CHANGELOG.replace(
        "### Changed" + chr(10) * 2
        + "- **A pre-existing unreleased entry** ([#893](https://example/893)). Body."
        + chr(10) * 2,
        "")
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **F** ([#906](x)). Body." + chr(10)},
                                changelog=empty)

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == 0, out
    assert "folded" in out.lower()
    assert "0" in out or "empty" in out.lower()


def test_two_fragments_in_one_section_both_appear_in_a_deterministic_order(
        tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {
        "910.fixed.md": "- **Later issue** ([#910](x)). Body.\n",
        "878.fixed.md": "- **Earlier issue** ([#878](x)). Body.\n",
        "878.fixed.second-entry.md": "- **Same issue, second entry** ([#878](x)). Body.\n",
    })

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))
    text = changelog.read_text(encoding="utf-8")

    assert code == 0, out
    assert _subheadings(text, "## [0.24.0] - 2026-08-07").count("### Fixed") == 1
    for needle in ("Later issue", "Earlier issue", "Same issue, second entry"):
        assert _section_of(text, needle) == "## [0.24.0] - 2026-08-07"
    order = [text.index(n) for n in ("Earlier issue", "Same issue, second entry", "Later issue")]
    assert order == sorted(order), "fragments must order by issue number, then by slug"


def test_sections_are_emitted_in_keep_a_changelog_order(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {
        "1.security.md": "- **Sec**. Body.\n",
        "2.added.md": "- **Add**. Body.\n",
        "3.fixed.md": "- **Fix**. Body.\n",
    })

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))
    text = changelog.read_text(encoding="utf-8")

    assert code == 0, out
    assert _subheadings(text, "## [0.24.0] - 2026-08-07") == [
        "### Added", "### Changed", "### Fixed", "### Security"]
    # `### Changed` is the folded residue, and it lands in spec position rather
    # than at the top where it happened to sit under `[Unreleased]`.


def test_fragments_are_consumed_and_keep_preserves_them(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **Fragments** ([#906](x)). Body.\n"})

    code, _ = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                   "--changelog", str(changelog), "--dir", str(frag_dir))
    assert code == 0
    assert not (frag_dir / "906.added.md").exists(), "an unconsumed fragment ships twice"

    changelog, frag_dir = _repo(tmp_path / "b",
                                {"906.added.md": "- **Fragments** ([#906](x)). Body.\n"})
    code, _ = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07", "--keep",
                   "--changelog", str(changelog), "--dir", str(frag_dir))
    assert code == 0
    assert (frag_dir / "906.added.md").exists()


def test_dry_run_writes_nothing_and_says_so(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **Fragments** ([#906](x)). Body.\n"})
    before = changelog.read_text(encoding="utf-8")

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07", "--dry-run",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == 0
    assert "dry-run" in out.lower()
    assert changelog.read_text(encoding="utf-8") == before
    assert (frag_dir / "906.added.md").exists()


def test_link_refs_are_rewritten_for_the_new_version(tmp_path, capsys) -> None:
    """Four files change at release; this is the one the assembler can finish."""
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **Fragments** ([#906](x)). Body.\n"})

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))
    text = changelog.read_text(encoding="utf-8")

    assert code == 0, out
    assert "[Unreleased]: https://github.com/Digital-Process-Tools/claude-supertool/compare/v0.24.0...HEAD" in text
    assert "[0.24.0]: https://github.com/Digital-Process-Tools/claude-supertool/releases/tag/v0.24.0" in text
    assert text.count("[0.23.0]: ") == 1


def test_a_version_already_in_the_changelog_is_refused(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **Fragments** ([#906](x)). Body.\n"})
    before = changelog.read_text(encoding="utf-8")

    code, out = _run(capsys, "--version", "0.23.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code != 0
    assert "0.23.0" in out
    assert changelog.read_text(encoding="utf-8") == before


def test_prose_quoting_a_heading_does_not_count_as_that_release(tmp_path, capsys) -> None:
    """Entries here quote headings — #731's defect, one file over.

    `"## [0.24.0]" in text` is satisfied by an entry *about* the 0.24.0 heading
    as readily as by the heading, and the failure mode is a release that cannot
    be cut with no way to tell why.
    """
    quoted = CHANGELOG.replace(
        "- **Shipped op A** ([#800](https://example/800)). Body.",
        "- **Shipped op A** ([#800](https://example/800)). It renamed the\n"
        "  `## [0.24.0]` heading, quoted here so the guard has something to trip on.",
    )
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **Fragments** ([#906](x)). Body.\n"},
                                changelog=quoted)

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == 0, out
    assert _section_of(changelog.read_text(encoding="utf-8"), "Fragments") == \
        "## [0.24.0] - 2026-08-07"


def test_a_changelog_with_no_anchor_heading_is_refused_not_guessed(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path,
                                {"906.added.md": "- **Fragments** ([#906](x)). Body.\n"},
                                changelog="# Changelog\n\nNothing here yet.\n")

    code, out = _run(capsys, "--version", "0.24.0", "--date", "2026-08-07",
                     "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code != 0
    assert "refused" in out.lower()


# ---------------------------------------------------------------------------
# The two read-only modes CI and the release trigger use
# ---------------------------------------------------------------------------

def test_check_mode_states_an_empty_directory_rather_than_passing_silently(
        tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path)

    code, out = _run(capsys, "--check", "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == 0, "an empty directory is not a CI failure"
    assert "0 fragments" in out or "no fragments" in out.lower()


def test_check_mode_refuses_a_bad_name_without_writing(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {"nope.md": "- **X**. Body.\n"})
    before = changelog.read_text(encoding="utf-8")

    code, out = _run(capsys, "--check", "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code != 0
    assert "nope.md" in out
    assert changelog.read_text(encoding="utf-8") == before


def test_count_mode_prints_the_exact_number(tmp_path, capsys) -> None:
    """The auto-release trigger reads this instead of grepping for `- **` lines."""
    changelog, frag_dir = _repo(tmp_path, {
        "906.added.md": "- **A**. Body.\n",
        "910.fixed.md": "- **B**. Body.\n",
        "README.md": "# changelog.d\n",
    })

    code, out = _run(capsys, "--count", "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == 0
    assert out.strip() == "2", f"--count must print a bare integer, got {out!r}"


def test_count_mode_on_an_empty_directory_prints_zero(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path)

    code, out = _run(capsys, "--count", "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == 0
    assert out.strip() == "0"


# ---------------------------------------------------------------------------
# The filename grammar, directly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,issue,section,slug", [
    ("906.added.md", 906, "added", ""),
    ("878.fixed.second-entry.md", 878, "fixed", "second-entry"),
    ("1.security.md", 1, "security", ""),
])
def test_parse_fragment_name_accepts(name, issue, section, slug) -> None:
    frag = asm.parse_fragment_name(name)
    assert (frag.issue, frag.section, frag.slug) == (issue, section, slug)


@pytest.mark.parametrize("name", [
    "added.906.md", "906.md", "906.added.txt", "abc.added.md", "906.improved.md", ".906.added.md",
])
def test_parse_fragment_name_refuses(name) -> None:
    with pytest.raises(asm.BadFragment) as excinfo:
        asm.parse_fragment_name(name)
    assert name in str(excinfo.value), "the message must name the file"


# ---------------------------------------------------------------------------
# The CI guard, read structurally (#731: never grep a workflow for its meaning)
# ---------------------------------------------------------------------------

WORKFLOW = REPO / ".github" / "workflows" / "changelog.yml"


def _changelog_workflow_jobs() -> dict[str, str]:
    from _workflow_parse import job_blocks
    return job_blocks(WORKFLOW.read_text(encoding="utf-8"))


def test_the_changelog_check_has_its_own_workflow_file() -> None:
    """It is not a test job, and `tests.yml` is edited by everyone at once."""
    assert WORKFLOW.exists()


def test_every_job_in_it_declares_a_wall_clock_budget() -> None:
    from _workflow_parse import job_budget

    jobs = _changelog_workflow_jobs()
    assert jobs, "parsed no jobs — a parser finding nothing renders this test green"
    for name, block in jobs.items():
        assert job_budget(block) is not None, f"{name} has no timeout-minutes"


def test_the_job_actually_runs_the_assembler_check() -> None:
    from _workflow_parse import job_steps, run_blocks

    jobs = _changelog_workflow_jobs()
    runs = "\n".join(run for block in jobs.values() for run in run_blocks(job_steps(block)))
    assert runs.strip(), "parsed no run blocks"
    assert "assemble_changelog.py" in runs
    assert "--check" in runs
