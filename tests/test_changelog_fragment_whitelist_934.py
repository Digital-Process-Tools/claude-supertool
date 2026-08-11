"""The fragment guard is a whitelist now, not a blacklist (#934).

#927 refused fragment lines matching heading and link-ref patterns anchored at
column 0; #930 found three bypasses. #932 widened them to `^ {0,3}` and made
labels case-insensitive; the next audit found **six more**, plus a false
refusal, plus documentation whose prescribed remedy is itself an injection.

Enumerating what CommonMark can do is unbounded. This file pins the inversion.

**The accepted shape, in one sentence:** a fragment is one or more `- ` bullets
at column 0, every other line blank or indented at least two spaces, and — with
fenced code blocks excepted — no line may open anything but a list item, a
table row or ordinary prose.

Everything else is refused *by construction* rather than by enumeration, so a
multi-line label, a setext underline, an HTML block, a blockquote prefix and
container-relative indentation are all out without any of them being named.

**Why indentation cannot be the rule, measured rather than reasoned.** Rendered
through markdown-it-py inside a `- ` bullet (the container fragments actually
land in), a link-reference definition is live at 2, 4, 5 *and tab* indent, and
an ATX heading is live at 2, 4 and 5. #932 allowed 4+ and allowed tabs, on the
reasoning that four columns is an indented code block — which is true at the
top level of a document and false inside a list item, whose content column is
2. The threshold there is 6. Both the refusal message and `changelog.d/README.md`
prescribed four.

**Fence-awareness.** #927 and #932 both declined it, because nothing downstream
parses fences, so a fence bought no safety. That reasoning does not survive the
inversion. It was an argument about the assembler's own column-scoped scanners;
the whitelist keeps those safe by position instead (no body line ever reaches
column 0 except a `- ` bullet), which frees the fence to do the job it actually
does for the *renderer* — verified here: a heading and a link-ref definition
are both inert inside a fenced block at indent 2. So the fence becomes the
remedy for quoting a heading, replacing the four-space advice that was a live
injection, and the false refusal #934 reported (a `#` comment in a ```bash
block) is accepted instead.

Would these tests pass if the code did nothing? No. Every refusal pin below was
run against `master` @ `1a87a88` first, where each one produced `ok` and exit 0
on both disclosure surfaces while the released file carried the injected line;
and `test_a_fenced_comment_in_a_bullet_continuation_is_a_legitimate_entry` is
the false refusal, which failed there in the other direction.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "assemble_changelog.py"

_spec = importlib.util.spec_from_file_location("assemble_changelog_934", SCRIPT)
assert _spec is not None and _spec.loader is not None
asm = importlib.util.module_from_spec(_spec)
sys.modules["assemble_changelog_934"] = asm
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


def _assert_refused(capsys, tmp_path, body: str, marker: str = "evil.example"):
    """Both surfaces refuse, the finding names the line, and nothing is written."""
    changelog, frag_dir = _repo(tmp_path, {"934.fixed.md": body})

    code, out = _run(capsys, "--check", "--dir", str(frag_dir))
    assert code == asm.REFUSED, "--check accepted it:\n" + out
    assert "refused" in out
    assert "934.fixed.md:" in out, "the finding does not name file:line:\n" + out

    code, out = _cut(capsys, changelog, frag_dir)
    assert code == asm.REFUSED, "the release path accepted it:\n" + out
    assert changelog.read_text(encoding="utf-8") == CHANGELOG, \
        "CHANGELOG.md was modified by a refused cut"
    assert marker not in changelog.read_text(encoding="utf-8")
    return out


def _assert_accepted(capsys, tmp_path, body: str, name: str = "934.fixed.md"):
    changelog, frag_dir = _repo(tmp_path, {name: body})
    code, out = _run(capsys, "--check", "--dir", str(frag_dir))
    assert code == asm.OK, "a legitimate entry was refused:\n" + out
    code, out = _cut(capsys, changelog, frag_dir)
    assert code == asm.OK, "a legitimate entry was refused at release:\n" + out
    return changelog.read_text(encoding="utf-8"), out


# --------------------------------------------------------------------------
# The six link-reference vectors of #934's first table.
# --------------------------------------------------------------------------

def test_a_link_label_split_across_two_lines_is_refused(capsys, tmp_path):
    """`[Unreleased` / `]: url` — neither line matches `^ {0,3}\\[[^\\]]+\\]:`.

    The verbatim reproduction from #934: `--check` said ok, the cut exited 0,
    and `grep evil.example CHANGELOG.md` found it at line 106.
    """
    _assert_refused(capsys, tmp_path,
                    "- Fixed the thing.\n\n[Unreleased\n]: " + EVIL + "/HIJACK\n")


def test_a_split_label_in_a_bullet_continuation_is_refused(capsys, tmp_path):
    """The same label split, indented into the bullet where it is still live."""
    _assert_refused(capsys, tmp_path,
                    "- Fixed the thing.\n\n  [Unreleased\n  ]: " + EVIL + "/HIJACK\n")


def test_a_split_lowercase_label_is_refused(capsys, tmp_path):
    """Labels are case-insensitive; `[unreleased]` is the same definition."""
    _assert_refused(capsys, tmp_path,
                    "- Fixed the thing.\n\n  [unreleased\n  ]: " + EVIL + "/HIJACK\n")


def test_a_label_and_destination_both_split_is_refused(capsys, tmp_path):
    """`[Unreleased` / `]:` / `url` — the destination may sit on its own line."""
    _assert_refused(capsys, tmp_path,
                    "- Fixed the thing.\n\n  [Unreleased\n  ]:\n  " + EVIL + "/HIJACK\n")


def test_a_blockquote_prefixed_definition_is_refused(capsys, tmp_path):
    """`> [Unreleased]: url` — a container the patterns never modelled."""
    _assert_refused(capsys, tmp_path,
                    "- Fixed the thing.\n\n> [Unreleased]: " + EVIL + "/HIJACK\n")


def test_a_four_space_definition_inside_a_list_item_is_refused(capsys, tmp_path):
    """Four spaces is #932's *allowed* side, and inside a bullet it is live.

    CommonMark's four-column threshold is relative to the containing block's
    content column. A `- ` bullet's is 2, so four spaces is two relative
    columns — a paragraph, and a link-reference definition in it resolves.
    """
    _assert_refused(capsys, tmp_path,
                    "- Fixed the thing.\n\n    [Unreleased]: " + EVIL + "/HIJACK\n")


def test_a_tab_indented_definition_inside_a_list_item_is_refused(capsys, tmp_path):
    """#932 allowed tabs on the reasoning that a tab reaches column 4.

    It does, and column 4 inside a `- ` bullet is relative column 2. Rendered,
    the definition is live. The reasoning was right about the column and wrong
    about which column mattered.
    """
    _assert_refused(capsys, tmp_path,
                    "- Fixed the thing.\n\n\t[Unreleased]: " + EVIL + "/HIJACK\n")


def test_an_escaped_bracket_label_is_refused(capsys, tmp_path):
    """`[Unre\\]leased]:` — `\\[[^\\]]+\\]:` stops at the escaped bracket.

    A definition the guard's own regex cannot see, of a label the author
    chooses.
    """
    _assert_refused(capsys, tmp_path,
                    "- Fixed the thing.\n\n  [Unre\\]leased]: " + EVIL + "/HIJACK\n",
                    marker=EVIL)


def test_a_definition_as_the_bullet_content_is_refused(capsys, tmp_path):
    """`- [Unreleased]: url` — the bullet line itself carries the definition."""
    _assert_refused(capsys, tmp_path,
                    "- [Unreleased]: " + EVIL + "/HIJACK\n")


# --------------------------------------------------------------------------
# The heading vectors of #934's second table.
# --------------------------------------------------------------------------

def test_the_four_space_remedy_the_guard_prescribed_is_refused(capsys, tmp_path):
    """`- e` / blank / `    # Injected` — #934's headline reproduction.

    The refusal message told authors to do exactly this, and it produced a
    live heading. Following the guard's advice walked into the injection.
    """
    out = _assert_refused(capsys, tmp_path, "- e\n\n    # Injected\n", marker="# Injected")
    assert "934.fixed.md:3" in out, out


def test_a_setext_equals_underline_is_refused(capsys, tmp_path):
    """`===` under a paragraph is an h1; the guard modelled ATX only."""
    _assert_refused(capsys, tmp_path, "- e\n\n  Injected\n  ===\n", marker="Injected\n  ===")


def test_a_setext_dash_underline_is_refused(capsys, tmp_path):
    """`---` under a paragraph is an h2."""
    _assert_refused(capsys, tmp_path, "- e\n\n  Injected\n  ---\n", marker="Injected\n  ---")


def test_a_raw_html_heading_is_refused(capsys, tmp_path):
    """`<h1>` renders as a heading without being one to any `#` pattern."""
    _assert_refused(capsys, tmp_path, "- e\n\n  <h1>Injected</h1>\n", marker="<h1>")


def test_a_quoted_heading_is_refused(capsys, tmp_path):
    """`> # Injected` — the blockquote container, on the heading side."""
    _assert_refused(capsys, tmp_path, "- e\n\n  > # Injected\n", marker="> # Injected")


def test_a_heading_as_the_bullet_content_is_refused(capsys, tmp_path):
    """`- # Injected` renders an h1 inside the list item."""
    _assert_refused(capsys, tmp_path, "- # Injected\n", marker="# Injected")


def test_a_two_space_heading_is_still_refused(capsys, tmp_path):
    """The case #930 already covered stays covered."""
    _assert_refused(capsys, tmp_path, "- e\n\n  # Injected\n", marker="# Injected")


