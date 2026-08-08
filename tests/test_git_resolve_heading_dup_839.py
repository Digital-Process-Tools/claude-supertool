"""git-resolve `both` on a Keep a Changelog file — refuse when the union duplicates
a heading, disclose when forced (#839).

The union itself is not wrong: it mirrors git's `merge=union` driver, line for line.
What is wrong is applying it to a document whose meaning comes from *structure* and
then reporting only that the markers are gone. When both sides of a hunk carry the
same `## [x.y.z]` release heading, the union emits it twice — and every line that
sat between the two copies, work that has not shipped, is now parented under a
tagged release.

These assertions are deliberately about the resulting *document*: which headings it
has, and which section each entry ends up in. `<<<<<<<` being absent is the proxy
assertion that let this bug through, so it is not the assertion used here.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

_common = sys.modules["_git_common"]


# ---------------------------------------------------------------------------
# Fixtures — the shape from the rebase that filed the issue
# ---------------------------------------------------------------------------

# Both branches added an entry under `## [Unreleased]` / `### Fixed`. One of them
# has since had the `## [0.23.0]` release section cut above it, so the release
# heading now sits INSIDE the hunk, once on each side.
CHANGELOG_CONFLICT = (
    "# Changelog\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "### Fixed\n"
    "\n"
    "<<<<<<< HEAD\n"
    "- ours: fixed the marker gate\n"
    "\n"
    "## [0.23.0] - 2026-08-05\n"
    "\n"
    "### Added\n"
    "\n"
    "- ours: shipped op A\n"
    "=======\n"
    "- theirs: fixed the digest\n"
    "\n"
    "## [0.23.0] - 2026-08-05\n"
    "\n"
    "### Added\n"
    "\n"
    "- theirs: shipped op B\n"
    ">>>>>>> branch\n"
    "\n"
    "## [0.22.0] - 2026-08-01\n"
)

# The ordinary case, and the one that must keep working: two branches each added a
# bullet under the same existing heading. The heading is context, not hunk content,
# so the union is exactly right and nothing may be refused.
CHANGELOG_PLAIN_CONFLICT = (
    "# Changelog\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "### Fixed\n"
    "\n"
    "<<<<<<< HEAD\n"
    "- ours: fixed the marker gate\n"
    "=======\n"
    "- theirs: fixed the digest\n"
    ">>>>>>> branch\n"
    "\n"
    "## [0.22.0] - 2026-08-01\n"
)


# ---------------------------------------------------------------------------
# Structural reading of the produced document
# ---------------------------------------------------------------------------

def _release_headings(text: str) -> list[str]:
    """Every `## ...` heading line, in file order, duplicates included."""
    return [ln.strip() for ln in text.splitlines() if ln.startswith("## ")]


def _section_of(text: str, entry: str) -> str:
    """The `## ` heading ENTRY ends up under — the question the bug gets wrong."""
    current = "(no section)"
    for ln in text.splitlines():
        if ln.startswith("## "):
            current = ln.strip()
        elif ln.strip() == entry:
            return current
    raise AssertionError(f"entry not found in document: {entry!r}")


# ---------------------------------------------------------------------------
# git double — scoped to the calls resolve.py actually makes
# ---------------------------------------------------------------------------

def _patch_git(monkeypatch, fn) -> None:
    monkeypatch.setattr(resolve, "_git", fn)
    monkeypatch.setattr(_common, "_git", fn)


def _fake_git(calls, conflicted, staged, union_attr=()):
    def fake_git(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="".join(f"{p}\n" for p in conflicted), stderr="")
        if args[:3] == ["check-attr", "merge", "--"]:
            rows = "".join(
                f"{p}: merge: {'union' if p in union_attr else 'unspecified'}\n" for p in args[3:])
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=rows, stderr="")
        if args[:2] == ["add", "--"]:
            staged.append(args[2])
            if args[2] in conflicted:
                conflicted.remove(args[2])
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    return fake_git


def _run(monkeypatch, capsys, argv, conflicted, union_attr=()):
    calls: list[list[str]] = []
    staged: list[str] = []
    _patch_git(monkeypatch, _fake_git(calls, conflicted, staged, union_attr))
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: "validate: ok" for p in ps})
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", *argv])
    rc = resolve.main()
    return rc, capsys.readouterr().out, staged


# ---------------------------------------------------------------------------
# The hazard — what a line-level union does to a structured document
# ---------------------------------------------------------------------------

def test_union_duplicates_the_release_heading_and_reparents_unreleased_work(tmp_path) -> None:
    """Evidence, not contract: `_union_file` alone produces the broken document.

    Both consequences from the issue, asserted separately:
      1. two identical `## [0.23.0]` headings;
      2. an entry that belongs to `[Unreleased]` now sits under a tagged release.
    """
    f = tmp_path / "CHANGELOG.md"
    f.write_text(CHANGELOG_CONFLICT, encoding="utf-8")

    ok, err = resolve._union_file(str(f))
    assert ok and err == ""
    text = f.read_text(encoding="utf-8")

    assert _release_headings(text).count("## [0.23.0] - 2026-08-05") == 2
    assert _section_of(text, "- theirs: fixed the digest") == "## [0.23.0] - 2026-08-05"


# ---------------------------------------------------------------------------
# The contract — succeed with a sound document, or refuse. Never a third thing.
# ---------------------------------------------------------------------------

def test_both_never_stages_a_document_with_a_duplicated_release_heading(
        monkeypatch, capsys, tmp_path) -> None:
    """`git-resolve:both:all` on the conflicted changelog, stated as the contract.

    If the op stages the file it is asserting the document is usable, and the
    document must then hold up structurally: one release heading, and unreleased
    work still unreleased. If it cannot produce that, it must refuse and leave the
    file conflicted — the only signal git itself enforces.
    """
    f = tmp_path / "CHANGELOG.md"
    f.write_text(CHANGELOG_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])
    text = f.read_text(encoding="utf-8")

    if staged:
        headings = _release_headings(text)
        assert len(headings) == len(set(headings)), (
            f"staged a document with duplicated heading(s): {headings}")
        assert _section_of(text, "- ours: fixed the marker gate") == "## [Unreleased]"
        assert _section_of(text, "- theirs: fixed the digest") == "## [Unreleased]"
    else:
        assert rc == 1
        assert "refused" in out.lower()
        assert text == CHANGELOG_CONFLICT, "refusal must leave the file conflicted"


def test_refusal_names_the_heading_it_saw(monkeypatch, capsys, tmp_path) -> None:
    """A refusal that does not say what it saw is a refusal you override blind."""
    f = tmp_path / "CHANGELOG.md"
    f.write_text(CHANGELOG_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])

    assert rc == 1
    assert staged == []
    assert "Refused: 1" in out
    assert "## [0.23.0] - 2026-08-05" in out
    assert "force" in out  # the receipt names the way through


def test_ordinary_changelog_conflict_still_unions(monkeypatch, capsys, tmp_path) -> None:
    """The habit case — two bullets under one heading — must not be refused.

    A guard that fires on every changelog conflict is a guard that gets forced by
    reflex, which is the same defect one layer up.
    """
    f = tmp_path / "CHANGELOG.md"
    f.write_text(CHANGELOG_PLAIN_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])
    text = f.read_text(encoding="utf-8")

    assert rc == 0
    assert staged == [str(f)]
    assert "Refused" not in out
    assert _section_of(text, "- ours: fixed the marker gate") == "## [Unreleased]"
    assert _section_of(text, "- theirs: fixed the digest") == "## [Unreleased]"


def test_force_unions_anyway_and_the_tally_says_so(monkeypatch, capsys, tmp_path) -> None:
    """`force` is the way through, and using it is disclosed in the tally.

    The digest line above still reads `validate: ok` about a document with two
    release headings, so the tally has to carry the doubt the validator cannot.
    """
    f = tmp_path / "CHANGELOG.md"
    f.write_text(CHANGELOG_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all", "force"], [str(f)])
    text = f.read_text(encoding="utf-8")

    assert rc == 0
    assert staged == [str(f)]
    assert _release_headings(text).count("## [0.23.0] - 2026-08-05") == 2
    assert "heading" in out.lower()
    assert "verify" in out.lower()


def test_mixed_set_refuses_only_the_structurally_broken_file(
        monkeypatch, capsys, tmp_path) -> None:
    """Per file, never per set — same rule #744 established for source text."""
    bad = tmp_path / "CHANGELOG.md"
    bad.write_text(CHANGELOG_CONFLICT, encoding="utf-8")
    good = tmp_path / "docs" / "notes.md"
    good.parent.mkdir()
    good.write_text(CHANGELOG_PLAIN_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(bad), str(good)])

    assert rc == 1
    assert staged == [str(good)]
    assert "Refused: 1" in out
    assert bad.read_text(encoding="utf-8") == CHANGELOG_CONFLICT


