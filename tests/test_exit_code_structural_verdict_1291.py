"""The exit code is a fact the dispatcher holds, not a shape in the receipt.

`_body_indicates_failure` re-read the rendered body — header included — to
decide the process exit code, and the header carries the caller's argument
verbatim. That made the verdict a function of what the caller typed:

  * an argument spanning lines, followed by an error-shaped line, fired the
    failure arm on an op that SUCCEEDED (#1291a) — and #1284's batch tally
    then printed `1 refused` in words, an explicit false sentence;
  * an argument containing a line that ends in ` ---` moved the header close
    earlier, so a REFUSAL was read off the wrong line and exited 0 (#1291b).
    Reachable from this repo's own commit convention: a message quoting an
    op receipt.

Both directions come from one cause — the boundary between the header and
the op's answer was being *searched for* in text where it is ambiguous, when
the frame that joins them knows exactly where it is. These pin the verdict
to the op's own return value instead.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"

sys.path.insert(0, str(REPO))
import supertool  # noqa: E402


def _run(args: list, cwd: Path) -> tuple:
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", cwd=str(cwd),
        env={**os.environ, "SUPERTOOL_COAUTHOR": "Test Bot <bot@example.invalid>"},
    )
    return proc.returncode, proc.stdout + proc.stderr


def _git_repo(tmp_path: Path) -> Path:
    """A throwaway repo on the shipped git preset, with one dirty file."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}' + chr(10))
    (work / "a.txt").write_text("hi" + chr(10))
    subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=work, check=True)
    (work / "a.txt").write_text("hi there" + chr(10))
    return work


# An argument that spans lines and whose continuation is error-shaped. The
# op itself is a plain grep that finds nothing, which is a success.
POISONED_GREP = "grep:zzz ---\nERROR: nope:README.md"

# A commit message that quotes an op receipt — what `docs/contributing.md`
# asks for ("say what you actually verified"). The indented `--- git-status
# ---` line is the first ` ---\n` in the whole body, ahead of the header's
# own close.
RECEIPT_QUOTING_MESSAGE = (
    "fix: thing\n"
    "\n"
    "Verified with:\n"
    "\n"
    "    --- git-status ---\n"
    "    ok\n"
)


# --- (a) a successful op must not exit 1 -----------------------------------


def test_an_argument_that_looks_like_a_header_does_not_fail_a_successful_op(
    tmp_path: Path,
) -> None:
    """The grep ran, found nothing, and said so. That is exit 0."""
    (tmp_path / "README.md").write_text("nothing to see" + chr(10))
    rc, out = _run([POISONED_GREP], cwd=tmp_path)
    assert "0 results" in out, out
    assert rc == 0, out


def test_the_batch_tally_does_not_assert_a_refusal_that_never_happened(
    tmp_path: Path,
) -> None:
    """#1284's tally states the verdict in words, so a wrong bit becomes a
    wrong sentence. Two ops, both successful — nothing may say `refused`."""
    (tmp_path / "README.md").write_text("nothing to see" + chr(10))
    (tmp_path / "b.txt").write_text("fine" + chr(10))
    rc, out = _run([POISONED_GREP, "read:b.txt"], cwd=tmp_path)
    assert "refused" not in out, out
    assert rc == 0, out


# --- (b) a failing op must not exit 0 --------------------------------------


def test_a_refusal_whose_message_quotes_a_receipt_still_exits_one(
    tmp_path: Path,
) -> None:
    """Same refusal, two messages. The exit code may not depend on the prose.

    The control is not decoration: it is what makes this a test of the
    verdict rather than of git-commit refusing at all.
    """
    work = _git_repo(tmp_path)
    plain_rc, plain_out = _run(["git-commit:::fix: thing"], cwd=work)
    assert plain_rc == 1, plain_out
    quoted_rc, quoted_out = _run(
        ["git-commit:::" + RECEIPT_QUOTING_MESSAGE], cwd=work)
    assert "ERROR: no PATHS were given" in quoted_out, quoted_out
    assert quoted_rc == plain_rc, quoted_out


# --- what must keep working ------------------------------------------------


def test_a_failing_sub_op_inside_a_batch_still_flips_the_exit_code(
    tmp_path: Path,
) -> None:
    """The old marker reached sub-op headers by searching the batch body.
    Removing the search must not remove the reach."""
    (tmp_path / "ok.txt").write_text("alpha" + chr(10))
    payload = (
        '[[ops]]' + chr(10)
        + 'op = "read"' + chr(10)
        + 'path = "ok.txt"' + chr(10)
        + '[[ops]]' + chr(10)
        + 'op = "read"' + chr(10)
        + 'path = "no-such-file.txt"' + chr(10)
    )
    (tmp_path / "ops.toml").write_text(payload)
    rc, out = _run(["batch:@ops.toml"], cwd=tmp_path)
    assert "no-such-file.txt" in out, out
    assert rc == 1, out


def test_an_ops_own_output_containing_a_receipt_is_still_not_a_failure(
    tmp_path: Path,
) -> None:
    """A quoted receipt inside rendered CONTENT is the caller's data."""
    (tmp_path / "notes.md").write_text(
        "--- git-status ---" + chr(10) + "ERROR: quoted, not ours" + chr(10))
    rc, out = _run(["read:notes.md"], cwd=tmp_path)
    assert "ERROR: quoted, not ours" in out, out
    assert rc == 0, out


def test_the_verdict_is_not_a_scan_of_a_diff_shaped_receipt(
    tmp_path: Path,
) -> None:
    """This runs on every dispatch, and a `git-diff` receipt is thousands of
    lines beginning `--- a/path`. A lazy search over the body measured 4.0s
    on a 255KB body of that shape (#1279); the anchored form measured
    0.0013s. The bound is three orders above the anchored cost so a slow
    runner cannot redden it, and well below the searching form's.
    """
    big = tmp_path / "huge.diff"
    big.write_text(
        ("--- a/src/file.py" + chr(10) + "+++ b/src/file.py" + chr(10)
         + "@@ -1 +1 @@" + chr(10) + "-x" + chr(10) + "+y" + chr(10)) * 4000
    )
    start = time.monotonic()
    rc, out = _run(["read:huge.diff:0:100000"], cwd=tmp_path)
    elapsed = time.monotonic() - start
    assert rc == 0, out
    assert elapsed < 20.0, elapsed


def test_the_helper_that_read_the_rendered_body_is_gone() -> None:
    """The point of #1291 is that no verdict is taken from a rendered body.

    A replacement that leaves the old scanner in place beside it is the
    state this issue describes, not a fix for it.
    """
    assert not hasattr(supertool, "_body_indicates_failure")
    assert not hasattr(supertool, "_FAIL_MARKER")
