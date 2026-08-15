"""git-resolve `both` reported `markers: clean` over a duplicated heading (#911).

#839's guard asks "is this hunk internally self-duplicating" — the same heading
line on BOTH sides of one conflict block. The rebase that filed #911 produced the
asymmetric arrangement instead: `### Fixed` inside the hunk on one side only, its
twin sitting in the surrounding context that git had already auto-merged. The
union concatenates the two, the document comes out with two `### Fixed` under one
`## [Unreleased]`, and the receipt says `markers: clean`.

The fixtures below are the literal output of a real three-way rebase, not a
hand-built marker sandwich:

    base   `## [Unreleased]` holds only `### Added`
    master adds `### Changed` above it and `### Fixed` below it
    branch adds `### Fixed` above it

git trims `### Added` and everything after it out of the conflict as shared
context, which is precisely how the twin ends up outside the hunk.

As in #839, the assertions are about the resulting *document* — which headings it
carries under which parent — never about `<<<<<<<` being absent. The absent
marker is the proxy that let both bugs through.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_911", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

_common = sys.modules["_git_common"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# The issue's shape. `### Fixed` appears once inside the hunk (theirs) and once
# in the trailing context. #839's guard sees no heading on both sides and passes.
ASYMMETRIC_CONFLICT = (
    "# Changelog\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "<<<<<<< HEAD\n"
    "### Changed\n"
    "\n"
    "- **pre-push hook runs the suite for master** (#893)\n"
    "=======\n"
    "### Fixed\n"
    "\n"
    "- **gh-pr MISMATCH** (#850)\n"
    ">>>>>>> branch\n"
    "\n"
    "### Added\n"
    "\n"
    "- **map op on markdown** (#912)\n"
    "\n"
    "### Fixed\n"
    "\n"
    "- **git-checkout Rebase in progress** (#900)\n"
    "\n"
    "## [0.25.0] - 2026-08-06\n"
    "\n"
    "- released thing\n"
)

# #887's rebase, verified correct at the time and quoted in the issue comment:
# the hunk merges `### Changed` against `### Added` and the one `### Fixed` is
# shared context. Nothing may be refused here.
SAFE_CONFLICT = (
    "# Changelog\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "<<<<<<< HEAD\n"
    "### Changed\n"
    "\n"
    "- ours: changed a thing\n"
    "=======\n"
    "### Added\n"
    "\n"
    "- theirs: added a thing\n"
    ">>>>>>> branch\n"
    "\n"
    "### Fixed\n"
    "\n"
    "- shared: fixed a thing\n"
)

# Two bullets under one existing heading — the ordinary changelog conflict the
# union exists for.
PLAIN_CONFLICT = (
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

# `### Fixed` twice in the union, but under two different release headings —
# which is what every changelog on earth looks like. Refusing this trains the
# override, so it must union.
REPEATED_ACROSS_RELEASES = (
    "# Changelog\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "<<<<<<< HEAD\n"
    "### Fixed\n"
    "\n"
    "- ours: fixed the gate\n"
    "=======\n"
    "### Changed\n"
    "\n"
    "- theirs: changed the gate\n"
    ">>>>>>> branch\n"
    "\n"
    "## [0.22.0] - 2026-08-01\n"
    "\n"
    "### Fixed\n"
    "\n"
    "- shipped: fixed something else\n"
)


# ---------------------------------------------------------------------------
# Structural reading of the produced document
# ---------------------------------------------------------------------------

def _headings_under(text: str, parent: str) -> list[str]:
    """Every `### ` heading under release heading PARENT, in file order."""
    out: list[str] = []
    current = "(none)"
    for ln in text.splitlines():
        if ln.startswith("## "):
            current = ln.strip()
        elif ln.startswith("### ") and current == parent:
            out.append(ln.strip())
    return out


def _section_of(text: str, entry: str) -> str:
    """The `### ` heading ENTRY ends up under."""
    current = "(no section)"
    for ln in text.splitlines():
        if ln.startswith("### "):
            current = ln.strip()
        elif ln.strip() == entry:
            return current
    raise AssertionError(f"entry not found in document: {entry!r}")


# ---------------------------------------------------------------------------
# git double — scoped to the calls resolve.py actually makes
# ---------------------------------------------------------------------------

def _fake_git(conflicted: list[str], staged: list[str]):
    def fake_git(args, timeout=10):
        done = subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0,
                                               stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(
                args=args, returncode=0,
                stdout="".join(p + chr(0) for p in conflicted), stderr="")
        if args[:3] == ["check-attr", "merge", "--"]:
            rows = "".join(f"{p}: merge: unspecified\n" for p in args[3:])
            return subprocess.CompletedProcess(args=args, returncode=0,
                                               stdout=rows, stderr="")
        if args[:2] == ["add", "--"]:
            staged.append(args[2])
            if args[2] in conflicted:
                conflicted.remove(args[2])
            return done
        return done
    return fake_git


def _run(monkeypatch, capsys, argv, conflicted):
    staged: list[str] = []
    fake = _fake_git(conflicted, staged)
    monkeypatch.setattr(resolve, "_git", fake)
    monkeypatch.setattr(_common, "_git", fake)
    monkeypatch.setattr(resolve, "_validate_paths",
                        lambda ps: {p: "validate: ok" for p in ps})
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", *argv])
    rc = resolve.main()
    return rc, capsys.readouterr().out, staged


def _write(tmp_path: Path, text: str) -> Path:
    f = tmp_path / "CHANGELOG.md"
    f.write_text(text, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# The hazard
# ---------------------------------------------------------------------------

def test_union_alone_duplicates_a_heading_whose_twin_is_outside_the_hunk(tmp_path) -> None:
    """Evidence, not contract: the union produces the broken document.

    Two `### Fixed` under one `## [Unreleased]`, and the #850 entry orphaned
    above `### Added` — the file dump from the issue, line for line.
    """
    f = _write(tmp_path, ASYMMETRIC_CONFLICT)

    ok, err = resolve._union_file(str(f))
    assert ok and err == ""
    text = f.read_text(encoding="utf-8")

    assert _headings_under(text, "## [Unreleased]").count("### Fixed") == 2
    assert _section_of(text, "- **gh-pr MISMATCH** (#850)") == "### Fixed"


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

def test_both_never_stages_a_document_with_two_fixed_in_one_release(
        monkeypatch, capsys, tmp_path) -> None:
    """Staging is an assertion that the document is usable. It must hold up.

    Either the op produces one `### Fixed` under `## [Unreleased]`, or it refuses
    and leaves the file conflicted. `markers: clean` over a document with two is
    the third thing, and the third thing is the bug.
    """
    f = _write(tmp_path, ASYMMETRIC_CONFLICT)

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])
    text = f.read_text(encoding="utf-8")

    if staged:
        under = _headings_under(text, "## [Unreleased]")
        assert len(under) == len(set(under)), (
            f"staged a document with duplicated heading(s): {under}")
    else:
        assert rc == 1
        assert "refused" in out.lower()
        assert "markers: clean" not in out
        assert text == ASYMMETRIC_CONFLICT, "a refusal must leave the file conflicted"


def test_the_refusal_names_the_heading_it_saw(monkeypatch, capsys, tmp_path) -> None:
    """A refusal that does not say what it found is one you override blind."""
    _write(tmp_path, ASYMMETRIC_CONFLICT)
    f = tmp_path / "CHANGELOG.md"

    _, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])
    assert not staged, out
    assert "### Fixed" in out, out


def test_a_selected_block_resolve_refuses_the_same_arrangement(
        monkeypatch, capsys, tmp_path) -> None:
    """The partial path shares the guard, so it must share the answer."""
    f = _write(tmp_path, ASYMMETRIC_CONFLICT)

    rc, out, staged = _run(monkeypatch, capsys, ["both", str(f), "1"], [str(f)])
    assert not staged, out
    assert rc == 1
    assert f.read_text(encoding="utf-8") == ASYMMETRIC_CONFLICT


def test_force_still_unions_the_arrangement(monkeypatch, capsys, tmp_path) -> None:
    """`force` is the override, and it must keep working — with disclosure."""
    f = _write(tmp_path, ASYMMETRIC_CONFLICT)

    _, out, staged = _run(monkeypatch, capsys, ["both", "all", "force"], [str(f)])
    assert staged == [str(f)], out
    assert _headings_under(f.read_text(encoding="utf-8"),
                           "## [Unreleased]").count("### Fixed") == 2


# ---------------------------------------------------------------------------
# The cases that must stay quiet — a guard that fires on everything is a guard
# nobody reads
# ---------------------------------------------------------------------------

def test_the_887_arrangement_still_unions(monkeypatch, capsys, tmp_path) -> None:
    """Verified correct on `fix/887` and quoted in the issue: one of each."""
    f = _write(tmp_path, SAFE_CONFLICT)

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])
    assert rc == 0 and staged == [str(f)], out
    assert _headings_under(f.read_text(encoding="utf-8"), "## [Unreleased]") == [
        "### Changed", "### Added", "### Fixed"]


def test_two_bullets_under_one_heading_still_unions(monkeypatch, capsys, tmp_path) -> None:
    f = _write(tmp_path, PLAIN_CONFLICT)

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])
    assert rc == 0 and staged == [str(f)], out
    text = f.read_text(encoding="utf-8")
    assert "- ours: fixed the marker gate" in text
    assert "- theirs: fixed the digest" in text


def test_the_same_heading_under_two_releases_still_unions(
        monkeypatch, capsys, tmp_path) -> None:
    """`### Fixed` once per release section is every changelog, not a defect.

    The union adds a second `### Fixed` to the file and none to any one release,
    so a file-wide heading count would refuse this. That count is the wrong
    question.
    """
    f = _write(tmp_path, REPEATED_ACROSS_RELEASES)

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])
    assert rc == 0 and staged == [str(f)], out
    text = f.read_text(encoding="utf-8")
    assert _headings_under(text, "## [Unreleased]") == ["### Fixed", "### Changed"]
    assert _headings_under(text, "## [0.22.0] - 2026-08-01") == ["### Fixed"]


def test_a_hash_inside_a_fenced_block_is_not_a_heading(
        monkeypatch, capsys, tmp_path) -> None:
    """`# comment` in a shell fence is a comment. Refusing on it is a false alarm.

    This repo's changelog quotes commands constantly, so a scanner that reads
    fenced `#` lines as structure would refuse ordinary entries.
    """
    fenced = (
        "# Changelog\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "<<<<<<< HEAD\n"
        "### Changed\n"
        "\n"
        "- ours, with a snippet:\n"
        "\n"
        "```sh\n"
        "# run it\n"
        "supertool 'git-status'\n"
        "```\n"
        "=======\n"
        "### Added\n"
        "\n"
        "- theirs\n"
        ">>>>>>> branch\n"
        "\n"
        "```sh\n"
        "# run it\n"
        "supertool 'git-push'\n"
        "```\n"
    )
    f = _write(tmp_path, fenced)

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])
    assert rc == 0 and staged == [str(f)], out


def test_a_non_markdown_path_is_never_scanned(tmp_path) -> None:
    """The `#` heading grammar is Markdown's; a guard opines only where it reads."""
    f = tmp_path / "notes.txt"
    f.write_text(ASYMMETRIC_CONFLICT, encoding="utf-8")
    assert resolve._duplicated_headings(str(f)) == []

