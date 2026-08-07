"""The link-ref table at the bottom of `CHANGELOG.md` must agree with the file (#918).

`## [0.24.0]` and `## [0.25.0]` shipped with no matching link-reference
definition, so those headings render as literal bracketed text rather than as
links, and `[Unreleased]` has twice been left comparing from a tag two releases
behind — a link that resolves, returns a real diff, and states the opposite of
the truth about what is in the reader's install.

The assembler writes one ref per cut (#906), which stops the *next* drift and
says nothing about the state it inherited. So the missing half is an audit of
the whole table, and it is the audit that is tested here rather than the current
contents of the file: `audit_link_refs` is handed documents that are wrong in
each way the release can leave them wrong, and has to name each one.

Three states, not two. A version that was never tagged has nothing to link to,
and inventing `releases/tag/vX.Y.Z` for it publishes a 404 that looks exactly
like a working link. Those versions are declared in
`assemble_changelog.UNTAGGED_RELEASES`, the declaration is itself audited (a
declared version that *does* have a ref, or that is not in the file at all, is a
finding), and the declaration cannot absorb anything from the assembler era.

Would these tests pass if the code did nothing? No — `audit_link_refs` does not
exist, and each case below asserts a specific finding naming a specific version.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import List

import supertool

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "assemble_changelog.py"

_spec = importlib.util.spec_from_file_location("assemble_changelog", SCRIPT)
assert _spec is not None and _spec.loader is not None
asm = importlib.util.module_from_spec(_spec)
sys.modules["assemble_changelog"] = asm
_spec.loader.exec_module(asm)

BASE = "https://example.invalid/o/r"


def _doc(headings: List[str], refs: List[str]) -> str:
    body = "# Changelog\n\n"
    for title in headings:
        body += "## {0}\n\nsomething shipped.\n\n".format(title)
    return body + "\n".join(refs) + "\n"


def _tag(version: str) -> str:
    return "[{0}]: {1}/releases/tag/v{0}".format(version, BASE)


def _unreleased(version: str) -> str:
    return "[Unreleased]: {0}/compare/v{1}...HEAD".format(BASE, version)


# ---------------------------------------------------------------------------
# `--check-links`, the arm that turns the audit into an exit status (#991)
# ---------------------------------------------------------------------------
#
# `audit_link_refs` is covered above in every way a release can leave the table
# wrong. `check_links` — the function the CI step actually invokes, which turns
# those findings into a receipt and one of three exit statuses — was not
# covered at all, which is how the #991 floor on `.github/scripts/` found it on
# its first real run. The audit being right is worth nothing if the arm that
# reports it returns OK regardless, and that is the same asymmetry #918 filed
# one level up: a link that resolves and states the opposite of the truth.
#
# Would these pass if the code did nothing? No — each asserts a distinct exit
# status, and `_receipt` prints a distinct word for each. A `check_links` that
# returned OK unconditionally fails three of the four.

def _write(tmp_path, body: str) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_check_links_returns_ok_on_a_table_with_nothing_wrong(
        tmp_path: Path, capsys) -> None:
    """Clean means every declared untagged release is present too.

    A document that simply omits them is not clean — a stale declaration is
    its own finding — so the fixture carries them, which is also the only way
    to reach the branch that lists them in the receipt.
    """
    untagged = sorted(asm.UNTAGGED_RELEASES)
    rc = asm.check_links(_write(tmp_path, _doc(
        ["[Unreleased]", "[0.26.0] - 2026-08-07"]
        + ["[{0}] - 2026-01-01".format(v) for v in untagged],
        [_unreleased("0.26.0"), _tag("0.26.0")])))
    out = capsys.readouterr().out
    assert rc == asm.OK, "a clean table did not report ok: " + out
    assert "ok" in out
    if untagged:
        assert "untagged" in out, (
            "the declared-untagged releases were not disclosed in the receipt")


def test_check_links_refuses_and_names_the_findings(
        tmp_path: Path, capsys) -> None:
    """A finding must reach the exit status, not only the audit's return value."""
    rc = asm.check_links(_write(tmp_path, _doc(
        ["[Unreleased]", "[0.26.0] - 2026-08-07", "[0.25.0] - 2026-08-06"],
        [_unreleased("0.26.0"), _tag("0.26.0")])))
    out = capsys.readouterr().out
    assert rc == asm.REFUSED, (
        "a release heading with no link ref did not red the step")
    assert "0.25.0" in out, "the receipt did not name the offending version"


def test_check_links_declines_when_the_file_cannot_be_read(
        tmp_path: Path, capsys) -> None:
    """Unreadable is `skipped`, never `ok`.

    A missing file read as a pass is the whole defect class: nothing was
    audited, and nothing-audited renders identically to nothing-wrong.
    """
    rc = asm.check_links(tmp_path / "no-such-changelog.md")
    out = capsys.readouterr().out
    assert rc == asm.SKIPPED, (
        "an unreadable changelog was not distinguished from a clean one")
    assert "skipped" in out and "nothing was audited" in out


def test_check_links_declines_when_there_is_nothing_to_audit(
        tmp_path: Path, capsys) -> None:
    """A document with no release sections cannot answer the question."""
    rc = asm.check_links(_write(tmp_path, "# Changelog\\n\\nnothing here yet.\\n"))
    assert rc == asm.SKIPPED
    assert "skipped" in capsys.readouterr().out


def test_the_three_exit_statuses_are_distinct() -> None:
    """Three states, not two — and not two spellings of one number.

    `ok`, `refused` and `skipped` collapsing onto a shared status is how a
    caller ends up acting on the wrong one; the distinction is the point of
    the receipt.
    """
    assert len({asm.OK, asm.REFUSED, asm.SKIPPED}) == 3


