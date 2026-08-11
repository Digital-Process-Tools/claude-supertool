"""#1300 — a `~/` path was refused as not-found, and the receipt named a path
that was never stat-ed.

Two halves, and the second is the one that cost time:

* **`~` was never expanded on the way to the op.** `_safe_path` — the
  containment gate — expands `~` before it checks, and has since #146. The
  *value* it approved was then thrown away and the op stat-ed the caller's
  literal `~/x`, which resolves to `./~/x` under the cwd and never exists.
  So `~/` failed where the same file named absolutely read fine, and
  `cwd:~/dir` (which does expand, in the CLI pre-pass) worked all along.
* **The not-found receipt printed the expanded path.** `_path_not_found` ran
  its own `os.path.expanduser` purely for display, so `tried:` named a path
  the tool had never touched — one that existed, and that `ls` confirmed a
  second later. A reader who trusts it concludes the file is missing.

The containment story, which is the whole judgment in the issue: the
expansion added here is the **same** expansion the gate already applies, and
it is written back only *after* the gate has approved the argument. Nothing
reaches an op that `_safe_path` did not clear, so this widens nothing —
`test_a_tilde_path_outside_the_boundary_is_still_refused` is the pin.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import supertool

MARK = "TILDE-1300-OK"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `~` at tmp_path on every platform `expanduser` supports.

    POSIX reads HOME; Windows prefers USERPROFILE and falls back to
    HOMEDRIVE+HOMEPATH, so the last two are removed rather than left to
    point at the real profile.
    """
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    assert Path(os.path.expanduser("~")) == h
    return h


@pytest.fixture()
def sample(home: Path) -> Path:
    f = home / "sample.txt"
    f.write_text(MARK + chr(10) + "second line" + chr(10), encoding="utf-8")
    return f


_FILE_CALLS = [
    "read:~/sample.txt",
    "read:~/sample.txt:0:1",
    "head:~/sample.txt:1",
    "tail:~/sample.txt:1",
    "wc:~/sample.txt",
    "stat:~/sample.txt",
    "grep:" + MARK + ":~/sample.txt",
    "grep_around:" + MARK + ":~/sample.txt:1",
    "around:" + MARK + ":~/sample.txt:1",
    "around_line:~/sample.txt:1:1",
    "between:re:" + MARK + ":second:~/sample.txt",
]


class TestEveryPathOpExpandsTilde:
    """The issue expected this to be every path-taking op. It was."""

    @pytest.mark.parametrize("call", _FILE_CALLS)
    def test_a_tilde_path_reaches_the_file(
            self, call: str, sample: Path) -> None:
        out = supertool.dispatch(call)
        assert "not found" not in out, out
        assert "ERROR" not in out, out

    @pytest.mark.parametrize("call", ["ls:~/dir", "tree:~/dir"])
    def test_a_tilde_directory_reaches_the_directory(
            self, call: str, home: Path) -> None:
        d = home / "dir"
        d.mkdir()
        (d / "inside.txt").write_text(MARK, encoding="utf-8")
        out = supertool.dispatch(call)
        assert "not found" not in out, out
        assert "inside.txt" in out, out

    def test_map_reads_a_tilde_path(self, home: Path) -> None:
        (home / "mod.py").write_text(
            "def " + "marked_fn" + "():" + chr(10) + "    pass" + chr(10),
            encoding="utf-8")
        out = supertool.dispatch("map:~/mod.py")
        assert "not found" not in out, out
        assert "marked_fn" in out, out

    def test_the_payload_route_expands_it_too(self, sample: Path) -> None:
        """`read:@-` builds `parts` and re-enters the same gate (#1300)."""
        out = supertool.dispatch("read:@-", pre_parsed=(
            ["read", "~/sample.txt"], False))
        assert "not found" not in out, out
        assert MARK in out, out


class TestGlobRefusesRatherThanInventingAZero:
    """`glob` is the deliberate exception, and it has to say so.

    It is not in `_PATH_ARG_POSITIONS` and resolves its own pattern, so there
    is no containment gate for an expansion to sit behind. Expanding `~` here
    would widen what the op can reach; answering `(0 files)` was worse than
    either — a zero the tool made up, reading exactly like an empty directory.
    """

    def test_a_tilde_pattern_is_refused_not_answered_with_zero(
            self, home: Path) -> None:
        (home / "a.txt").write_text(MARK, encoding="utf-8")
        out = supertool.dispatch("glob:~/*.txt")
        assert "(0 files)" not in out, out
        assert "unsupported path form" in out, out
        assert "absolute path" in out, out

    def test_an_absolute_pattern_still_answers(self, home: Path) -> None:
        (home / "a.txt").write_text(MARK, encoding="utf-8")
        out = supertool.dispatch("glob:" + (home / "*.txt").as_posix())
        assert "a.txt" in out, out
        assert "unsupported" not in out, out


