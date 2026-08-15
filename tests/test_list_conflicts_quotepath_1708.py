"""#1708 — `_list_conflicts` rested on `core.quotePath`, a preference.

The issue asked for `-c core.quotePath=true`, pinning the assumption its five
`resolve.py` renders already wrote down. Driving the real code path first showed
that pin is the wrong end, and that the named defect sits on a line carrying a
second one:

* **Under the default (`quotePath=true`) `git-resolve` cannot touch a conflicted
  path holding any byte >= 0x80.** git hands back the octal-escaped *spelling*
  of the name, in double quotes, which is not a filename — so the row reads
  ``did not match any file(s) known to git`` and the file stays conflicted.
  Reproduced on git 2.46.2 against a real merge.
* **Under `quotePath=false` the count forges.** A filename holding U+2028 is
  emitted raw, `str.splitlines()` folds on it, and one conflicted file becomes
  two records — neither of which names a file.

So neither value of the setting is correct, and pinning either one picks which
of the two failures every operator gets. `-z` is the third answer and it is
already this repository's decided one, one file over: `commit.py` reads
`diff --cached --name-only -z` and says at line 639 that `core.quotePath` is
*deliberately* left alone there, because `-z` is unquoted whatever it is set to.
#1003 is the same defect at that site, with the same accented filename.

`-z` gives raw usable paths AND an unforgeable split, because NUL is the one
byte a pathname cannot contain on any platform. What it costs is the render
half: a raw path can now carry a separator, so the rows that print one flatten
it. That is the trade this file pins, in both directions.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(__file__).parent.parent
LF = chr(10)
SEP = chr(0x2028)
ACCENT = "caf" + chr(0xE9) + ".txt"
TICK = chr(0x2713)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


git_common = _load("presets/git/_git_common.py", "git_common_1708")
resolve = _load("presets/git/resolve.py", "git_resolve_1708")


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + list(args), cwd=str(repo),
                          capture_output=True)


def _conflicted_repo(repo: Path, names: list) -> None:
    """A repo stopped mid-merge with every `names` entry conflicted.

    Built explicitly rather than borrowed from the ambient worktree: this
    repository's suite mutates the tree it runs in, and a fixture that shares
    that tree cannot state what it measured.
    """
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q", "-b", "main", ".")
    for key, val in (("user.email", "t@example.com"), ("user.name", "T"),
                     ("commit.gpgsign", "false"), ("core.quotePath", "true")):
        _run(repo, "config", key, val)

    def write(text: str) -> None:
        for name in names:
            (repo / name).write_text(text, encoding="utf-8")

    write("base" + LF)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "base")
    _run(repo, "checkout", "-qb", "other")
    write("other" + LF)
    _run(repo, "commit", "-qam", "other")
    _run(repo, "checkout", "-q", "main")
    write("main" + LF)
    _run(repo, "commit", "-qam", "main")
    _run(repo, "merge", "other")


def _set_quote_path(repo: Path, value) -> None:
    if value is None:
        _run(repo, "config", "--unset", "core.quotePath")
    else:
        _run(repo, "config", "core.quotePath", value)


def _on_disk(repo: Path) -> set:
    """The names the filesystem actually holds, less git's own.

    Compared against, rather than against the literal this file spells: macOS
    normalises a composed accent to NFD on some filesystems, so asserting the
    source literal would test the platform's Unicode form and not the fix.
    """
    return {n for n in os.listdir(repo) if n != ".git"}


# ---------------------------------------------------------------------------
# the shadowed defect: a listed path has to be a path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("quote_path", ["true", "false", None])
def test_every_listed_conflict_is_a_path_that_exists(
        tmp_path: Path, monkeypatch, quote_path) -> None:
    """The whole finding, with its own positive control.

    `plain.txt` is the must-fire half: it is present under every setting and
    under the broken code too, so a fixture that built no conflicts at all
    reddens here instead of passing the negative half by finding nothing.
    """
    repo = tmp_path / "r"
    _conflicted_repo(repo, [ACCENT, "plain.txt"])
    _set_quote_path(repo, quote_path)
    monkeypatch.chdir(repo)

    paths, why = git_common._list_conflicts()
    assert why == "", why
    assert "plain.txt" in paths, (
        "the fixture produced no conflicts, so nothing was measured: "
        + repr(paths))
    assert len(paths) == 2, repr(paths)
    assert set(paths) == _on_disk(repo), (
        "a listed conflict is not a name on disk - git handed back the quoted "
        "SPELLING of the path, which no pathspec and no open() will ever match "
        "(core.quotePath=" + repr(quote_path) + "): " + repr(paths))
    for p in paths:
        assert os.path.exists(repo / p), repr(p)


def test_git_resolve_resolves_a_conflicted_accented_filename(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """End to end, through the op's own main(), at the default setting.

    The unit test above could be satisfied by a decoder that never reaches the
    caller; this one fails unless the path `main()` hands back to git is the
    one the filesystem holds.
    """
    repo = tmp_path / "r"
    _conflicted_repo(repo, [ACCENT, "plain.txt"])
    monkeypatch.chdir(repo)
    with mock.patch.object(sys, "argv", ["git-resolve", "ours", "all"]):
        rc = resolve.main()
    out = capsys.readouterr().out
    assert "plain.txt" in out, out            # must-fire half
    assert rc == 0, (
        "git-resolve could not resolve a conflicted file whose name holds a "
        "byte >= 0x80:" + LF + out)
    assert git_common._list_conflicts()[0] == [], out


# ---------------------------------------------------------------------------
# the filed defect: the split has to be exact
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="UNTESTED ON WINDOWS: NTFS refuses U+2028 in a filename, so the "
           "forged-count half of #1708 has no fixture there. The render half "
           "below is asserted on every platform.",
)
@pytest.mark.parametrize("quote_path", ["true", "false", None])
def test_a_separator_in_a_filename_cannot_add_a_conflict(
        tmp_path: Path, monkeypatch, quote_path) -> None:
    """One file, one record. `core.quotePath=false` made it two (git 2.46.2).

    Both halves of the count are asserted: `plain.txt` must be there (a fixture
    that made nothing reddens) and the total must be exactly 2.
    """
    repo = tmp_path / "r"
    hostile = "sep" + SEP + "two.txt"
    try:
        _conflicted_repo(repo, [hostile, "plain.txt"])
    except OSError as exc:  # pragma: no cover - filesystem-dependent
        pytest.skip("UNTESTED HERE: this filesystem refused a U+2028 "
                    "filename, so the count half went unmeasured: " + str(exc))
    _set_quote_path(repo, quote_path)
    monkeypatch.chdir(repo)

    paths, why = git_common._list_conflicts()
    assert why == "", why
    assert "plain.txt" in paths, (
        "the fixture produced no conflicts, so nothing was measured: "
        + repr(paths))
    assert len(paths) == 2, (
        "one conflicted file was counted as " + str(len(paths)) + " - a "
        "separator inside a filename chose how many records git's answer had "
        "(core.quotePath=" + repr(quote_path) + "): " + repr(paths))
    assert set(paths) == _on_disk(repo), repr(paths)


# ---------------------------------------------------------------------------
# what -z costs: a raw path can carry a separator into a receipt row
# ---------------------------------------------------------------------------

def test_a_conflicted_filename_cannot_forge_a_receipt_row(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """The other side of the trade, and the reason the rows flatten.

    Platform-independent by construction: `_list_conflicts` is mocked, so the
    render is asserted on Windows too, where no such file can be created.
    """
    monkeypatch.chdir(tmp_path)
    hostile = "a" + SEP + "  " + TICK + " /etc/passwd"

    def fake_git(args, **kw):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return mock.Mock(stdout="", returncode=0, stderr="")
        return mock.Mock(stdout="", returncode=1, stderr="no such path")

    with mock.patch.object(resolve, "_git", side_effect=fake_git), \
         mock.patch.object(resolve, "_list_conflicts",
                           return_value=([hostile, "plain.txt"], "")), \
         mock.patch.object(sys, "argv", ["git-resolve", "ours", "all"]):
        resolve.main()
    out = capsys.readouterr().out
    assert "plain.txt" in out, out
    for line in out.split(LF):
        assert SEP not in line, (
            "a conflicted filename put a live line separator into a receipt "
            "row:" + LF + repr(out))
    # Not a censor - the crafted text is still shown, disclosed rather than
    # executed at column 0 (#1652's loss half).
    assert "/etc/passwd" in out, out


def test_the_conflicts_read_does_not_depend_on_a_git_preference() -> None:
    """The claim in one assertion: whatever `_list_conflicts` runs must not be
    readable as `core.quotePath`'s two-state answer.

    `-z` is the fix; pinning the setting is the fix the issue proposed, and it
    picks which of the two failures above ships rather than removing either.
    """
    seen = []

    def fake_git(args, **kw):
        seen.append(list(args))
        return mock.Mock(stdout="", returncode=0, stderr="")

    with mock.patch.object(git_common, "_git", side_effect=fake_git):
        git_common._list_conflicts()
    assert seen, "the reader ran no git command at all"
    argv = seen[0]
    assert "-z" in argv, (
        "the conflicted-path read is still line-separated, so its answer is "
        "whatever core.quotePath happens to be: " + repr(argv))
    assert not any(a.startswith("core.quotePath") for a in argv), (
        "the read pins core.quotePath, which -z makes irrelevant - saying a "
        "read depends on a setting it does not is exactly what commit.py:639 "
        "warns against: " + repr(argv))