def test_a_fence_closed_inside_a_hunk_does_not_manufacture_a_refusal(tmp_path) -> None:
    """A second parse of the file would disagree with the first about the fence.

    The first shape of this fix rendered the surrounding context separately and
    compared heading counts. An odd number of ``` delimiters inside a hunk closes a
    fence in the union and leaves it open in the context render, so the two parses
    disagreed about which later lines were headings at all — and a document that
    already repeated `### Fixed`, before this conflict existed, was refused for it.
    """
    f = _write(tmp_path, (
        "# Changelog\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "```sh\n"
        "supertool 'git-status'\n"
        "<<<<<<< HEAD\n"
        "```\n"
        "=======\n"
        "- theirs\n"
        ">>>>>>> branch\n"
        "\n"
        "### Fixed\n"
        "\n"
        "- a\n"
        "\n"
        "### Fixed\n"
        "\n"
        "- b\n"
    ))
    assert resolve._duplicated_headings(str(f)) == []


def test_the_same_heading_duplicated_under_two_parents_is_two_findings(tmp_path) -> None:
    """One incident per section. Deduping on the heading text alone reported one.

    `### Fixed` doubled under `## [Unreleased]` and again under `## [0.22.0]` is two
    separate things gone wrong, and a refusal that says "1 heading" while naming a
    string that occurs in two places is a refusal you override having understood
    half of it.
    """
    f = _write(tmp_path, (
        "# Changelog\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "<<<<<<< HEAD\n"
        "### Fixed\n"
        "\n"
        "- ours\n"
        "=======\n"
        "### Changed\n"
        "\n"
        "- theirs\n"
        ">>>>>>> branch\n"
        "\n"
        "### Fixed\n"
        "\n"
        "- context\n"
        "\n"
        "## [0.22.0] - 2026-08-01\n"
        "\n"
        "<<<<<<< HEAD\n"
        "### Fixed\n"
        "\n"
        "- ours again\n"
        "=======\n"
        "### Added\n"
        "\n"
        "- theirs again\n"
        ">>>>>>> branch\n"
        "\n"
        "### Fixed\n"
        "\n"
        "- context again\n"
    ))
    dups = resolve._duplicated_headings(str(f))
    assert len(dups) == 2, dups
    assert any("## [Unreleased]" in d for d in dups), dups
    assert any("## [0.22.0] - 2026-08-01" in d for d in dups), dups


def test_a_crlf_document_is_read_the_same_way(tmp_path) -> None:
    """CI runs on Windows; a checkout there can carry CRLF. Same verdict, or the
    guard is only a guard on the platform it was written on."""
    f = _write(tmp_path, ASYMMETRIC_CONFLICT.replace(chr(10), chr(13) + chr(10)))
    assert resolve._duplicated_headings(str(f)) == [
        "### Fixed (under ## [Unreleased])"]
