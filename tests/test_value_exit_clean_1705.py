"""A declared value exit that is not clean must not exit 0 (#1705).

#1672 was right and this does not touch it: `exit 1` from `git-worktrees` is the
op's ANSWER about occupancy, not a failure, and rendering it as a refusal was the
bug. The receipt still says `PASS`, the batch footer still does not say `refused`.

What #1672 changed as a side effect, and never argued for, is supertool's own
process exit. Before it, any non-zero child collapsed to a non-zero supertool
exit, so

    supertool 'git-worktrees:PATH' && rm -rf PATH

held as a fail-safe. After it, `occupied` and `cannot tell` both exited 0 and the
guard passed. #1282 records a consumer of exactly that shape.

So `0` is reserved: it means "nothing to worry about", never "here is a non-clean
answer". A declared value the op does not declare clear-to-proceed reaches the
process exit through its own per-call channel, beside the skipped write and the
rolled-back edit that already get there without being refusals.
"""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import supertool

from _changelog_findable import assert_change_is_findable

REPO_ROOT = Path(__file__).resolve().parent.parent

VALUES = {"values": [0, 1, 2]}


def _op(tmp_path: Path, body: str, declared: object = None) -> dict:
    script = tmp_path / "probe.py"
    script.write_text(body)
    entry = {"cmd": "{python} " + shlex.quote(script.as_posix())}
    if declared is not None:
        entry["exitStatus"] = declared
    return {"ops": {"probe": entry}}


def _occupied(tmp_path: Path, declared: object = VALUES) -> dict:
    return _op(
        tmp_path,
        "import sys" + chr(10) + "print('occupied')" + chr(10) + "sys.exit(1)",
        declared)


def test_a_non_clean_value_answer_does_not_exit_zero(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The consumer shape: `supertool 'op' && rm -rf PATH` must not proceed."""
    supertool._CONFIG = _occupied(tmp_path)

    code = supertool.main(["probe"])
    printed = capsys.readouterr().out

    assert code != 0, printed


def test_it_is_still_not_a_refusal(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """#1672's property, unchanged: the answer renders as an answer."""
    supertool._CONFIG = _occupied(tmp_path)

    code = supertool.main(["probe", "probe", "probe"])
    printed = capsys.readouterr().out

    assert "FAIL" not in printed, printed
    assert "refused" not in printed, printed
    assert "occupied" in printed, printed
    assert code != 0, printed


def test_the_verdict_line_says_why_supertool_exits_non_zero(
        tmp_path: Path) -> None:
    """A single op prints no batch tally, so the disclosure has to be here."""
    supertool._CONFIG = _occupied(tmp_path)

    out, _failed = supertool.dispatch_verdict("probe")

    assert "clear to proceed" in out, out


def test_the_batch_tally_does_not_blame_a_skipped_write(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Exit 1 with 0 refused already had a reason line; it must not be false."""
    supertool._CONFIG = _occupied(tmp_path)

    supertool.main(["probe", "probe"])
    printed = capsys.readouterr().out

    assert "[batch]" in printed, printed
    assert "a skipped write" not in printed, printed
    assert "clear to proceed" in printed, printed


def test_an_op_may_declare_a_non_zero_value_clean(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The meaning of each value is the op's to state, not this layer's to guess."""
    supertool._CONFIG = _occupied(tmp_path, {"values": [0, 1], "clean": [1]})

    code = supertool.main(["probe"])
    printed = capsys.readouterr().out

    assert code == 0, printed


def test_zero_is_clean_without_being_declared(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    supertool._CONFIG = _op(tmp_path, "print('idle')", VALUES)

    code = supertool.main(["probe"])
    printed = capsys.readouterr().out

    assert code == 0, printed


def test_a_malformed_clean_declaration_leaves_only_zero_clean(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Wrong in the loud direction, like every other tolerant read of the entry."""
    supertool._CONFIG = _occupied(tmp_path, {"values": [0, 1, 2], "clean": "1"})

    code = supertool.main(["probe"])
    printed = capsys.readouterr().out

    assert code != 0, printed


def test_the_shipped_op_does_not_exit_zero_for_cannot_tell(
        tmp_path: Path) -> None:
    """End to end, through the real `git-worktrees` - the instance #1705 names.

    A path that is not a worktree of this repository is `cannot tell` (exit 2)
    on every platform, and `cannot tell` is explicitly NOT `idle`.
    """
    proc = subprocess.run(
        [sys.executable, "supertool.py",
         "git-worktrees:" + tmp_path.as_posix() + ":nopr"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace")

    assert "cannot tell" in proc.stdout, proc.stdout + proc.stderr
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_git_worktrees_declares_which_of_its_values_is_clean() -> None:
    """The only shipped op that overloads its exit code says so itself."""
    registry = json.loads(
        (REPO_ROOT / "presets" / "git.json").read_text(encoding="utf-8"))
    entry = registry["ops"]["git-worktrees"]
    expected = {"values": [0, 1, 2], "clean": [0]}

    assert entry.get("exitStatus") == expected, entry.get("exitStatus")


def test_no_shipped_op_overloads_its_exit_code_unaccounted() -> None:
    """The sweep #1705 asked for, kept honest as the registry grows.

    Every op declaring `exitStatus` must also declare which of its values is
    clean, so a future one cannot inherit the implicit `0` this issue is about.
    """
    undeclared = []
    for registry_path in sorted((REPO_ROOT / "presets").glob("*.json")):
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        for name, entry in (registry.get("ops") or {}).items():
            if not isinstance(entry, dict):
                continue
            decl = entry.get("exitStatus")
            if isinstance(decl, dict) and not isinstance(decl.get("clean"), list):
                undeclared.append(registry_path.name + ":" + name)

    assert undeclared == [], undeclared


def test_change_is_documented() -> None:
    assert_change_is_findable("1705", REPO_ROOT)
