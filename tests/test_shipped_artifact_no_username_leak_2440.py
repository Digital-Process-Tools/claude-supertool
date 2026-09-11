"""#2440 -- the #2426 guard (`test_trap_d_no_local_paths_2426.py`) frames the
defect class as a property of the *shipped artifact* in its own docstring,
but its actual sweep is `TRAP_D.glob("*.md")` alone. A release audit found
two live leaks of the maintainer's real username sitting outside that glob,
already shipped:

- `presets/claude-log/_common.py:83` -- a docstring illustrating a resolver
  bug, carrying `-Users-floriandavid-Documents-st-wt-1024` verbatim.
- `CHANGELOG.md` (the #1317 entry) -- the identical string, copied from the
  same source.

`git log -S` attributes both to `ef0d4e3e` (the v0.34.0 release commit): old,
already-shipped content, not something introduced by the current cut. Still
worth a guard, because the property this file's docstring claims to hold
("no local path leaks into anything this repo ships") has never actually been
checked outside `trap.d/`.

**Why this does not just widen #2426's own glob to `**/*`.** This repo's own
docs and preset code deliberately use single-word, username-shaped example
paths throughout -- `/Users/x`, `/home/dev`, `/Users/f`, `/Users/me`,
`config=/Users/x/src/mine` -- to illustrate a resolver or a CLI without
picking a plausible-looking name. A generic sweep with #2426's allowlist
(three Windows account names and an ellipsis) would flag dozens of these as
"offenders", which is not the defect class this issue is about: those are
placeholders, not leaks, and drowning the two real leaks in that noise would
make the guard worse than the two-file glob it replaces.

So this guard inverts the allowlist instead of widening it: rather than
flagging every hit not in a small *known-benign* set (#2426's approach,
which only works because `trap.d/` fragments are prose that has no reason to
use a real-looking username as a placeholder), it flags only hits matching a
small *known-real* set -- usernames independently confirmed, by this
investigation, to be the maintainer's actual local account. Today that set
has one member. A future real leak under a *different* username would not be
caught by this guard alone; it would still be caught by #2426's own
trap.d-scoped, allowlist-of-benign guard for any new trap.d fragment, and by
a human reader for anything else, same as before #2440. This is a narrower
but honest widening: it closes the two known recurrences and any future
recurrence of the *same* leaked identity, anywhere in the tracked tree, and
says so rather than claiming a genericity it does not have.

`tests/` is excluded from the sweep: test fixtures deliberately plant
username-shaped strings (`exampleuser`, and this file's own docstring above)
as part of demonstrating the guard itself, and a test file is not part of
what an installed plugin runs -- unlike `trap.d/`, which #2426's own
docstring notes `.oss/statusline.py` reads from the installed tree at
runtime. `trap.d/*.md` is excluded too: it is #2426's own guard's territory,
covered there with the wider, benign-allowlist approach appropriate to prose
fragments.
"""
from __future__ import annotations

from pathlib import Path

import subprocess

from _local_path_scan import _local_path_hits
from _repo_walk import REPO_ROOT

# Independently confirmed real local usernames this repo's shipped tree has
# actually leaked, per #2440's own investigation (`git log -S` on the string
# below attributes both known occurrences to `ef0d4e3e`, the v0.34.0 release
# commit). Add to this set only when a new leak of a *different* real
# identity is confirmed -- it is a record of known incidents, not a general
# username blocklist.
KNOWN_REAL_USERNAMES = frozenset({"floriandavid"})

# Directories excluded for reasons stated in the module docstring above:
# `tests/` (fixtures deliberately plant the very shape this guard looks
# for), `trap.d/` (already covered, by #2426's own wider, prose-appropriate
# guard).
_EXCLUDED_TOP_DIRS = {"tests", "trap.d"}

# Tracked binary assets #2440's own self-review confirmed cannot decode as
# UTF-8 and therefore cannot carry a text leak -- named rather than silently
# swallowed by a bare except, so a third undecodable file shows up as a
# ceiling breach instead of vanishing the same way the first two did before
# this review caught it (auditor spawn, #2440 self-review).
_KNOWN_BINARY_ASSETS = frozenset({
    "notifiers/cursor-witness/extension/icon.png",
    "supertool-banner.webp",
})