# --------------------------------------------------------------------------
# Structure: what is not a bullet-plus-continuation is out by construction.
# --------------------------------------------------------------------------

def test_a_fragment_that_does_not_start_with_a_bullet_is_refused(capsys, tmp_path):
    _assert_refused(capsys, tmp_path, "Just some prose, no bullet.\n", marker="Just some prose")


def test_a_column_zero_continuation_is_refused(capsys, tmp_path):
    """A lazy continuation at column 0 is where every container trick starts."""
    _assert_refused(capsys, tmp_path, "- e\n\nloose prose at column zero\n",
                    marker="loose prose")


def test_a_one_space_indent_is_refused(capsys, tmp_path):
    _assert_refused(capsys, tmp_path, "- e\n\n text\n", marker="\n text")


def test_a_tab_indented_continuation_is_refused(capsys, tmp_path):
    """Tabs are out entirely: the shipped file contains zero of them."""
    _assert_refused(capsys, tmp_path, "- e\n\n\tordinary looking prose\n",
                    marker="\tordinary")


def test_a_star_bullet_is_refused(capsys, tmp_path):
    """`* ` is a list to CommonMark and not an entry to `_entry_count`.

    Accepting it would put the balance guard's arithmetic and the document's
    structure into disagreement — a lossy cut reported as a clean one.
    """
    _assert_refused(capsys, tmp_path, "* e\n", marker="* e")


