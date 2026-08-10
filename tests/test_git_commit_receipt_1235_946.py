"""git-commit's receipt: what the header replays, and what a refusal suggests.

Two issues, one lane (#1235, #946).

#946 asked for the message not to be replayed above a receipt that already
proves the commit landed. #1235 asked for the same replay to stop happening
on a call that was going to be refused. They are the SAME line of code — the
core's `--- {arg} ---` header — and they want opposite things from it, which
is why they are pinned together here:

  * on SUCCESS the message is in the commit, so the header is a second copy
    of something `git log` will hand back;
  * on a REFUSAL nothing was committed and the header is the ONLY surviving
    copy of the message the caller composed. Eliding it there would cause
    exactly the loss #1235 is worried about.

The third group is the load-bearing one, and it is neither issue's headline:
the refusal that fires when a PATH looks like spilled message prose rebuilds
the caller's message to suggest a repair, and it rebuilt it with the WRONG
separator. Pasting the suggestion committed a corrupted message under a
refusal's authority.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"

COAUTHOR = "Test Bot <bot@example.invalid>"
Q1 = "'"
Q3 = Q1 * 3

LONG_MESSAGE = (
    "feat(git): a subject line long enough to be worth composing\n"
    "\n"
    "Body line one explaining the mechanism in some detail at length.\n"
    "Body line two explaining the reasoning in some detail at length.\n"
    "Body line three with the judgment call spelled out at some length."
)


def _repo(tmp_path: Path) -> Path:
    """A throwaway git repo on the shipped git preset, with one commit."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n')
    (work / "a.txt").write_text("hi\n")
    subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=work, check=True)
    (work / "a.txt").write_text("hi there\n")
    return work


def _run(args: list, cwd: Path, stdin: str = "") -> tuple:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _header(out: str) -> str:
    """Everything up to and including the closing `---` of the op header."""
    start = out.index("--- ")
    return out[: out.index(" ---", start) + 4]


# --- the load-bearing group: a repair suggestion that corrupts the thing -----

TRIPLE_MSG = "fix: thing a::: and more prose"


def test_triple_colon_message_is_rebuilt_with_the_separator_it_was_split_on(
    tmp_path: Path,
) -> None:
    """A ':::' inside the message must survive the refusal that names it.

    The message was split on ':::' and rejoined on ':', so the suggested
    repair silently rewrote the caller's own bytes. Pasting it commits a
    message the caller never wrote, under a refusal that was otherwise right.
    """
    work = _repo(tmp_path)
    code, out = _run(["git-commit:::" + TRIPLE_MSG + ":::a.txt"], cwd=work)
    assert code != 0, out
    msg_lines = [ln.strip() for ln in out.splitlines()
                 if ln.strip().startswith("message = ")]
    assert msg_lines, out
    assert msg_lines[0] == "message = " + Q3 + TRIPLE_MSG + Q3, msg_lines[0]


def test_the_single_colon_reading_is_offered_but_says_it_rewrites(
    tmp_path: Path,
) -> None:
    """Both readings, and the lossy one is labelled as lossy.

    A ':::' typed where a ':' was meant is a real way to land here, and
    #963 pins that the colon form handed back must actually commit the
    subject the caller wanted — so withholding it entirely would trade one
    defect for a worse remedy. What must not happen is what did: the rewrite
    offered as though it were the message that had been typed.
    """
    work = _repo(tmp_path)
    code, out = _run(["git-commit:::" + TRIPLE_MSG + ":::a.txt"], cwd=work)
    assert code != 0, out
    colon = [ln.strip() for ln in out.splitlines()
             if ln.strip().startswith("./supertool " + Q1 + "git-commit:::")]
    assert len(colon) == 1, out
    assert "fix: thing a: and more prose" in colon[0], colon[0]
    # The rewrite has to be stated, above the line that performs it.
    lines = [ln.strip() for ln in out.splitlines()]
    at = lines.index(colon[0])
    assert "becomes" in " ".join(lines[max(0, at - 2):at]), out
    # And the byte-faithful route is offered FIRST.
    assert out.index("git-commit:@-") < out.index(
        "./supertool " + Q1 + "git-commit:::"), out


def test_payload_route_refusal_does_not_claim_a_split_that_never_happened(
    tmp_path: Path,
) -> None:
    """`paths = [...]` fields were never tokenized, so nothing was split.

    The refusal claimed the message "was split on ':'" and then FUSED the
    message and the prose-looking path into a single string it invited the
    caller to commit. Both are statements about a parse that did not run.
    """
    work = _repo(tmp_path)
    payload = (
        "message = " + Q3 + "subject: here" + Q3 + "\n"
        + 'paths = ["some prose here", "a.txt"]\n'
    )
    code, out = _run(["git-commit:@-"], cwd=work, stdin=payload)
    assert code != 0, out
    assert "split on" not in out, out
    assert "subject: here:some prose here" not in out, out
    assert "some prose here" in out, out


# --- #946 / #1235: what the header replays ---------------------------------


def test_long_message_header_is_summarised_on_a_successful_commit(
    tmp_path: Path,
) -> None:
    """The commit landed, so `git log` holds the message — the header need not.

    Subject and a body line count, not the body. The receipt underneath is
    what proves the call did something.
    """
    work = _repo(tmp_path)
    code, out = _run(["git-commit:::" + LONG_MESSAGE + ":::a.txt"], cwd=work)
    assert code == 0, out
    head = _header(out)
    assert "Body line one" not in head, head
    assert "Body line three" not in head, head
    assert "feat(git): a subject line" in head, head
    assert "4 more message lines" in head, head
    assert "a.txt" in head, head


def test_a_refused_commit_keeps_the_verbatim_message_in_its_header(
    tmp_path: Path,
) -> None:
    """Nothing was committed, so the header is the only copy that exists.

    This is the half of #1235 that must NOT be optimised: the composed
    message is expensive to lose and the receipt is the only place it
    survives a refused call.
    """
    work = _repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "--", "a.txt"], cwd=work,
                   check=True)
    code, out = _run(["git-commit:::" + LONG_MESSAGE], cwd=work)
    assert code != 0, out
    head = _header(out)
    assert "Body line one" in head, head
    assert "Body line three" in head, head


def test_a_short_message_keeps_its_verbatim_header(tmp_path: Path) -> None:
    """The #384 length gate is not lowered here — a short op is unchanged."""
    work = _repo(tmp_path)
    code, out = _run(["git-commit:::fix: short one:::a.txt"], cwd=work)
    assert code == 0, out
    assert _header(out).startswith("--- git-commit:::fix: short one:::a.txt")