def _tried(out: str) -> Path:
    lines = [ln for ln in out.splitlines() if ln.strip().startswith("tried:")]
    assert len(lines) == 1, out
    return Path(lines[0].split("tried:", 1)[1].split(" (cwd:")[0].strip())


_UNKNOWN_USER = "~nosuchuser1300/x.txt"


class TestTheReceiptNamesWhatWasTried:
    """The issue's non-negotiable: `tried:` is the string that was stat-ed.

    Asserted as a round-trip rather than against a computed literal. Creating
    a file at the path the receipt named must make the identical call
    succeed — if the two ever diverge again, no spelling of an expected
    string has to be maintained for the test to notice.
    """

    def test_creating_the_tried_path_satisfies_the_same_call(
            self, home: Path) -> None:
        out = supertool.dispatch("read:~/absent.txt")
        assert "not found" in out, out
        target = _tried(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(MARK, encoding="utf-8")
        assert MARK in supertool.dispatch("read:~/absent.txt"), out

    def test_it_holds_for_a_tilde_user_too(
            self, home: Path, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """Whatever this platform makes of `~user`, the receipt must match it.

        POSIX leaves an unknown user alone; `ntpath.expanduser` resolves it
        against the parent of USERPROFILE without checking it exists. Both
        are fine — the claim under test is that the op and the receipt agree,
        not which of the two answers the platform gives.

        `chdir` first, because on POSIX the unexpanded form resolves relative
        to the cwd and the round-trip below then creates it there — this test
        left a `~nosuchuser1300/` directory in the repo worktree once.
        """
        work = tmp_path / "cwd"
        work.mkdir()
        monkeypatch.chdir(work)
        out = supertool.dispatch("read:" + _UNKNOWN_USER)
        assert "not found" in out, out
        target = _tried(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(MARK, encoding="utf-8")
        assert MARK in supertool.dispatch("read:" + _UNKNOWN_USER), out

    def test_an_unexpandable_tilde_is_not_blamed_on_the_cwd(
            self, home: Path) -> None:
        """`cwd:` provably cannot fix a `~` that did not expand (#921, #734).

        Skipped where the platform has no unexpandable `~user` at all rather
        than branched into a vacuous pass: on Windows `expanduser` invents a
        home for any name, so the hint has no case to serve and asserting it
        fires would be asserting a bug.
        """
        if os.path.expanduser(_UNKNOWN_USER) != _UNKNOWN_USER:
            pytest.skip("expanduser resolves ~user here without checking it "
                        "exists, so there is no unexpandable form to hint at")
        out = supertool.dispatch("read:" + _UNKNOWN_USER)
        assert "wrong CWD?" not in out, out
        assert "not expanded" in out, out


class TestContainmentIsNotWidened:

    def test_a_tilde_path_outside_the_boundary_is_still_refused(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The expansion is written back only behind the gate.

        conftest sets the env opt-out globally; a containment assertion must
        not inherit it.
        """
        monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
        h = tmp_path / "home"
        h.mkdir()
        (h / "secret.txt").write_text("secret", encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.setenv("HOME", str(h))
        monkeypatch.setenv("USERPROFILE", str(h))
        monkeypatch.delenv("HOMEDRIVE", raising=False)
        monkeypatch.delenv("HOMEPATH", raising=False)
        monkeypatch.setattr(supertool, "_CONFIG", {})
        monkeypatch.chdir(work)
        out = supertool.dispatch("read:~/secret.txt")
        assert "ERROR: path escapes cwd" in out, out
        assert "(resolved to " in out, out

    def test_the_gate_returns_the_value_it_checked(self) -> None:
        """`_gate_paths` cannot hand back a string it did not clear."""
        err, resolved = supertool._gate_paths(["~/a.txt", "b.txt", ""])
        assert err is None, err
        assert resolved == [os.path.expanduser("~/a.txt"), "b.txt", ""]
