"""#2426 -- `trap.d/` ships whole into every plugin install
(`.oss/statusline.py` reads it from the installed tree at runtime), so a
fragment carrying the maintainer's own absolute home-directory path leaks it
to every user. `#2279` fixed one instance
(`trap.d/2226.reviewer-deleted-untracked-file-in-main-clone.md`) by hand, with
no guard added, and the same defect recurred: `#2426` found three more files,
five more occurrences, one release later. This is that guard, so a third
recurrence fails CI instead of waiting for the next release audit.

Three shapes are in scope, the ones this repo's own cross-platform guidance
already enumerates: `/Users/<name>/...`, `/home/<name>/...`,
`C:\\Users\\<name>\\...`. A CI-runner path such as `C:\\Users\\runneradmin\\...`
is deliberately NOT the maintainer's own home directory and must not trip
this guard --
`trap.d/1165.windows-shlex-backslash-corrupts-unquoted-cmd-template-path.md`
carries exactly that shape as the subject of its own note, so the negative
fixture below pins it by name.

A fourth shape joined after a first self-review pass (#2426, Explore spawn):
the dash-flattened form this repo's own scratchpad directory naming produces
(`-Users-<name>-Documents-...`, seen verbatim, pre-redaction, in
`trap.d/2015.scratchpad-collision.md`), and a looser boundary after the
username on all three slash/backslash shapes -- the original regex required
a literal trailing separator, so `/Users/<name>` followed by punctuation,
whitespace or end-of-string (rather than another `/`) went unflagged. Both
gaps were real: the pre-fix `2015` fragment's dash-flattened line and a
"/Users/<name>." shape are exactly the kind of prose a trap.d/ fragment
writes.
"""
from __future__ import annotations

from pathlib import Path

from _local_path_scan import _local_path_hits

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAP_D = REPO_ROOT / "trap.d"

# The regex and the placeholder allowlist (`runneradmin`, the Windows-shlex
# `...` template segment, ...) now live in `_local_path_scan.py`, shared with
# #2440's whole-artifact sweep so the same pattern is not maintained twice
# (the drift `_repo_walk.py`'s own docstring warns about, for the identical
# reason). Behaviour here is unchanged: every hit not in that allowlist.


def _scan_fragments(trap_dir):
    """Return (readme_found, fragment_paths) for a trap.d-shaped directory.

    #2568: the old guard pinned a floor ("at least 5 files") to tell a
    working scan apart from a broken one -- but `curate_route_threshold`
    (#2561, #2565) is now allowed to drain trap.d/ to zero fragments on
    purpose, and a legitimately-empty directory renders identically to a
    directory a broken glob or a typo'd pattern silently stopped seeing.
    `README.md` is tracked, never a fragment, and never removed by a curate
    pass, so its presence in the scan is the discriminator: found ->
    the glob mechanism works, whatever it found beside it is the truth;
    missing -> the scan itself cannot be trusted, regardless of count."""
    scanned = sorted(trap_dir.glob("*.md"))
    readme = trap_dir / "README.md"
    readme_found = readme in scanned
    fragments = [path for path in scanned if path != readme]
    return readme_found, fragments


def test_no_local_home_paths_in_trap_d():
    """Every trap.d/*.md fragment, scanned fresh (glob, not a hardcoded
    filename list) -- new fragments land here constantly and each one ships
    to every plugin install unredacted."""
    readme_found, fragments = _scan_fragments(TRAP_D)
    # #2568: README.md missing from the scan means the glob mechanism itself
    # is broken (directory rename, typo'd pattern, ...) -- "verified clean"
    # and "verified nothing" must not render the same way. Zero fragments
    # beside a found README.md is a legitimately drained directory, not a
    # broken scan, so it is no longer asserted against.
    assert readme_found, (
        "trap.d/*.md did not find README.md -- the glob mechanism itself "
        "looks broken. README.md is tracked and never a fragment, so its "
        "absence from this scan means the scan cannot be trusted, whatever "
        "count it reports."
    )
    offenders = {}
    for path in fragments:
        text = path.read_text(encoding="utf-8")
        hits = _local_path_hits(text)
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "trap.d/ fragment(s) carry a literal local machine path that ships "
        f"to every plugin install: {offenders}. Redact to a descriptive "
        "placeholder (<worktree-NNNN>, <main-clone>, ...) per #2279's own "
        "precedent."
    )