def test_an_unclosed_fence_is_refused(capsys, tmp_path):
    """An open fence swallows the rest of `CHANGELOG.md` into a code block."""
    _assert_refused(capsys, tmp_path, "- e\n\n  ```\n  still open\n", marker="still open")


def test_a_fence_left_open_across_a_new_bullet_is_refused(capsys, tmp_path):
    """A fence must close inside the bullet that opened it."""
    _assert_refused(capsys, tmp_path, "- e\n\n  ```\n  x\n- f\n", marker="```")


# --------------------------------------------------------------------------
# The other direction: legitimate entries, lifted from the shipped file.
# --------------------------------------------------------------------------

def test_a_fenced_comment_in_a_bullet_continuation_is_a_legitimate_entry(capsys, tmp_path):
    """#934's false refusal. `1a87a88` called this "a Markdown heading"."""
    body = ("- Documented the new install step.\n"
            "\n"
            "  ```bash\n"
            "  # install it\n"
            "  pip install supertool\n"
            "  ```\n")
    text, _ = _assert_accepted(capsys, tmp_path, body)
    assert "# install it" in text


def test_the_shipped_fenced_block_at_changelog_line_392_is_accepted(capsys, tmp_path):
    """Lifted verbatim from `CHANGELOG.md`. A 2-space fence inside a bullet."""
    body = ("- **`gh-run` reconciles its leg counts** ([#804](https://example/804)).\n"
            "  Caught in the act while sampling every ~2s:\n"
            "\n"
            "  ```\n"
            "  15:57:31  run_view=0   latest=0   all_distinct=14\n"
            "  15:57:39  run_view=9   latest=9   all_distinct=14\n"
            "  ```\n"
            "\n"
            "  For ~18s `gh-run` printed a tally against a fourteen-leg matrix.\n")
    _assert_accepted(capsys, tmp_path, body)


