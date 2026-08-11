"""#927's guard anchors at column 0; CommonMark does not (#930).

#923 was closed by refusing fragment lines that would become a heading or a
link-reference definition in the released `CHANGELOG.md`. Every pattern in
`_FORBIDDEN_LINE` is anchored at column 0 and matches `[Unreleased]:`
case-sensitively. CommonMark agrees with neither:

- An ATX heading and a link-reference definition each allow **0-3 leading
  spaces**. Four is the cut-off, because four is an indented code block.
- Link **labels are case-insensitive**, and the **first definition wins**.
  Fragment bodies are inserted near the top of the file and the genuine link
  refs are at the bottom, so a planted definition beats the real one.
- The label set is not two names. Any `[label]:` line is a definition.

Three bypasses, pinned separately below, each of which produced `ok` on both
disclosure surfaces while the released file carried the injected lines.

The boundary this file also pins, in both directions:

- **Spaces 0-3 are refused, 4+ are not.** Four spaces is the remedy, and it is
  a real one: to CommonMark it is an indented code block, and to `_anchor`,
  `_unreleased_span` and `_link_ref_block` it is not a heading and not a link
  ref. The old message prescribed *two* spaces, which is inside the bypass.
- **A leading tab is not refused**, and `test_a_leading_tab_is_already_the_remedy`
  is why: a tab advances to the next four-column tab stop, so a tab-indented
  line is already at four columns or more. Adding it to the pattern would refuse
  a line that is safe, and every character class added here is one an author
  trips over writing an ordinary entry.
- **Still not fence-aware**, for #927's reason unchanged: nothing downstream is.

Would these tests pass if the code did nothing? No — every one of the first
group was run against `master` @ 9106fb1 first and failed there, asserting
either a refusal naming file and line, or the absence of an injected line from
a `CHANGELOG.md` read back off disk.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "assemble_changelog.py"

_spec = importlib.util.spec_from_file_location("assemble_changelog_930", SCRIPT)
assert _spec is not None and _spec.loader is not None
asm = importlib.util.module_from_spec(_spec)
sys.modules["assemble_changelog_930"] = asm
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
    # A fixture body says nothing about issue numbers, and #1251 requires
    # the entry to name its own. The helper asks the assembler what counts.
    from _changelog_fragment_fixture import with_self_reference
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    for name, body in fragments.items():
        (frag_dir / name).write_text(
            with_self_reference(name, body), encoding="utf-8")
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
# 1. Bypass one — 1 to 3 leading spaces
# ---------------------------------------------------------------------------

def test_a_heading_indented_by_one_to_three_spaces_is_still_a_heading(
        tmp_path, capsys) -> None:
    """CommonMark allows 0-3 spaces before `#`. The guard allowed 1-3 through."""
    for spaces in (1, 2, 3):
        changelog, frag_dir = _repo(tmp_path / str(spaces), {
            "999.fixed.md": (
                "- **Innocent looking fix** ([#999](x)). Prose.\n"
                "\n"
                + " " * spaces + "## [0.26.0] - 2020-01-01\n"
            ),
        })
        before = changelog.read_text(encoding="utf-8")

        code, out = _cut(capsys, changelog, frag_dir)

        assert code == asm.REFUSED, "{0} space(s): {1}".format(spaces, out)
        assert "999.fixed.md:3" in out, out
        assert changelog.read_text(encoding="utf-8") == before, \
            "a refused run must write nothing"


def test_a_link_ref_indented_by_two_spaces_is_still_a_definition(
        tmp_path, capsys) -> None:
    """The old refusal prescribed exactly this indent as the remedy."""
    changelog, frag_dir = _repo(tmp_path, {
        "999.fixed.md": (
            "- **Innocent looking fix** ([#999](x)). Prose.\n"
            "\n"
            "  [0.26.0]: " + EVIL + "/releases/tag/v0.26.0\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)

    assert code == asm.REFUSED, out
    assert "999.fixed.md:3" in out, out
    assert EVIL not in changelog.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. Bypass two — the label is case-insensitive, and first definition wins
# ---------------------------------------------------------------------------

def test_a_lowercase_unreleased_label_is_the_same_link_reference(
        tmp_path, capsys) -> None:
    """`[unreleased]:` and `[Unreleased]:` are one label to any CommonMark reader.

    The fragment lands above the genuine block, and the first definition of a
    label wins, so the lowercase copy is the one the document resolves.
    """
    changelog, frag_dir = _repo(tmp_path, {
        "999.fixed.md": (
            "- **Innocent looking fix** ([#999](x)). Prose.\n"
            "\n"
            "[unreleased]: " + EVIL + "/compare/v0.0.1...HEAD\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)
    text = changelog.read_text(encoding="utf-8")

    assert code == asm.REFUSED, out
    assert "999.fixed.md:3" in out, out
    assert EVIL not in text
    assert "[Unreleased]: " + GENUINE + "/compare/v0.23.0...HEAD" in text


# ---------------------------------------------------------------------------
# 3. Bypass three — a label the pattern never enumerated
# ---------------------------------------------------------------------------

def test_a_link_ref_with_any_other_label_is_refused(tmp_path, capsys) -> None:
    """`Unreleased` and `x.y.z` are not the label set — every `[label]:` is one.

    A fragment defining `[docs]:` redefines that label for the whole released
    document from a position above wherever it is genuinely defined.
    """
    changelog, frag_dir = _repo(tmp_path, {
        "999.fixed.md": (
            "- **Innocent looking fix** ([#999](x)). Prose.\n"
            "\n"
            "[docs]: " + EVIL + "/pwn\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)

    assert code == asm.REFUSED, out
    assert "999.fixed.md:3" in out, out
    assert EVIL not in changelog.read_text(encoding="utf-8")


def test_a_label_whose_destination_is_on_the_next_line_is_refused(
        tmp_path, capsys) -> None:
    """CommonMark lets the destination follow on the next line.

    So requiring a URL on the same line to call it a definition would reopen
    the hole one newline wide.
    """
    changelog, frag_dir = _repo(tmp_path, {
        "999.fixed.md": (
            "- **Innocent looking fix** ([#999](x)). Prose.\n"
            "\n"
            "[unreleased]:\n"
            "    " + EVIL + "/compare/v0.0.1...HEAD\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)

    assert code == asm.REFUSED, out
    assert "999.fixed.md:3" in out, out


# ---------------------------------------------------------------------------
# 4. The issue's reproduction, end to end, on both surfaces
# ---------------------------------------------------------------------------

_REPRO = (
    "- ordinary entry.\n"
    "\n"
    "[unreleased]: https://evil.example/pwn\n"
    "   ## [Unreleased]\n"
    "  [0.26.0]: https://evil.example/pwn2\n"
)


def test_the_reported_reproduction_is_refused_by_check(tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {"999.fixed.md": _REPRO})

    code, out = _run(capsys, "--check", "--changelog", str(changelog),
                     "--dir", str(frag_dir))

    assert code == asm.REFUSED, out
    assert "999.fixed.md:3" in out, out


def test_the_reported_reproduction_never_reaches_the_released_file(
        tmp_path, capsys) -> None:
    changelog, frag_dir = _repo(tmp_path, {"999.fixed.md": _REPRO})
    before = changelog.read_text(encoding="utf-8")

    code, out = _cut(capsys, changelog, frag_dir, version="0.26.0")
    text = changelog.read_text(encoding="utf-8")

    assert code == asm.REFUSED, out
    assert "evil.example" not in text, \
        "the guard reported ok while the released file carried the injection"
    assert text == before
    assert (frag_dir / "999.fixed.md").exists()


# ---------------------------------------------------------------------------
# 5. The receipt — "no body writes at column 0" was true while this happened
# ---------------------------------------------------------------------------

def test_the_ok_receipt_does_not_claim_a_column_it_no_longer_means(capsys, tmp_path) -> None:
    """A receipt that names the *implementation* outlives the implementation.

    `no body writes at column 0` stayed literally true through every bypass
    above, which is the defect as much as the pattern was. The claim has to be
    the property the reader cares about: the body cannot become a heading or a
    link reference of the released file.
    """
    _, frag_dir = _repo(tmp_path, {
        "906.added.md": "- **An ordinary entry** ([#906](x)). Prose.\n",
    })

    code, out = _run(capsys, "--check", "--dir", str(frag_dir))

    assert code == asm.OK, out
    assert "column 0" not in out, \
        "the receipt states where the old pattern was anchored, not what was checked"
    assert "heading" in out and "link ref" in out, out


def test_the_refusal_prescribes_a_remedy_that_is_one(tmp_path, capsys) -> None:
    """The old advice — "indent it by two spaces" — walked into the bypass."""
    changelog, frag_dir = _repo(tmp_path, {
        "999.fixed.md": "- **X** ([#999](x)).\n\n## [0.26.0] - 2020-01-01\n",
    })

    code, out = _cut(capsys, changelog, frag_dir)

    assert code == asm.REFUSED, out
    # This asserted "four spaces" once, and "two spaces" before that. Both are
    # live headings and live link refs inside a `- ` bullet, which is the only
    # container a fragment has (#934) — so the message must prescribe no indent
    # at all. It names a fence, which a real parser agrees is inert.
    assert "Indent it by" not in out, "the message prescribes an indent again"
    assert "fenced code block" in out, out


# ---------------------------------------------------------------------------
# 6. The boundary, pinned from the permissive side
# ---------------------------------------------------------------------------

def test_four_spaces_is_the_remedy_and_it_ships(tmp_path, capsys) -> None:
    """Four is where CommonMark stops reading a heading and starts reading code."""
    changelog, frag_dir = _repo(tmp_path, {
        "918.fixed.md": (
            "- **Backfilled the historical link refs** ([#918](x)). It now ends with:\n"
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


def test_a_leading_tab_is_refused_because_it_was_never_the_remedy(tmp_path, capsys) -> None:
    """This test asserted the opposite, and it was wrong (#934).

    The reasoning was that a tab advances to the next four-column tab stop, so a
    tab-indented line is at four columns or more — an indented code block. The
    arithmetic is right and the frame is not: four columns is the threshold at
    the *top level* of a document, and CommonMark measures it relative to the
    containing block's content column. Every fragment is a `- ` bullet, content
    column 2, so a tab reaches the second relative column: a live paragraph.

    Rendered through markdown-it-py, the exact body below produces an `<h1>`,
    and the tab-indented `[Unreleased]:` variant resolves the label to the
    attacker's URL. `tests/test_changelog_fragment_whitelist_934.py` renders
    both. So the pin is inverted rather than deleted — a test that blessed a
    live heading is the record of how this was missed — and the whitelist
    refuses tabs outright.
    """
    changelog, frag_dir = _repo(tmp_path, {
        "918.fixed.md": (
            "- **Quotes a heading with a tab** ([#918](x)):\n"
            "\n"
            "\t## [0.26.0] - 2020-01-01\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)

    assert code == asm.REFUSED, out
    assert "918.fixed.md:3" in out, out
    assert changelog.read_text(encoding="utf-8") == CHANGELOG, \
        "a refused cut must leave CHANGELOG.md alone"


def test_ordinary_entries_are_not_refused(tmp_path, capsys) -> None:
    """Every class the widened pattern could newly trip over, in one entry.

    A guard that cries wolf gets bypassed rather than obeyed, so the false
    positives are pinned as hard as the true ones.
    """
    changelog, frag_dir = _repo(tmp_path, {
        "906.added.md": (
            "- **An ordinary entry** ([#906](x)) with an inline `## [0.1.0]` mention\n"
            "  and a [link][ref] and a #hash and a bare [bracket] and 4 > 3.\n"
            "\n"
            "  Fixed the `[foo]: url` syntax, mid-sentence, at an indent.\n"
            "\n"
            "  - a nested bullet\n"
            "  > a block quote\n"
        ),
    })

    code, out = _cut(capsys, changelog, frag_dir)
    text = changelog.read_text(encoding="utf-8")

    assert code == asm.OK, out
    assert "a bare [bracket]" in text
    assert "a #hash" in text


# ---------------------------------------------------------------------------
# 7. The same class of gap, one layer down: `_link_ref_block`
# ---------------------------------------------------------------------------

def test_the_link_ref_block_recognises_an_indented_definition(tmp_path, capsys) -> None:
    """`_LINK_REF_RE` is column-0 anchored too, and it decides the block bounds.

    A genuine link ref written with 1-3 spaces — a hand edit, a formatter — is
    a definition to CommonMark and not one to the backward walk that finds the
    trailing block. The walk stops at it, so the block is truncated or empty and
    the release advances no link at all. That fails closed rather than open, but
    it ships a `## [x.y.z]` heading whose link resolves to nothing while the
    receipt states only that no compare line was found.
    """
    indented_tail = CHANGELOG.replace(
        "[0.23.0]: " + GENUINE + "/releases/tag/v0.23.0",
        "  [0.23.0]: " + GENUINE + "/releases/tag/v0.23.0",
    )
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **F** ([#906](x)). Body.\n"},
                                changelog=indented_tail)

    code, out = _cut(capsys, changelog, frag_dir)
    text = changelog.read_text(encoding="utf-8")

    assert code == asm.OK, out
    assert "[Unreleased]: " + GENUINE + "/compare/v0.24.0...HEAD" in text, \
        "the genuine compare link is inside the block and must advance"
    assert "[0.24.0]: " + GENUINE + "/releases/tag/v0.24.0" in text


def test_an_indented_decoy_inside_the_block_does_not_capture_the_rewrite(
        tmp_path, capsys) -> None:
    """Recognising an indented line as *part of* the block is not permission to write it.

    The assembler emits its compare line at column 0; `_UNRELEASED_LINK_RE`
    stays anchored there, so widening the block bounds cannot hand the rewrite
    to an indented line that merely looks like the one it wrote.

    **The verdict changed in #936, the mechanism did not.** The rewrite still
    lands on the genuine line and nowhere else — asserted directly below. What
    the *release* now does is refuse, because it re-parses the file it is about
    to write and the decoy is a live definition sitting above the genuine one:
    first definition wins, so `[Unreleased]` in the released document resolves
    to `evil.example` whatever this rewrite did. Exiting 0 printed
    `links [Unreleased] → compare/v0.24.0...HEAD` over a file where it does
    not. The decoy is pre-existing in CHANGELOG.md and the refusal says so.
    """
    decoyed = CHANGELOG.replace(
        "[Unreleased]: " + GENUINE + "/compare/v0.23.0...HEAD",
        "  [Unreleased]: " + EVIL + "/compare/v0.0.1...HEAD\n"
        "[Unreleased]: " + GENUINE + "/compare/v0.23.0...HEAD",
    )
    changelog, frag_dir = _repo(tmp_path, {"906.added.md": "- **F** ([#906](x)). Body.\n"},
                                changelog=decoyed)

    code, out = _cut(capsys, changelog, frag_dir)

    assert code == asm.REFUSED, out
    assert "no fragment introduced it" in out, \
        "the refusal blames the fragments for a line already in the file:\n" + out
    assert changelog.read_text(encoding="utf-8") == decoyed

    lines = decoyed.splitlines()
    assert asm._rewrite_links(lines, "0.24.0") is not None
    text = "\n".join(lines)
    assert "[0.24.0]: " + GENUINE + "/releases/tag/v0.24.0" in text
    assert EVIL + "/releases/tag/v0.24.0" not in text
    assert "[Unreleased]: " + GENUINE + "/compare/v0.24.0...HEAD" in text


# ---------------------------------------------------------------------------
# 8. What this repo ships today
# ---------------------------------------------------------------------------

def test_the_live_fragment_directory_passes_the_widened_rule(capsys) -> None:
    """A contract change that refuses the work already written is not shippable."""
    code, out = _run(capsys, "--check", "--dir", str(REPO / "changelog.d"))

    assert code == asm.OK, out