def test_windows_runner_path_is_not_flagged():
    """Positive control for the guard itself: a CI-runner path is a
    different environment, not the maintainer's machine, and must not trip
    the scanner -- otherwise the guard would be unfalsifiable noise the
    first time it saw a legitimate example."""
    text = (
        "reproduced on a Windows runner where `tmp_path` renders as "
        "`C:\\Users\\runneradmin\\AppData\\Local\\Temp\\argv.py`"
    )
    assert _local_path_hits(text) == []


def test_scanner_still_fires_on_a_planted_home_path(tmp_path):
    """Negative-assertion control (CLAUDE.md's own rule): pair the 'must not
    fire' case above with a 'must fire' case in the same fixture, so a
    scanner that matches nothing at all would not pass both silently."""
    planted = tmp_path / "planted.md"
    planted.write_text(
        "worktree /Users/exampleuser/Documents/st-wt/9999\n",
        encoding="utf-8",
    )
    hits = _local_path_hits(planted.read_text(encoding="utf-8"))
    assert hits == ["/Users/exampleuser/"]


def test_scanner_fires_on_home_style_variant(tmp_path):
    planted = tmp_path / "planted.md"
    planted.write_text(
        "config at /home/exampleuser/.config/thing\n", encoding="utf-8"
    )
    hits = _local_path_hits(planted.read_text(encoding="utf-8"))
    assert hits == ["/home/exampleuser/"]


def test_scanner_fires_on_windows_non_runner_user(tmp_path):
    planted = tmp_path / "planted.md"
    planted.write_text(
        "path C:\\Users\\exampleuser\\Documents\\thing\n",
        encoding="utf-8",
    )
    hits = _local_path_hits(planted.read_text(encoding="utf-8"))
    assert hits == ["C:\\Users\\exampleuser\\"]


def test_scanner_fires_without_a_trailing_path_segment(tmp_path):
    """#2426 self-review (Explore + oss:auditor spawns, converged
    independently): the original regex required a literal trailing
    separator, so a bare home-directory mention -- end of sentence,
    parenthetical, shell operator -- went unflagged. Three shapes in one
    fixture, none of which have a path segment after the username."""
    planted = tmp_path / "planted.md"
    planted.write_text(
        "reported from /Users/exampleuser.\n"
        "(cd /Users/exampleuser && ls)\n"
        "see /Users/exampleuser here\n",
        encoding="utf-8",
    )
    hits = _local_path_hits(planted.read_text(encoding="utf-8"))
    assert len(hits) == 3
    assert all(hit.startswith("/Users/exampleuser") for hit in hits)


def test_scanner_fires_on_dash_flattened_scratchpad_shape(tmp_path):
    """#2426 self-review (Explore + oss:auditor spawns, converged
    independently): this repo's own scratchpad directory naming flattens
    path separators to dashes -- exactly the shape
    trap.d/2015.scratchpad-collision.md:22 carried, verbatim, before this
    same commit hand-redacted it."""
    planted = tmp_path / "planted.md"
    planted.write_text(
        "(`/private/tmp/claude-501/-Users-exampleuser-Documents-"
        "claude-supertool/<session-uuid>/scratchpad`)\n",
        encoding="utf-8",
    )
    hits = _local_path_hits(planted.read_text(encoding="utf-8"))
    assert hits == ["-Users-exampleuser-"]


def test_scan_fragments_detects_broken_glob(tmp_path):
    """Positive control for #2568's discriminator: a directory missing
    README.md must report readme_found=False -- a broken scan must not
    render identically to a directory legitimately drained to zero."""
    readme_found, fragments = _scan_fragments(tmp_path)
    assert readme_found is False
    assert fragments == []


def test_scan_fragments_passes_on_legitimately_drained_directory(tmp_path):
    """The exact case #2568 fixes: trap.d/ drained to zero fragments by a
    curate pass (PR #2567) must not be mistaken for a broken scan."""
    (tmp_path / "README.md").write_text("# trap.d\n", encoding="utf-8")
    readme_found, fragments = _scan_fragments(tmp_path)
    assert readme_found is True
    assert fragments == []


def test_scan_fragments_still_finds_real_fragments_beside_readme(tmp_path):
    """README.md is excluded from the fragment list it also proves the
    glob works for -- a real fragment beside it is still scanned."""
    (tmp_path / "README.md").write_text("# trap.d\n", encoding="utf-8")
    (tmp_path / "9999.example.md").write_text("note\n", encoding="utf-8")
    readme_found, fragments = _scan_fragments(tmp_path)
    assert readme_found is True
    assert [path.name for path in fragments] == ["9999.example.md"]
