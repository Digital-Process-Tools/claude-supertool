"""A fragment's *content* can forge a release section and hijack the link refs (#923).

`render()` inserts `frag.path.read_text().strip()` unmodified, and `--check`
validates filenames only. Two things follow from that one fact:

1. A column-0 `## [` line in a fragment becomes a real heading in the published
   `CHANGELOG.md`. It reparents every entry below it, and it is what `_anchor()`
   finds on the *next* cut. The duplicate-heading guard runs before insertion,
   so it cannot see it.
2. `_rewrite_links()` returned on its first `[Unreleased]: .../compare/v...HEAD`
   match anywhere in the document. Fragment content lands near the top; the
   genuine link refs are at the bottom. So a fragment carrying that line was
   matched first, and the shipped tag ref was written against a URL the fragment
   chose while the genuine compare link never advanced.

The receipt said `ok` for both, and the corruption is durable: the injected line
is still there on the next cut, and matches again.

The trigger that matters is not the hostile one. A fragment whose prose contains
a **fenced code block with an unindented heading** does this by accident, and
this repo's entries quote headings constantly — the script's own docstring says
so. There is no fence tracking anywhere in `_anchor` / `_unreleased_span` /
`_rewrite_links`, and adding it here would only teach this one reader about
fences while every other consumer of the file (git's merge driver included)
still reads column 0 as a heading. So the contract is positional, not semantic:
**the first four columns belong to the assembler; a fragment's own lines are
indented past them.** Which is what `changelog.d/README.md` already asked for.

The boundary in this file was originally column 0 and two spaces of remedy, and
both were wrong by CommonMark's own rules — see #930 and
`tests/test_changelog_fragment_indent_bypass_930.py`, which pins 0-3 spaces as
refused and four as the escape hatch. The tests here are unchanged apart from
that indent.

Would these tests pass if the code did nothing? No. Each either asserts a
refusal naming a file *and a line number*, or reads back a `CHANGELOG.md` and
asserts which link ref moved.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "assemble_changelog.py"

_spec = importlib.util.spec_from_file_location("assemble_changelog_923", SCRIPT)
assert _spec is not None and _spec.loader is not None
asm = importlib.util.module_from_spec(_spec)
sys.modules["assemble_changelog_923"] = asm
_spec.loader.exec_module(asm)

GENUINE = "https://github.com/Digital-Process-Tools/claude-supertool"
EVIL = "https://evil.example/attacker/repo"

CHANGELOG = (
    "# Changelog\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "## [0.23.0] - 2026-08-05\n"
    "\n"
    "### Added\n"
    "\n"
    "- **Shipped op A** ([#800](https://example/800)). Body.\n"
    "\n"
    "[Unreleased]: " + GENUINE + "/compare/v0.23.0...HEAD\n"
    "[0.23.0]: " + GENUINE + "/releases/tag/v0.23.0\n"
)


def _repo(tmp_path: Path, fragments: dict, changelog: str = CHANGELOG):
    root = tmp_path / "repo"
    frag_dir = root / "changelog.d"
    frag_dir.mkdir(parents=True)
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    for name, body in fragments.items():
        (frag_dir / name).write_text(body, encoding="utf-8")
    return root / "CHANGELOG.md", frag_dir


def _run(capsys, *argv: str):
    code = asm.main(list(argv))
    return code, capsys.readouterr().out


def _cut(capsys, changelog: Path, frag_dir: Path, version: str = "0.24.0"):
    return _run(capsys, "--version", version, "--date", "2026-08-07",
                "--changelog", str(changelog), "--dir", str(frag_dir))


def _release_headings(text: str):
    return [ln.strip() for ln in text.splitlines()
            if ln.startswith("## [") and not ln.startswith("## [Unreleased]")]


# ---------------------------------------------------------------------------
# 1. The forged heading
# ---------------------------------------------------------------------------

def test_a_column_0_release_heading_in_a_fragment_never_becomes_a_heading(
        tmp_path, capsys) -> None:
    """The hostile case, stated as a property of the document, not of the exit code."""
    changelog, frag_dir = _repo(tmp_path, {
        "999.fixed.md": (
            "- **Innocent looking fix** ([#999](x)). Prose.\n"
            "\n"
            "## [0.26.0] - 2020-01-01\n"
        ),
    })
    before = changelog.read_text(encoding="utf-8")

    code, out = _cut(capsys, changelog, frag_dir)
    text = changelog.read_text(encoding="utf-8")

    assert code == asm.REFUSED, out
    assert "## [0.26.0] - 2020-01-01" not in _release_headings(text)
    assert _release_headings(text) == ["## [0.23.0] - 2026-08-05"], \
        "a fragment supplied a release heading the releaser never typed"
    assert text == before, "a refused run must write nothing"
    assert (frag_dir / "999.fixed.md").exists(), "a refused run must consume nothing"


def test_the_refusal_names_the_file_and_the_line(tmp_path, capsys) -> None:
    """`invalid fragment` is not actionable. The author needs a file and a line."""
    changelog, frag_dir = _repo(tmp_path, {
        "999.fixed.md": (
            "- **Innocent looking fix** ([#999](x)). Prose.\n"
            "\n"
            "More prose, still fine.\n"
            "\n"
            "## [0.26.0] - 2020-01-01\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)

    assert code == asm.REFUSED, out
    assert "refused" in out.lower(), "three states, and this run is in the third one"
    assert "999.fixed.md:5" in out, out
    assert "## [0.26.0] - 2020-01-01" in out, "the refusal must quote the line it means"


def test_check_mode_finds_it_in_ci_where_the_author_is_standing(tmp_path, capsys) -> None:
    """The release path is the wrong place to learn this. `--check` runs on the PR."""
    changelog, frag_dir = _repo(tmp_path, {
        "999.fixed.md": "- **X** ([#999](x)).\n\n## [0.26.0] - 2020-01-01\n",
    })

    code, out = _run(capsys, "--check", "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == asm.REFUSED, out
    assert "999.fixed.md:3" in out, out


def test_count_mode_refuses_rather_than_counting_a_fragment_it_would_refuse(
        tmp_path, capsys) -> None:
    """`--count` drives the auto-release trigger; it must not say a bad set is fine."""
    changelog, frag_dir = _repo(tmp_path, {
        "906.added.md": "- **Fine** ([#906](x)).\n",
        "999.fixed.md": "- **X** ([#999](x)).\n\n## [0.26.0] - 2020-01-01\n",
    })

    code, _ = _run(capsys, "--count", "--changelog", str(changelog), "--dir", str(frag_dir))

    assert code == asm.REFUSED


# ---------------------------------------------------------------------------
# 2. The hijacked link refs
# ---------------------------------------------------------------------------

def test_a_fragment_carrying_an_unreleased_link_ref_is_refused(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {
        "999.fixed.md": (
            "- **Innocent looking fix** ([#999](x)). Prose.\n"
            "\n"
            "[Unreleased]: " + EVIL + "/compare/v0.0.1...HEAD\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)
    text = changelog.read_text(encoding="utf-8")

    assert code == asm.REFUSED, out
    assert "999.fixed.md:3" in out, out
    assert EVIL not in text
    assert "[Unreleased]: " + GENUINE + "/compare/v0.23.0...HEAD" in text, \
        "the genuine compare link must be untouched by a refused run"


def test_a_fragment_carrying_a_tag_link_ref_is_refused(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {
        "999.fixed.md": (
            "- **Innocent looking fix** ([#999](x)). Prose.\n"
            "\n"
            "[0.26.0]: " + EVIL + "/releases/tag/v0.26.0\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)

    assert code == asm.REFUSED, out
    assert "999.fixed.md:3" in out, out


def test_rewrite_links_anchors_to_the_bottom_link_ref_block(tmp_path, capsys) -> None:
    """Fix the second half at its own layer, not only via the refusal.

    A document that *already* carries a stray `[Unreleased]: .../compare/v...HEAD`
    line above the link-ref block — a previous bad cut, a hand edit, a quoted
    example — steered the whole rewrite. The link refs live in one block at the
    bottom of the file; that block is the only place this may write.
    """
    poisoned = CHANGELOG.replace(
        "## [0.23.0] - 2026-08-05",
        "- **An entry that quotes a link ref**, at column 0, in prose.\n"
        "\n"
        "[Unreleased]: " + EVIL + "/compare/v0.0.1...HEAD\n"
        "\n"
        "## [0.23.0] - 2026-08-05",
    )
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **F** ([#906](x)). Body.\n"},
                                changelog=poisoned)

    code, out = _cut(capsys, changelog, frag_dir)
    text = changelog.read_text(encoding="utf-8")

    assert code == asm.OK, out
    assert "[Unreleased]: " + GENUINE + "/compare/v0.24.0...HEAD" in text, \
        "the genuine compare link is the one that must advance"
    assert "[0.24.0]: " + GENUINE + "/releases/tag/v0.24.0" in text
    assert EVIL + "/releases/tag/v0.24.0" not in text, \
        "the new tag ref was written against a URL the document's prose chose"
    assert "[Unreleased]: " + EVIL + "/compare/v0.0.1...HEAD" in text, \
        "the stray line is left exactly as found — rewriting prose is a separate job (#918)"


def test_a_document_whose_only_unreleased_ref_is_outside_the_block_is_left_alone(
        tmp_path, capsys) -> None:
    """No genuine ref to advance is a stated outcome, not a silent guess at one."""
    no_block = (
        "# Changelog\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "- **Prose quoting** `[Unreleased]:` refs:\n"
        "\n"
        "[Unreleased]: " + EVIL + "/compare/v0.0.1...HEAD\n"
        "\n"
        "## [0.23.0] - 2026-08-05\n"
        "\n"
        "### Added\n"
        "\n"
        "- **Shipped op A** ([#800](https://example/800)). Body.\n"
    )
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **F** ([#906](x)). Body.\n"},
                                changelog=no_block)

    code, out = _cut(capsys, changelog, frag_dir)
    text = changelog.read_text(encoding="utf-8")

    assert code == asm.OK, out
    assert "links     none" in out, out
    assert EVIL + "/compare/v0.24.0...HEAD" not in text
    assert "[0.24.0]: " not in text


# ---------------------------------------------------------------------------
# 3. The accidental trigger — the one that will actually happen
# ---------------------------------------------------------------------------

def test_a_fenced_code_block_quoting_a_heading_at_column_0_is_refused(
        tmp_path, capsys) -> None:
    """No fence awareness anywhere downstream, so the fence buys no safety.

    This is the likely real-world trigger: an entry documenting a change *to* a
    heading, shown in a fenced example. Nothing about it is hostile, and the
    published document is corrupted exactly as hard.
    """
    changelog, frag_dir = _repo(tmp_path, {
        "918.fixed.md": (
            "- **Backfilled the historical link refs** ([#918](x)). The file now ends with:\n"
            "\n"
            "```markdown\n"
            "## [Unreleased]\n"
            "```\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)

    assert code == asm.REFUSED, out
    assert "918.fixed.md:4" in out, out
    assert "indent" in out.lower(), "a refusal without the remedy makes the author guess"


def test_the_remedy_is_indentation_and_it_ships(tmp_path, capsys) -> None:
    """The escape hatch has to exist *and* be safe, or it is just the hole again.

    Indented is what the fragment format already asks for — a bullet plus its
    indented paragraphs — and indented is what `_anchor`, `_unreleased_span` and
    `_rewrite_links` all agree is not a heading and not a link ref. The rule and
    the safety property are the same line, which is why no fence parser is needed.

    **Four spaces, not the two this test used to assert** (#930). CommonMark
    allows 0-3 leading spaces before a heading and before a link-reference
    definition, so the two-space escape hatch this file blessed was itself a
    live heading and a live link ref — the hole again, written down as the
    remedy. `tests/test_changelog_fragment_indent_bypass_930.py` pins the
    boundary from both sides.
    """
    changelog, frag_dir = _repo(tmp_path, {
        "918.fixed.md": (
            "- **Backfilled the historical link refs** ([#918](x)). The file now ends with:\n"
            "\n"
            "    ```markdown\n"
            "    ## [Unreleased]\n"
            "    [Unreleased]: " + EVIL + "/compare/v0.0.1...HEAD\n"
            "    ```\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)
    text = changelog.read_text(encoding="utf-8")

    assert code == asm.OK, out
    assert "    ## [Unreleased]" in text, "the quoted example must survive into the release"
    assert _release_headings(text) == ["## [0.24.0] - 2026-08-07", "## [0.23.0] - 2026-08-05"]
    assert "[Unreleased]: " + GENUINE + "/compare/v0.24.0...HEAD" in text
    assert "[0.24.0]: " + GENUINE + "/releases/tag/v0.24.0" in text, \
        "the indented decoy must not have captured the rewrite"


def test_an_indented_sub_heading_and_ordinary_prose_are_untouched(
        tmp_path, capsys) -> None:
    """The rule is about column 0 only. Everything a real entry does still passes."""
    changelog, frag_dir = _repo(tmp_path, {
        "906.added.md": (
            "- **An ordinary entry** ([#906](x)). Prose with an inline `## [0.1.0]` mention\n"
            "  and a [link][ref] and a #hash that is not a heading.\n"
            "\n"
            "  **A bold lead-in**, not a heading. Second paragraph.\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)
    text = changelog.read_text(encoding="utf-8")

    assert code == asm.OK, out
    assert "A bold lead-in" in text
    assert "a #hash that is not a heading" in text


# ---------------------------------------------------------------------------
# 4. The fragments this repo ships today
# ---------------------------------------------------------------------------

def test_the_live_fragment_directory_passes_the_new_rule(capsys) -> None:
    """The contract change must not refuse work already written against the old one."""
    code, out = _run(capsys, "--check", "--dir", str(REPO / "changelog.d"))

    assert code == asm.OK, out