def test_the_shipped_fenced_block_at_changelog_line_548_is_accepted(capsys, tmp_path):
    body = ("- **The header printed `Status: queued`** ([#789](https://example/789)).\n"
            "  The header now leads with the tally and keeps the raw field:\n"
            "\n"
            "  ```\n"
            "  Status: in progress — 14 total: 10 passed ⚠ NOT ALL GREEN (run-level field: queued)\n"
            "  ```\n"
            "\n"
            "  **The tally and the field are not ranked.** They answer different questions.\n")
    _assert_accepted(capsys, tmp_path, body)


def test_the_shipped_fenced_block_at_changelog_line_578_is_accepted(capsys, tmp_path):
    body = ("- **The abstract read is a tree-sitter capability now** ([#670](https://example/670)).\n"
            "  The map has to earn the substitution each time:\n"
            "\n"
            "  ```\n"
            "  [abstract read skipped — no symbols found in src/rows.ts (typescript)]\n"
            "  ```\n"
            "\n"
            "  When tree-sitter is not installed the reason says so.\n")
    _assert_accepted(capsys, tmp_path, body)


def test_multiple_bullets_and_wrapped_continuations_are_accepted(capsys, tmp_path):
    """`891.added.md`'s shape: two top-level bullets, hard-wrapped at 2."""
    body = ("- **Four heavy local tests marked `slow`**\n"
            "  ([#891](https://example/891)). Three tests that cost 95s of CPU —\n"
            "  `test_fetch_timeout_gives_a_verdict_not_a_traceback` — are now slow.\n"
            "- **`--durations=25` on the main CI pytest leg** ([#891](https://example/891)).\n"
            "  Roughly 7 of the ~10.5 minutes a Windows leg takes are spent between\n"
            "  98% and 99% of the progress bar.\n")
    _assert_accepted(capsys, tmp_path, body)


def test_nested_bullets_and_tables_are_accepted(capsys, tmp_path):
    """Both appear in the shipped file at indent 2 and neither is dangerous."""
    body = ("- **An entry with structure** ([#1](https://example/1)). Prose.\n"
            "\n"
            "  - a nested bullet\n"
            "  - another\n"
            "\n"
            "  | column | column |\n"
            "  | --- | --- |\n"
            "  | 1 | 2 |\n")
    _assert_accepted(capsys, tmp_path, body)


def test_an_ordinary_block_quote_is_accepted_but_its_contents_are_checked(capsys, tmp_path):
    """The marker is stripped, not refused — what it hides is what matters.

    Refusing `>` outright would cost an author a legitimate construct for no
    safety: a quote of ordinary prose forges nothing. A quote *containing* a
    heading or a definition is live, and is refused by the same check that
    reads an unquoted line.
    """
    _assert_accepted(capsys, tmp_path, "- e\n\n  > an ordinary block quote\n")
    assert asm.scan_fragment_body("x.fixed.md", "- e\n\n  > # Injected\n")
    assert asm.scan_fragment_body("x.fixed.md", "- e\n\n  > [Unreleased]: https://evil/H\n")
    assert asm.scan_fragment_body("x.fixed.md", "- e\n\n  > > # Injected\n")


def test_bold_lead_ins_are_accepted(capsys, tmp_path):
    """887 lines of the shipped file open with `**`; none may be read as a break."""
    body = ("- **Real entry** ([#1](https://example/1)). Prose.\n"
            "\n"
            "  **A bold lead-in.** More prose.\n")
    _assert_accepted(capsys, tmp_path, body)


