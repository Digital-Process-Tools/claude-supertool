"""A preset whose exit code is an ANSWER must not be counted as a refusal (#1672).

`git-worktrees` deliberately compresses its occupancy verdict into the process
exit status - `0 = idle, 1 = occupied, 2 = cannot tell` - and says so in the line
immediately above the batch footer. The dispatcher reads any non-zero child exit
as a refusal, so three correct answers rendered `FAIL` each and the footer said
`all 3 refused`, one line under the op's own `the op itself did not fail`.

This is not #1291: #1297's structural dispatch is right that the exit was
non-zero. What it could not know is that for this op non-zero is not failure -
and nothing in the process carried that fact, because it is a property of the op,
so it belongs in the registry beside `paths` and `safety`.

**What the declaration costs the op.** Giving up the integer as a verdict means
the failure channel has to be somewhere else, and there are two, both already
conventions here: a line at column 0 beginning `ERROR:` (the repo-wide refusal
form `_op_body_failed` reads), and anything on stderr. An exit code outside the
declared set stays a refusal, so a crash exiting 127 is still a crash.

**Measured before choosing, because the issue's third direction is priced on it**
(#1672 direction 3, "it breaks whatever is branching on the integer"): nothing can
be branching on it through supertool. `main` collapses every failing op to exit 1,
so `supertool 'git-worktrees:PATH'` on a `cannot tell` worktree returned 1 to the
shell and never 2. The value never crossed the boundary; only the false `refused`
did.
"""

import json
import shlex
from pathlib import Path

import pytest

import supertool

from _changelog_findable import assert_change_is_findable

REPO_ROOT = Path(__file__).resolve().parent.parent


def _script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(body)
    return shlex.quote(script.as_posix())


def _op(tmp_path: Path, body: str, declared: object = None) -> dict:
    entry = {"cmd": "{python} " + _script(tmp_path, "probe.py", body)}
    if declared is not None:
        entry["exitStatus"] = declared
    return {"ops": {"probe": entry}}


VALUES = {"values": [0, 1, 2]}


def test_a_declared_value_exit_is_not_a_refusal(tmp_path: Path) -> None:
    supertool._CONFIG = _op(
        tmp_path,
        "import sys" + chr(10) + "print('occupied')" + chr(10) + "sys.exit(1)",
        VALUES)

    out, failed = supertool.dispatch_verdict("probe")

    assert not failed, out
    assert "FAIL" not in out, out
    assert "occupied" in out, out


def test_the_pass_line_discloses_that_the_integer_was_read_as_a_value(tmp_path: Path) -> None:
    """A reader seeing PASS beside a non-zero exit must be told why (#1496's rule)."""
    supertool._CONFIG = _op(
        tmp_path, "import sys" + chr(10) + "sys.exit(2)", VALUES)

    out, _failed = supertool.dispatch_verdict("probe")

    assert "exit 2" in out, out


def test_an_undeclared_op_exiting_non_zero_is_still_a_refusal(tmp_path: Path) -> None:
    supertool._CONFIG = _op(
        tmp_path, "import sys" + chr(10) + "sys.exit(1)")

    out, failed = supertool.dispatch_verdict("probe")

    assert failed, out
    assert "FAIL" in out, out


def test_an_exit_code_outside_the_declared_set_is_still_a_refusal(tmp_path: Path) -> None:
    supertool._CONFIG = _op(
        tmp_path, "import sys" + chr(10) + "sys.exit(127)", VALUES)

    out, failed = supertool.dispatch_verdict("probe")

    assert failed, out


def test_an_error_line_in_the_body_is_still_a_refusal(tmp_path: Path) -> None:
    """`git-worktrees` returns 2 for `cannot tell` AND for `git did not answer`."""
    supertool._CONFIG = _op(
        tmp_path,
        "import sys" + chr(10) + "print('ERROR: git worktree list failed')"
        + chr(10) + "sys.exit(2)",
        VALUES)

    out, failed = supertool.dispatch_verdict("probe")

    assert failed, out


def test_anything_on_stderr_is_still_a_refusal(tmp_path: Path) -> None:
    """A traceback exits 1, which is a declared value - stderr is what tells."""
    supertool._CONFIG = _op(
        tmp_path,
        "import sys" + chr(10) + "raise SystemExit(_undefined_name)",
        VALUES)

    out, failed = supertool.dispatch_verdict("probe")

    assert failed, out


def test_a_declared_zero_exit_is_unchanged(tmp_path: Path) -> None:
    supertool._CONFIG = _op(
        tmp_path, "print('idle')", VALUES)

    out, failed = supertool.dispatch_verdict("probe")

    assert not failed, out
    assert "idle" in out, out


def test_the_batch_footer_does_not_call_value_answers_refusals(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    supertool._CONFIG = _op(
        tmp_path,
        "import sys" + chr(10) + "print('occupied')" + chr(10) + "sys.exit(1)",
        VALUES)

    code = supertool.main(["probe", "probe", "probe"])
    printed = capsys.readouterr().out

    assert "refused" not in printed, printed
    # The footer is what this test is about, and it still does not say
    # `refused`. The process exit is a separate channel and #1705 took it back:
    # `1` is not a value `probe` declares clean, and `0` has to keep meaning
    # nothing-to-worry-about. See tests/test_value_exit_clean_1705.py.
    assert code == 1, printed


def test_git_worktrees_declares_the_integers_its_own_note_documents() -> None:
    """The op this was filed against, and the only shipped one that overloads."""
    registry = json.loads((REPO_ROOT / "presets" / "git.json").read_text(encoding="utf-8"))
    entry = registry["ops"]["git-worktrees"]

    # `clean` is #1705's half of the same declaration, pinned in its own file.
    assert entry.get("exitStatus", {}).get("values") == [0, 1, 2], entry.get("exitStatus")


def test_change_is_documented() -> None:
    assert_change_is_findable("1672", REPO_ROOT)