def _candidate_files() -> list[Path]:
    """Every git-*tracked* file outside `tests/` and `trap.d/`.

    #2440's own self-review (Explore spawn) caught the first version of this
    walking `REPO_ROOT.rglob("*")` filtered only by `is_machine_state` --
    which answers "is this machine state", not "is this tracked" -- so an
    untracked scratch file sitting anywhere ungitignored (this review's own
    `tree_snapshot.py` output, reproduced live) was scanned and misreported
    as a "shipped file" leak. `git ls-files` is the tracked answer directly,
    the same property #2426's own regex allowlist assumes but never checked
    for its own glob (`trap.d/*.md` files are, in practice, always tracked).
    """
    proc = subprocess.run(
        ["git", "ls-files", "-z"], cwd=str(REPO_ROOT),
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"git ls-files failed (exit {proc.returncode}): "
        f"{proc.stderr.decode('utf-8', 'replace')}"
    )
    rels = [r for r in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if r]
    files = []
    for rel in sorted(rels):
        if rel.split("/")[0] in _EXCLUDED_TOP_DIRS:
            continue
        path = REPO_ROOT / rel
        if path.is_file():
            files.append(path)
    return files


def test_no_known_real_username_leak_outside_trap_d():
    """Sweeps every git-tracked file (minus `tests/` and `trap.d/`, see
    module docstring) for the specific real usernames #2440 confirmed this
    repo has actually leaked before -- not a generic username scan, which
    would flag this repo's own many legitimate example paths (see module
    docstring for why)."""
    candidates = _candidate_files()
    # Pin a floor the same way #2426 does: a directory rename or a typo'd
    # exclusion set silently walking zero files must not read as "clean".
    assert len(candidates) >= 100, (
        f"only {len(candidates)} candidate file(s) found -- expected at "
        "least 100. A near-zero count means the walk stopped finding "
        "files, not that they are clean."
    )
    offenders = {}
    undecodable = []
    for path in candidates:
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            undecodable.append(rel)
            continue
        hits = _local_path_hits(text, restrict_users=KNOWN_REAL_USERNAMES)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "shipped file(s) outside trap.d/ carry a known-real local username "
        f"leak: {offenders}. Redact to a generic placeholder path, per "
        "#2279's own precedent."
    )
    # A silent `except: continue` here would be exactly this repo's own
    # named defect class (CLAUDE.md's "an absence produced by the tool,
    # read as an absence in the world") one file deep: "clean" and "N files
    # never got a real read" must not render the same way. Every
    # undecodable file must be an already-named binary asset -- an
    # unnamed one is a third state (a new binary file, or a real text file
    # gone corrupt) and must surface rather than silently drop the file
    # from the sweep.
    unnamed = sorted(set(undecodable) - _KNOWN_BINARY_ASSETS)
    assert not unnamed, (
        f"{len(unnamed)} tracked file(s) could not be decoded as UTF-8 and "
        f"are not in `_KNOWN_BINARY_ASSETS`: {unnamed}. Confirm each is a "
        "genuine binary asset (not a text file this guard should be "
        "reading) and add it to that set, naming it rather than letting "
        "it vanish from the sweep silently."
    )


def test_scanner_still_ignores_this_repos_own_placeholder_examples():
    """Positive control: the many single-word example paths this repo's
    own docs and presets already carry (`/Users/x`, `/home/dev`, `/Users/f`,
    `config=/Users/x/src/mine`, ...) must not trip the known-username
    filter -- otherwise this guard could not distinguish a real leak from
    this repo's own documented style."""
    text = (
        "supertool 'read:/home/jo bob/file.py' and "
        "config=/Users/x/src/mine, root=/Users/x/src/evil, /Users/f, "
        "/Users/me/.claude/watch-b.sock"
    )
    assert _local_path_hits(text, restrict_users=KNOWN_REAL_USERNAMES) == []


def test_scanner_still_fires_on_the_known_real_username(tmp_path):
    """Negative-assertion control (CLAUDE.md's own rule): pair the 'must
    not fire' case above with a 'must fire' case in the same fixture."""
    text = "resolved from -Users-floriandavid-Documents-st-wt-1024 again\n"
    hits = _local_path_hits(text, restrict_users=KNOWN_REAL_USERNAMES)
    assert hits == ["-Users-floriandavid-"]