def test_every_live_fragment_in_the_repo_still_passes(capsys):
    """The eight fragments staged for v0.26.0, unmodified, through the real guard."""
    code, out = _run(capsys, "--check", "--dir", str(REPO / "changelog.d"))
    assert code == asm.OK, "the whitelist refuses a fragment this repo shipped:\n" + out


def test_every_entry_body_in_the_shipped_changelog_would_pass_as_a_fragment():
    """The strongest anti-false-refusal pin: the guard must accept what shipped.

    Every `- ` entry of `CHANGELOG.md`, with its continuation lines, fed back
    through `scan_fragment_body`. A rule that refuses the repo's own published
    entries is a rule authors will route around.
    """
    lines = (REPO / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("## ["))
    entries, current = [], None
    for line in lines[start:]:
        if line.startswith("## ") or line.startswith("### "):
            if current:
                entries.append(current)
            current = None
            continue
        if line.startswith("- "):
            if current:
                entries.append(current)
            current = [line]
        elif current is not None:
            if line.strip() and not line.startswith("  "):
                entries.append(current)
                current = None
            else:
                current.append(line)
    if current:
        entries.append(current)

    assert len(entries) > 300, "the entry scraper found nothing to check"
    refused = []
    for entry in entries:
        findings = asm.scan_fragment_body("shipped.fixed.md", "\n".join(entry).rstrip() + "\n")
        if findings:
            refused.append((entry[0][:90], findings[0]))
    assert not refused, "the whitelist refuses {0} shipped entries, e.g.\n{1}".format(
        len(refused), "\n".join(r[1] for r in refused[:5]))


# --------------------------------------------------------------------------
# The receipt and the remedy — #930's other half.
# --------------------------------------------------------------------------

def test_the_receipt_states_the_guarantee_not_the_mechanism(capsys, tmp_path):
    """`no body writes at column 0` was true through three bypasses (#930).

    The claim has to be the property a reviewer is trusting the gate for, and
    it must not name a column, because a column is what kept being wrong.
    """
    _, frag_dir = _repo(tmp_path, {"934.fixed.md": "- **Ordinary** ([#1](x)). Prose.\n"})
    code, out = _run(capsys, "--check", "--dir", str(frag_dir))
    assert code == asm.OK, out
    assert "column" not in out.lower(), "the receipt still names a column:\n" + out
    assert "heading" in out and "link ref" in out, out
    assert "bullet" in out.lower(), "the receipt does not state the accepted shape:\n" + out


def test_the_refusal_prescribes_a_fence_and_never_four_spaces(capsys, tmp_path):
    """The four-space advice is the injection itself (#934), verified in a bullet."""
    out = _assert_refused(capsys, tmp_path, "- e\n\n  # Injected\n", marker="# Injected")
    assert "four spaces" not in out.lower(), "the message still prescribes the bypass:\n" + out
    assert "fenced" in out.lower() or "```" in out, \
        "the message does not prescribe the remedy that works:\n" + out


def test_the_readme_prescribes_a_fence_and_never_four_spaces():
    """The README taught the attack; it must now teach the remedy.

    Asserted on the *prescriptive* forms rather than on the string "four
    spaces", which the page still contains — in the paragraph explaining why
    four spaces is wrong. Banning the words would ban the correction too.
    """
    readme = (REPO / "changelog.d" / "README.md").read_text(encoding="utf-8")
    for prescription in ("indent it\nby four spaces", "**indent it by four spaces**",
                         "Indent by four spaces", "indent it by four spaces**"):
        assert prescription not in readme, \
            "changelog.d/README.md still prescribes the four-space injection"
    assert "fenced code block" in readme, "the README does not give a remedy that works"
    assert "```" in readme


# --------------------------------------------------------------------------
# Cross-checked against a real CommonMark parser, not against reasoning.
# --------------------------------------------------------------------------

_HEADING_RE = re.compile(r"<h[1-6][ >]", re.I)

