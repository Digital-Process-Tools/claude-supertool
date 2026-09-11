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


def test_no_local_home_paths_in_trap_d():
    """Every trap.d/*.md fragment, scanned fresh (glob, not a hardcoded
    filename list) -- new fragments land here constantly and each one ships
    to every plugin install unredacted."""
    scanned = sorted(TRAP_D.glob("*.md"))
    # #2426 self-review (oss:auditor spawn): a directory rename or a typo'd
    # glob pattern would make this loop run zero times and the assertion
    # below pass vacuously -- "verified clean" and "verified nothing" must
    # not render the same way. Pin a floor rather than an exact count, since
    # new fragments land here constantly.
    assert len(scanned) >= 5, (
        f"trap.d/*.md scanned {len(scanned)} file(s) -- expected at least "
        "5 (the count present when this guard was written). A near-zero "
        "count means the glob stopped finding fragments, not that they "
        "are clean."
    )
    offenders = {}
    for path in scanned:
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