# ---------------------------------------------------------------------------
# the audit, against documents wrong in each way a release can leave them
# ---------------------------------------------------------------------------

def test_a_release_heading_with_no_link_ref_is_a_finding() -> None:
    findings = asm.audit_link_refs(_doc(
        ["[Unreleased]", "[0.26.0] - 2026-08-07", "[0.25.0] - 2026-08-06"],
        [_unreleased("0.26.0"), _tag("0.26.0")],
    ), untagged=set())
    assert len(findings) == 1, findings
    assert "0.25.0" in findings[0]
    assert "link ref" in findings[0]


def test_unreleased_comparing_from_a_superseded_tag_is_a_finding() -> None:
    findings = asm.audit_link_refs(_doc(
        ["[Unreleased]", "[0.26.0] - 2026-08-07", "[0.25.0] - 2026-08-06"],
        [_unreleased("0.24.0"), _tag("0.26.0"), _tag("0.25.0")],
    ), untagged=set())
    assert len(findings) == 1, findings
    assert "v0.24.0" in findings[0] and "0.26.0" in findings[0]


def test_a_missing_unreleased_ref_is_a_finding_not_a_pass() -> None:
    findings = asm.audit_link_refs(_doc(
        ["[Unreleased]", "[0.26.0] - 2026-08-07"], [_tag("0.26.0")],
    ), untagged=set())
    assert len(findings) == 1, findings
    assert "[Unreleased]" in findings[0]


def test_a_document_with_no_release_heading_cannot_be_audited() -> None:
    """Not "0 findings". A file the audit could not read is not a clean file."""
    try:
        asm.audit_link_refs("# Changelog\n\nnothing here yet.\n")
    except asm.CannotValidate as exc:
        assert "release heading" in str(exc)
    else:
        raise AssertionError("audited a document with nothing to audit")


def test_a_correct_table_produces_no_findings() -> None:
    assert asm.audit_link_refs(_doc(
        ["[Unreleased]", "[0.26.0] - 2026-08-07", "[0.25.0] - 2026-08-06"],
        [_unreleased("0.26.0"), _tag("0.26.0"), _tag("0.25.0")],
    ), untagged=set()) == []


# ---------------------------------------------------------------------------
# the declaration of what was never tagged is audited too
# ---------------------------------------------------------------------------

def test_a_declared_untagged_version_is_not_required_to_have_a_ref() -> None:
    assert asm.audit_link_refs(_doc(
        ["[Unreleased]", "[0.26.0] - 2026-08-07", "[0.17.0] - 2026-06-23"],
        [_unreleased("0.26.0"), _tag("0.26.0")],
    ), untagged={"0.17.0"}) == []


def test_a_declared_untagged_version_that_has_a_ref_is_a_finding() -> None:
    """The declaration says "no tag exists"; a ref says one does. One is wrong."""
    findings = asm.audit_link_refs(_doc(
        ["[Unreleased]", "[0.26.0] - 2026-08-07", "[0.17.0] - 2026-06-23"],
        [_unreleased("0.26.0"), _tag("0.26.0"), _tag("0.17.0")],
    ), untagged={"0.17.0"})
    assert len(findings) == 1, findings
    assert "0.17.0" in findings[0] and "never tagged" in findings[0]


def test_a_declared_version_absent_from_the_file_is_a_finding() -> None:
    """Otherwise the declaration silently grows into a place to hide a version."""
    findings = asm.audit_link_refs(_doc(
        ["[Unreleased]", "[0.26.0] - 2026-08-07"],
        [_unreleased("0.26.0"), _tag("0.26.0")],
    ), untagged={"0.17.0"})
    assert len(findings) == 1, findings
    assert "0.17.0" in findings[0]


# ---------------------------------------------------------------------------
# and the state of this repository's own file
# ---------------------------------------------------------------------------

CHANGELOG = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")


def test_this_repositorys_changelog_audits_clean() -> None:
    findings = asm.audit_link_refs(CHANGELOG)
    assert findings == [], "CHANGELOG.md link refs:\n" + "\n".join(findings)


def test_nothing_from_the_assembler_era_may_be_declared_untagged() -> None:
    """The assembler writes a ref for every version it cuts, so from the oldest
    tag still in the file onward there is no honest reason to be on this list.
    Without this the declaration is a place a real regression can be filed away.
    """
    floor = (0, 20, 0)
    for version in asm.UNTAGGED_RELEASES:
        parts = tuple(int(p) for p in version.split("."))
        assert parts < floor, (
            "{0} is declared as never tagged, but every version from 0.20.0 on "
            "is cut by the assembler, which writes its ref".format(version))


def test_the_newest_release_section_is_the_version_that_ships() -> None:
    """The fourth file of the release edit, tied to the other three.

    `plugin.json` and `pyproject.toml` are pinned to `supertool.VERSION`;
    `CHANGELOG.md` was pinned to nothing, so a release could bump three files
    and describe a different version in the one users read.
    """
    newest = asm.release_versions(CHANGELOG)[0]
    assert newest == supertool.VERSION, (
        "CHANGELOG.md's newest release section is [{0}] but supertool.VERSION "
        "is {1} — a release bump must move all four together".format(
            newest, supertool.VERSION))


def test_no_link_ref_points_at_a_tag_no_release_heading_claims() -> None:
    """A ref for a version the file does not document is debris that resolves."""
    _, refs, _ = asm._document_facts(CHANGELOG)
    documented = {v.upper() for v in asm.release_versions(CHANGELOG)}
    stray = sorted(label for label in refs
                   if re.fullmatch(r"\d+\.\d+\.\d+", label)
                   and label not in documented)
    assert stray == [], "link refs with no release section: {0}".format(stray)