_RENDER_TRAILER = (
    "\n[Unreleased]: https://real.example/compare/v0.25.0...HEAD\n"
    "\nResolves: [Unreleased].\n"
)


def _render(body: str) -> str:
    markdown_it = pytest.importorskip(
        "markdown_it", reason="markdown-it-py is the CommonMark oracle for this file")
    return markdown_it.MarkdownIt("commonmark").render(body + _RENDER_TRAILER)


@pytest.mark.parametrize("body", [
    "- e\n\n  # Injected\n",
    "- e\n\n    # Injected\n",
    "- e\n\n  Injected\n  ===\n",
    "- e\n\n  Injected\n  ---\n",
    "- e\n\n  <h1>Injected</h1>\n",
    "- e\n\n  > # Injected\n",
    "- # Injected\n",
])
def test_every_refused_heading_vector_really_renders_a_heading(body):
    """The refusals are not superstition: each one is a heading to a parser."""
    assert _HEADING_RE.search(_render(body)), \
        "refused as a heading but renders as none: " + repr(body)
    assert asm.scan_fragment_body("x.fixed.md", body), "not refused: " + repr(body)


@pytest.mark.parametrize("body", [
    "- e\n\n  [Unreleased]: https://evil.example/H\n",
    "- e\n\n    [Unreleased]: https://evil.example/H\n",
    "- e\n\n     [Unreleased]: https://evil.example/H\n",
    "- e\n\n\t[Unreleased]: https://evil.example/H\n",
    "- e\n\n[Unreleased\n]: https://evil.example/H\n",
    "- e\n\n  [Unreleased\n  ]: https://evil.example/H\n",
    "- e\n\n  [unreleased\n  ]: https://evil.example/H\n",
    "- e\n\n  [Unreleased\n  ]:\n  https://evil.example/H\n",
    "- e\n\n> [Unreleased]: https://evil.example/H\n",
    "- [Unreleased]: https://evil.example/H\n",
])
def test_every_refused_linkref_vector_really_hijacks_the_label(body):
    """Each refused definition beats the genuine one below it, first-wins."""
    assert 'href="https://evil' in _render(body), \
        "refused as a link ref but does not resolve: " + repr(body)
    assert asm.scan_fragment_body("x.fixed.md", body), "not refused: " + repr(body)


@pytest.mark.parametrize("body", [
    "- Documented the step.\n\n  ```bash\n  # install it\n  pip install supertool\n  ```\n",
    "- e\n\n  ```\n  # Injected\n  ```\n",
    "- e\n\n  ```\n  [Unreleased]: https://evil.example/H\n  ```\n",
    "- **Real entry** ([#1](https://example/1)). Prose.\n\n  **Bold lead-in.** More.\n",
    "- e\n\n  - nested\n\n  | a | b |\n  | --- | --- |\n",
])
def test_every_accepted_entry_is_inert_to_the_parser(body):
    """Accepted means: no heading, no hijack. Both checked, not one."""
    html = _render(body)
    assert not _HEADING_RE.search(html), "accepted but renders a heading: " + repr(body)
    assert 'href="https://evil' not in html, "accepted but hijacks: " + repr(body)
    assert not asm.scan_fragment_body("x.fixed.md", body), "refused: " + repr(body)


def test_a_fence_is_what_makes_a_quoted_heading_inert_inside_a_bullet():
    """The remedy, proven in the container it is prescribed for.

    Four spaces renders a heading inside a `- ` bullet — that is the shipped
    advice and it is an injection. Six spaces is inert but nobody would guess
    it. A fence at the bullet's own indent is inert and is what every fenced
    block in the shipped `CHANGELOG.md` already does.
    """
    assert _HEADING_RE.search(_render("- e\n\n    # Injected\n")), \
        "four spaces inside a bullet is inert after all — re-check the remedy"
    assert not _HEADING_RE.search(_render("- e\n\n      # Injected\n"))
    assert not _HEADING_RE.search(_render("- e\n\n  ```\n  # Injected\n  ```\n"))
    assert not _HEADING_RE.search(_render("text\n\n    # Injected\n")), \
        "four spaces at top level is inert — which is the reasoning that shipped"