def test_merge_union_attribute_does_not_bypass_the_heading_guard(
        monkeypatch, capsys, tmp_path) -> None:
    """`merge=union` answers "union this file", not "this union came out sound".

    The two questions are orthogonal, so the attribute overrides the source-text
    guard (#744) and not this one. Only `force` gets through here.
    """
    f = tmp_path / "CHANGELOG.md"
    f.write_text(CHANGELOG_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)], union_attr=(str(f),))

    assert rc == 1
    assert staged == []
    assert f.read_text(encoding="utf-8") == CHANGELOG_CONFLICT


# ---------------------------------------------------------------------------
# The detector, on its own
# ---------------------------------------------------------------------------

def test_detector_reports_only_headings_present_on_both_sides(tmp_path) -> None:
    """A heading only one side carries is not a duplication — the union keeps one."""
    f = tmp_path / "CHANGELOG.md"
    f.write_text(
        "<<<<<<< HEAD\n"
        "## [0.23.0] - 2026-08-05\n"
        "### Added\n"
        "=======\n"
        "### Added\n"
        ">>>>>>> branch\n",
        encoding="utf-8")

    # The finding names the section it is about since #911 — the same heading text
    # can be duplicated under two different parents, and reporting the bare line
    # collapsed two incidents into one.
    assert resolve._duplicated_headings(str(f)) == [
        "### Added (under ## [0.23.0] - 2026-08-05)"]


def test_detector_ignores_headings_outside_the_hunk(tmp_path) -> None:
    f = tmp_path / "CHANGELOG.md"
    f.write_text(CHANGELOG_PLAIN_CONFLICT, encoding="utf-8")

    assert resolve._duplicated_headings(str(f)) == []


def test_detector_drops_the_diff3_base_section(tmp_path) -> None:
    """diff3 `|||||||` base content is dropped by the union, so it cannot duplicate."""
    f = tmp_path / "CHANGELOG.md"
    f.write_text(
        "<<<<<<< HEAD\n"
        "## [0.23.0] - 2026-08-05\n"
        "||||||| base\n"
        "### Added\n"
        "=======\n"
        "### Added\n"
        ">>>>>>> branch\n",
        encoding="utf-8")

    assert resolve._duplicated_headings(str(f)) == []


def test_detector_is_scoped_to_markdown(tmp_path) -> None:
    """The heading grammar is markdown's. A `.txt` file gets no opinion."""
    f = tmp_path / "notes.txt"
    f.write_text(CHANGELOG_CONFLICT, encoding="utf-8")

    assert resolve._duplicated_headings(str(f)) == []
