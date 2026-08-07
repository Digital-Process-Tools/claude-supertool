"""The fragment guard and the reader are the same parser now (#936).

Three rounds of hand-written Markdown scanning, three bypasses, and each fix
opened the next hole:

===  ====================================================  =================
#927 blacklist anchored at column 0                        3 bypasses (#930)
#932 blacklist widened to ``^ {0,3}``, case-insensitive    6 bypasses (#934)
#934 whitelist, positional guarantee, fences parsed        3 bypasses (#936)
===  ====================================================  =================

Every one of those bypasses is the same shape: **our scanner disagreed with
CommonMark**. Column 0 versus 0-3 leading spaces, ATX versus setext, our fence
state machine versus the real one, our info-string handling versus the spec's.
That race is not winnable by patching patterns, so this file pins the end of it
— the guard is ``markdown-it-py``, which is what a reader's parser does.

**What the guard now establishes**, and it is the only thing it claims: the
fragment, parsed as CommonMark, produces no heading, no link-reference
definition and no raw HTML at any depth; every fenced block it opens is closed
inside it; and its top level is a single ``-`` bullet list, which is what
``_entry_count``'s arithmetic is counting.

**And a second, independent layer**: the assembled document is re-parsed before
it is written, and the release refuses unless its heading table is the old one
plus exactly the headings the assembler itself wrote, and its link-reference
table is the old one plus exactly the tag ref it meant to add. That layer holds
even if the per-fragment guard is wrong again — ``test_the_written_document_is
_verified_even_when_the_fragment_guard_is_disabled`` disables the first layer
entirely and the write still refuses.

Would these tests pass if the code did nothing? No. Every refusal pin below was
run against ``master`` @ ``1073d69`` first, where each produced ``ok`` on both
disclosure surfaces and the released file carried the injection; and the two
false-refusal pins failed there in the other direction. The RED output is in the
pull request.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "assemble_changelog.py"

_spec = importlib.util.spec_from_file_location("assemble_changelog_936", SCRIPT)
assert _spec is not None and _spec.loader is not None
asm = importlib.util.module_from_spec(_spec)
sys.modules["assemble_changelog_936"] = asm
_spec.loader.exec_module(asm)

markdown_it = pytest.importorskip(
    "markdown_it",
    reason="markdown-it-py is both the guard and the oracle for this file")

GENUINE = "https://github.com/Digital-Process-Tools/claude-supertool"
EVIL = "https://evil.example/attacker/repo"
BT = "`"
FENCE = BT * 3

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


# --------------------------------------------------------------------------
# The oracle: what a reader's parser makes of a *document*, not of a fragment.
# #936's instruction was to verify the released file, rendered.
# --------------------------------------------------------------------------

_RAW_HEADING_RE = re.compile(r"<h[1-6][ >]", re.I)


def _structure(text: str):
    """(heading multiset, label -> destination, rendered `<hN>` count).

    The third element is why the second `<h1>` vector needed finding: a raw
    HTML heading is not a `heading_open` token, so a token-stream oracle calls
    it inert while the reader sees a heading. What ships is the HTML.
    """
    md = markdown_it.MarkdownIt("commonmark")
    env: dict = {}
    tokens = md.parse(text, env)
    tags = len(_RAW_HEADING_RE.findall(md.render(text)))
    flat: list = []

    def _flatten(items):
        for item in items:
            flat.append(item)
            if item.children:
                _flatten(item.children)

    _flatten(tokens)
    headings = Counter()
    for index, token in enumerate(flat):
        if token.type == "heading_open":
            title = flat[index + 1].content if index + 1 < len(flat) else ""
            headings[(token.tag, title)] += 1
    refs = {label: value["href"] for label, value in env.get("references", {}).items()}
    return headings, refs, tags


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


def _assert_the_vector_is_real(body: str):
    """Prove the injection lands, by rendering the file it would produce.

    Not "would this line match a pattern" — the released document, through the
    same parser a reader uses. A pin whose vector is imaginary pins nothing,
    and three rounds of reasoning about CommonMark got it wrong three times.
    """
    before_h, before_r, before_tags = _structure(CHANGELOG)
    lines = CHANGELOG.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("## [0.23.0]"))
    forged = "\n".join(lines[:at] + ["## [0.24.0] - 2026-08-07", "", "### Fixed", ""]
                       + body.rstrip("\n").splitlines() + [""] + lines[at:]) + "\n"
    after_h, after_r, after_tags = _structure(forged)
    expected = Counter(before_h)
    expected[("h2", "[0.24.0] - 2026-08-07")] += 1
    expected[("h3", "Fixed")] += 1
    new_headings = after_h - expected
    hijacked = {k: v for k, v in after_r.items() if before_r.get(k) != v}
    raw = after_tags - before_tags - 2
    assert new_headings or hijacked or raw > 0, (
        "this vector forges nothing when rendered — it is not a vector:\n"
        + repr(body))
    return new_headings, hijacked, raw


def _assert_refused(capsys, tmp_path, body: str):
    """Both surfaces refuse, the finding names file:line, nothing is written."""
    _assert_the_vector_is_real(body)

    changelog, frag_dir = _repo(tmp_path, {"936.fixed.md": body})

    code, out = _run(capsys, "--check", "--dir", str(frag_dir))
    assert code == asm.REFUSED, "--check accepted it:\n" + out
    assert "refused" in out
    assert re.search(r"936\.fixed\.md:\d+:", out), \
        "the finding does not name file:line:\n" + out

    code, out = _cut(capsys, changelog, frag_dir)
    assert code == asm.REFUSED, "the release path accepted it:\n" + out
    assert changelog.read_text(encoding="utf-8") == CHANGELOG, \
        "CHANGELOG.md was modified by a refused cut"
    return out


def _assert_accepted(capsys, tmp_path, body: str):
    """Accepted, and the *released file* gains only what the release wrote."""
    changelog, frag_dir = _repo(tmp_path, {"936.fixed.md": body})
    code, out = _run(capsys, "--check", "--dir", str(frag_dir))
    assert code == asm.OK, "a legitimate entry was refused:\n" + out
    code, out = _cut(capsys, changelog, frag_dir)
    assert code == asm.OK, "a legitimate entry was refused at release:\n" + out

    before_h, before_r, before_tags = _structure(CHANGELOG)
    after_h, after_r, after_tags = _structure(changelog.read_text(encoding="utf-8"))
    expected = Counter(before_h)
    expected[("h2", "[0.24.0] - 2026-08-07")] += 1
    expected[("h3", "Fixed")] += 1
    assert not (after_h - expected), \
        "the released file gained a heading nobody wrote: {0}".format(after_h - expected)
    assert after_tags == before_tags + 2, \
        "the released file renders {0} heading tags, expected {1}".format(
            after_tags, before_tags + 2)
    assert set(after_r) == set(before_r) | {"0.24.0".upper()}, \
        "the released file's link-ref table changed: {0}".format(after_r)
    assert after_r["UNRELEASED"] == GENUINE + "/compare/v0.24.0...HEAD", after_r
    return changelog.read_text(encoding="utf-8"), out


# --------------------------------------------------------------------------
# #936's three vectors. All three return `ok` on 1073d69.
# --------------------------------------------------------------------------

def test_a_column_zero_line_inside_a_fence_is_refused(capsys, tmp_path):
    """#936's headline reproduction, verbatim.

    A code block takes no lazy continuation, so a column-0 line inside a fence
    inside a bullet ends the list, the item *and* the fence. Both lines parse
    at document level: an `h1`, and an `[Unreleased]` definition that captures
    the document's own heading link. `1073d69`'s scanner, with fence state
    open, `continue`d past both with no indent check and no opener check.
    """
    body = ("- entry\n"
            "\n"
            "  " + FENCE + "bash\n"
            "# INJECTED HEADING\n"
            "[Unreleased]: " + EVIL + "/pwned\n"
            "  " + FENCE + "\n")
    _assert_refused(capsys, tmp_path, body)


def test_a_release_heading_inside_a_fence_poisons_the_next_anchor(capsys, tmp_path):
    """`## [9.9.9] - 1999-01-01` at column 0 inside a fence. Class: destroys.

    It lands above the newest real release heading, so the *next* cut inserts
    against the wrong anchor and folds the wrong span.
    """
    body = ("- entry\n"
            "\n"
            "  " + FENCE + "\n"
            "## [9.9.9] - 1999-01-01\n"
            "  " + FENCE + "\n")
    _assert_refused(capsys, tmp_path, body)


def test_a_backtick_in_a_fence_info_string_is_not_a_fence(capsys, tmp_path):
    """CommonMark forbids backticks in a backtick fence's info string.

    So ```` ``` `x` ```` is an ordinary paragraph to the reader and an open
    fence to `1073d69`'s `_FENCE_RE`, which ignores the info string — and from
    there the scanner stops checking every following line, at indent 2, which
    is a bullet's content column where headings and definitions are live. The
    closing fence three lines down puts the scanner's state machine back in
    sync, so it never even reports an unclosed fence.
    """
    body = ("- entry\n"
            "\n"
            "  " + FENCE + " " + BT + "x" + BT + "\n"
            "  # Injected\n"
            "  [Unreleased]: " + EVIL + "/pwned\n"
            "  " + FENCE + "\n")
    _assert_refused(capsys, tmp_path, body)


def test_a_raw_html_heading_inside_a_paragraph_is_refused(capsys, tmp_path):
    """Found while re-deriving #936, and not in it: `<h1>` mid-line.

    `1073d69` refuses a line that *starts* with `<` and says so — "a raw HTML
    block, which renders as a heading without being one". Put the same tag
    after a word and it is `html_inline`, the line starts with a letter, and
    the guard passes it. It renders an `<h1>` in the released file exactly the
    same way. The named pattern was shadowing a second bug on its own line.
    """
    body = "- entry\n\n  Prose with <h1>Injected</h1> in it.\n"
    _assert_refused(capsys, tmp_path, body)


# --------------------------------------------------------------------------
# The false refusals. Both are legitimate entries refused by 1073d69.
# --------------------------------------------------------------------------

def test_a_bullet_opening_with_an_inline_link_is_accepted(capsys, tmp_path):
    """`- [#123](url) fixed the thing.` — `_OPENERS` matched the bare `[`.

    An inline link can never be a link-reference definition: a definition is a
    block-level construct and `[label](dest)` is not one. The 360-entry corpus
    pin passes only because the shipped file happens to contain zero `^- \\[`
    lines, so it never exercised the shape.
    """
    text, _ = _assert_accepted(capsys, tmp_path,
                               "- [#123](https://x/y) fixed the thing.\n")
    assert "[#123](https://x/y)" in text


def test_a_continuation_line_opening_with_an_inline_link_is_accepted(capsys, tmp_path):
    """The same false refusal one line down, where hard wrapping puts it."""
    body = ("- **An entry** ([#936](https://x/936)). Prose that wraps, and the\n"
            "  next line begins with a link:\n"
            "\n"
            "  [#123](https://x/y) is the follow-up.\n")
    _assert_accepted(capsys, tmp_path, body)


# --------------------------------------------------------------------------
# Every vector from rounds one and two, re-pinned against the new guard.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    # #930
    "- ordinary entry.\n\n[unreleased]: " + EVIL + "/pwn\n",
    "- ordinary entry.\n\n   ## [Unreleased]\n",
    "- ordinary entry.\n\n  [0.26.0]: " + EVIL + "/pwn2\n",
    # #934, link-reference table
    "- Fixed the thing.\n\n[Unreleased\n]: " + EVIL + "/HIJACK\n",
    "- Fixed the thing.\n\n  [Unreleased\n  ]: " + EVIL + "/HIJACK\n",
    "- Fixed the thing.\n\n  [unreleased\n  ]: " + EVIL + "/HIJACK\n",
    "- Fixed the thing.\n\n  [Unreleased\n  ]:\n  " + EVIL + "/HIJACK\n",
    "- Fixed the thing.\n\n> [Unreleased]: " + EVIL + "/HIJACK\n",
    "- Fixed the thing.\n\n    [Unreleased]: " + EVIL + "/HIJACK\n",
    "- Fixed the thing.\n\n\t[Unreleased]: " + EVIL + "/HIJACK\n",
    "- Fixed the thing.\n\n  [Unre\\]leased]: " + EVIL + "/HIJACK\n",
    "- [Unreleased]: " + EVIL + "/HIJACK\n",
    # #934, headings
    "- e\n\n    # Injected\n",
    "- e\n\n  Injected\n  ===\n",
    "- e\n\n  Injected\n  ---\n",
    "- e\n\n  <h1>Injected</h1>\n",
    "- e\n\n  > # Injected\n",
    "- # Injected\n",
    "- e\n\n  # Injected\n",
])
def test_every_earlier_round_vector_is_still_refused(capsys, tmp_path, body):
    """Rounds one and two, unchanged. The parser must not regress any of them."""
    _assert_refused(capsys, tmp_path, body)


# --------------------------------------------------------------------------
# Structure: what `_entry_count`'s arithmetic is allowed to assume.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body,why", [
    ("Just some prose, no bullet.\n", "no bullet at all"),
    ("- e\n\nloose prose at column zero\n", "a column-0 continuation"),
    ("- e\n\n text\n", "a one-space indent"),
    ("* e\n", "a `*` bullet, which `_entry_count` does not count"),
    ("- e\n\n  " + FENCE + "\n  still open\n", "an unclosed fence"),
    ("- e\n\n  " + FENCE + "\n  x\n- f\n", "a fence left open across a bullet"),
    ("1. e\n", "an ordered list"),
    ("| a | b |\n| --- | --- |\n", "a bare table at top level"),
    ("- e\n\n\tordinary looking prose\n", "a tab, of which the shipped file has none"),
])
def test_a_fragment_that_is_not_bullets_is_refused(capsys, tmp_path, body, why):
    """These forge nothing; they are refused because the shape is the contract.

    `_entry_count` counts lines beginning `- ` and the balance guard trusts
    that count to prove nothing was lost. A fragment whose top level is not a
    `-` bullet list puts the arithmetic and the document into disagreement,
    which is a lossy cut reported as a clean one. So this rule stays after the
    guard became a parser — but it is now *derived from the parse* rather than
    from a line prefix, which is why the ordered list and the bare table are
    caught too and were not before.
    """
    changelog, frag_dir = _repo(tmp_path, {"936.fixed.md": body})
    code, out = _run(capsys, "--check", "--dir", str(frag_dir))
    assert code == asm.REFUSED, "accepted {0}:\n{1}".format(why, out)
    code, out = _cut(capsys, changelog, frag_dir)
    assert code == asm.REFUSED, out
    assert changelog.read_text(encoding="utf-8") == CHANGELOG


# --------------------------------------------------------------------------
# The second layer: the written document is verified, independently.
# --------------------------------------------------------------------------

def test_the_written_document_is_verified_even_when_the_fragment_guard_is_disabled(
        capsys, tmp_path, monkeypatch):
    """The pin that survives the *next* guard defect.

    Three rounds of one guard, three bypasses. So the release no longer rests
    on the guard being right: it re-parses the document it is about to write
    and refuses unless the heading table is the old one plus the headings it
    wrote itself, and the link-ref table is the old one plus the tag ref it
    meant to add. Here the per-fragment guard is stubbed out entirely — which
    is what "the guard has a hole we have not found yet" looks like — and the
    write still refuses and still leaves CHANGELOG.md untouched.
    """
    monkeypatch.setattr(asm, "scan_fragment_body", lambda name, text: [])
    body = ("- entry\n\n  " + FENCE + "\n# INJECTED HEADING\n"
            "[Unreleased]: " + EVIL + "/pwned\n  " + FENCE + "\n")
    changelog, frag_dir = _repo(tmp_path, {"936.fixed.md": body})

    code, out = _cut(capsys, changelog, frag_dir)
    assert code == asm.REFUSED, "the document check did not fire:\n" + out
    assert changelog.read_text(encoding="utf-8") == CHANGELOG, \
        "CHANGELOG.md was written despite failing its own re-parse"
    assert "re-parse" in out or "reparse" in out or "verif" in out, out


def test_the_anchor_is_a_parsed_heading_not_a_line_that_looks_like_one(capsys, tmp_path):
    """`_anchor` used to be `line.startswith("## [")`, and #936 disproved that.

    `changelog.d/README.md` prescribes a fenced block as *the* way to quote a
    heading in an entry, so `CHANGELOG.md` will contain `## [Unreleased]` and
    `## [x.y.z]` lines inside fences by design. At column 0 inside a fence at
    column 0 they are inert to a reader and were the anchor to the assembler —
    the new section would be inserted above a quoted example, inside somebody
    else's entry.
    """
    poisoned = CHANGELOG.replace(
        "## [0.23.0] - 2026-08-05\n",
        FENCE + "\n## [9.9.9] - 1999-01-01\n" + FENCE + "\n\n## [0.23.0] - 2026-08-05\n")
    changelog, frag_dir = _repo(tmp_path, {"936.fixed.md": "- ordinary entry.\n"},
                                changelog=poisoned)
    code, out = _cut(capsys, changelog, frag_dir)
    assert code == asm.OK, out
    written = changelog.read_text(encoding="utf-8").splitlines()

    # The fenced example is `[Unreleased]` residue, so it folds into the new
    # release — intact. On `1073d69` the fenced line *was* the anchor, so the
    # whole release section was inserted between the opening fence and the
    # heading it was quoting, cutting the fence in half and turning the
    # quotation into live structure.
    quoted_at = written.index("## [9.9.9] - 1999-01-01")
    assert written[quoted_at - 1].strip() == FENCE, \
        "the release was inserted inside a fenced example:\n" + "\n".join(written[:24])
    assert written[quoted_at + 1].strip() == FENCE, \
        "the fenced example was cut in half:\n" + "\n".join(written[:24])

    new_at = written.index("## [0.24.0] - 2026-08-07")
    real_at = written.index("## [0.23.0] - 2026-08-05")
    assert new_at < real_at, "the release was not inserted above the newest release"
    headings, _, _ = _structure(changelog.read_text(encoding="utf-8"))
    assert ("h2", "[9.9.9] - 1999-01-01") not in headings, \
        "the quoted heading became a real one"


def test_a_link_ref_lookalike_inside_a_trailing_fence_is_not_the_link_block(
        capsys, tmp_path):
    """`_link_ref_block` walks up from the bottom by regex, fence-blind.

    An entry that ends the file with a fenced example of a link-ref block was
    mistaken for the block itself. On `1073d69` the failure is the quiet
    direction rather than the loud one: the backward walk stops on the closing
    fence, finds no definitions, and the release advances *no link at all*
    while reporting `links none — ... so the link refs were left alone`. The
    tag ships with a heading whose link resolves to nothing.
    """
    poisoned = CHANGELOG.rstrip("\n") + "\n\n" + FENCE + "\n[Unreleased]: " + EVIL \
        + "/compare/v0.1.0...HEAD\n" + FENCE + "\n"
    changelog, frag_dir = _repo(tmp_path, {"936.fixed.md": "- ordinary entry.\n"},
                                changelog=poisoned)
    code, out = _cut(capsys, changelog, frag_dir)
    assert code == asm.OK, out
    text = changelog.read_text(encoding="utf-8")
    assert EVIL + "/compare/v0.24.0...HEAD" not in text, \
        "the release rewrote a line inside a fenced example"
    assert EVIL + "/compare/v0.1.0...HEAD" in text, "the fenced example was mangled"
    _, refs, _ = _structure(text)
    assert refs["UNRELEASED"] == GENUINE + "/compare/v0.24.0...HEAD", \
        "the genuine compare link was not advanced: {0}".format(refs)
    assert refs["0.24.0".upper()] == GENUINE + "/releases/tag/v0.24.0", refs


# --------------------------------------------------------------------------
# Three states, applied to the gate itself: no parser means no claim.
# --------------------------------------------------------------------------

def test_without_the_parser_check_skips_and_says_so_rather_than_reporting_ok(
        capsys, tmp_path, monkeypatch):
    """A checker that cannot answer must say so, and must not exit 0.

    This is the defect class the whole tracker is about — an absence produced
    by the tool read as an absence in the world. `--check` is the reviewer's
    assurance; without a parser it has established nothing, so it reports
    `skipped`, names the reason, and exits non-zero so CI is red rather than
    green-on-nothing.
    """
    monkeypatch.setattr(asm, "_MD_IMPORT_ERROR", "ModuleNotFoundError: markdown_it")
    _, frag_dir = _repo(tmp_path, {"936.fixed.md": "- ordinary entry.\n"})
    code, out = _run(capsys, "--check", "--dir", str(frag_dir))
    assert code != asm.OK, "reported success without validating anything:\n" + out
    assert "skipped" in out, out
    assert "markdown-it-py" in out, "the receipt does not name what is missing:\n" + out
    assert " ok " not in out


def test_without_the_parser_the_release_refuses_to_write(capsys, tmp_path, monkeypatch):
    """The write is the irreversible half, so it is the one that must not guess."""
    monkeypatch.setattr(asm, "_MD_IMPORT_ERROR", "ModuleNotFoundError: markdown_it")
    changelog, frag_dir = _repo(tmp_path, {"936.fixed.md": "- ordinary entry.\n"})
    code, out = _cut(capsys, changelog, frag_dir)
    assert code != asm.OK, out
    assert "skipped" in out, out
    assert changelog.read_text(encoding="utf-8") == CHANGELOG, \
        "CHANGELOG.md was written by a run that could not validate anything"


def test_the_ci_check_installs_the_parser_it_now_depends_on():
    """The maintainer's premise was that `--check` runs where dev deps are.

    It does not: `.github/workflows/changelog.yml` is `actions/checkout` and a
    bare `python3`, with no install step at all. Left alone, the new guard
    would have reported `skipped` on every pull request — a red CI that pins
    nothing, which is the failure mode one step better than a green one.
    """
    workflow = (REPO / ".github" / "workflows" / "changelog.yml").read_text(encoding="utf-8")
    assert "markdown-it-py" in workflow, \
        "the changelog workflow does not install the parser --check requires"
    install = workflow.index("markdown-it-py")
    check = workflow.index("assemble_changelog.py --check")
    assert install < check, "the parser is installed after the step that needs it"


# --------------------------------------------------------------------------
# The receipt states what was established, and nothing else.
# --------------------------------------------------------------------------

def test_the_receipt_names_the_parser_and_claims_only_what_it_proved(capsys, tmp_path):
    """Three receipts in a row claimed exhaustiveness they did not have.

    "no body writes at column 0" was literally true through three bypasses;
    "at any indent or nesting" was true of the scanner's own model and false of
    CommonMark. The claim now has to be checkable by the reader of the receipt:
    it names the parser that made it.
    """
    _, frag_dir = _repo(tmp_path, {"936.fixed.md": "- **Ordinary** ([#1](x)). Prose.\n"})
    code, out = _run(capsys, "--check", "--dir", str(frag_dir))
    assert code == asm.OK, out
    assert "markdown-it" in out, "the receipt does not name the parser:\n" + out
    assert "column" not in out.lower(), "the receipt names a column again:\n" + out
    for overclaim in ("at any indent or nesting", "cannot", "never"):
        assert overclaim not in out.lower(), \
            "the receipt claims more than it established: {0!r}\n{1}".format(overclaim, out)


def test_the_release_receipt_reports_the_document_re_parse(capsys, tmp_path):
    """The write's receipt has to disclose the second layer ran, or it is noise."""
    _, out = _assert_accepted(capsys, tmp_path, "- **Ordinary** ([#1](x)). Prose.\n")
    assert "re-parse" in out or "verified" in out, out


# --------------------------------------------------------------------------
# The other direction: everything legitimate still assembles.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "- Documented the step.\n\n  " + FENCE + "bash\n  # install it\n  pip install supertool\n  " + FENCE + "\n",
    "- e\n\n  " + FENCE + "\n  # Injected\n  " + FENCE + "\n",
    "- e\n\n  " + FENCE + "\n  [Unreleased]: " + EVIL + "/H\n  " + FENCE + "\n",
    "- e\n\n  " + FENCE + "markdown\n  ## [Unreleased]\n  " + FENCE + "\n",
    "- **Real entry** ([#1](https://example/1)). Prose.\n\n  **Bold lead-in.** More.\n",
    "- e\n\n  - nested\n  - bullets\n\n  | a | b |\n  | --- | --- |\n  | 1 | 2 |\n",
    "- e\n\n  > an ordinary block quote\n",
    "- a\n- b\n- c\n",
    "- **Wrapped** ([#1](https://example/1)). Prose that\n  wraps at column two.\n",
])
def test_legitimate_entries_are_accepted_and_render_inert(capsys, tmp_path, body):
    """Accepted means the released document gained no heading and no hijack."""
    _assert_accepted(capsys, tmp_path, body)


def test_every_live_fragment_in_the_repo_still_passes(capsys):
    """The fragments staged for v0.26.0, unmodified, through the real guard."""
    code, out = _run(capsys, "--check", "--dir", str(REPO / "changelog.d"))
    assert code == asm.OK, "the parser guard refuses a fragment this repo shipped:\n" + out
