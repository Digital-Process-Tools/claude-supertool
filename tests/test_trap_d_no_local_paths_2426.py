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
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAP_D = REPO_ROOT / "trap.d"

# `runneradmin` is the one Windows CI account name this repo's own fixtures
# already use as a legitimate non-maintainer example (trap.d/1165's Windows
# shlex note); everything else matching the shape below is a real local
# username and is in scope.
_ALLOWED_WINDOWS_USERS = {"runneradmin", "Public", "Default", "..."}

# `...` is an ellipsis placeholder trap.d/1165's own Windows-shlex note uses
# for "some intermediate path segment", not a username -- the same file's
# `C:\\Users\\...\\argv.py` is a generic template, not a real account.

_LOCAL_PATH = re.compile(
    r"/Users/(?P<posix_user>[A-Za-z0-9_.-]+)/"
    r"|/home/(?P<home_user>[A-Za-z0-9_.-]+)/"
    r"|C:\\Users\\(?P<win_user>[A-Za-z0-9_.-]+)\\"
)


def _local_path_hits(text: str) -> list[str]:
    hits = []
    for match in _LOCAL_PATH.finditer(text):
        user = (
            match.group("posix_user")
            or match.group("home_user")
            or match.group("win_user")
        )
        if user in _ALLOWED_WINDOWS_USERS:
            continue
        hits.append(match.group(0))
    return hits


def test_no_local_home_paths_in_trap_d():
    """Every trap.d/*.md fragment, scanned fresh (glob, not a hardcoded
    filename list) -- new fragments land here constantly and each one ships
    to every plugin install unredacted."""
    offenders = {}
    for path in sorted(TRAP_D.glob("*.md")):
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
