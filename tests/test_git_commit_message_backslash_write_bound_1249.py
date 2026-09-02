r"""A doubled backslash in a `git-commit` payload `message` is refused, not
warned-and-committed (#1249, item 3).

The double-backslash detector treats a field as write-bound -- refused rather
than merely noted -- only if landing the wrong bytes matters, per
`_PAYLOAD_DBS_WRITE_KEYS`. A commit message was outside that set: the field
carries no risk of a non-match (unlike `old`, which the note-only half exists
for), it lands verbatim in permanent history exactly like `new`/`content`
lands in a file, and #1249 recorded the consequence directly -- a commit
message ABOUT backslash misreporting that itself misreported backslashes,
because the guard warned and the commit proceeded anyway, and the correction
afterwards needed a raw `git` amend outside the payload route entirely.

`message` now shares `_PAYLOAD_DBS_WRITE_KEYS` with `new`/`content`: refused
by default, suppressible with `literal_backslashes`, same as any other
write-bound field. The `old`-shaped case has no analogue here (a message has
no "anchor" reading) so there is no note-only sibling to preserve.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"
COAUTHOR = "Test Bot <bot@example.invalid>"

BS = chr(92)
NL = chr(10)
Q3 = chr(39) * 3


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n')
    (work / "a.txt").write_text("hi\n")
    subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
    return work


def _run(args: list, cwd: Path, stdin: str = "") -> tuple:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _log_count(cwd: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return int(out.stdout.strip()) if out.returncode == 0 else 0


def test_doubled_backslash_in_message_is_refused_not_committed(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    payload = (
        "message = " + Q3 + "about " + BS * 2 + "d+ paths" + Q3 + NL
        + 'paths = ["a.txt"]' + NL
    )
    code, out, err = _run(["git-commit:@-"], cwd=work, stdin=payload)
    assert "refused" in (out + err).lower(), out + err
    assert "literal" in (out + err).lower(), out + err
    assert _log_count(work) == 0, "no commit should have landed"


def test_the_flag_still_lands_the_intended_bytes(tmp_path: Path) -> None:
    """The suppressible escape hatch applies to `message` exactly as it does
    to any other write-bound field."""
    work = _repo(tmp_path)
    payload = (
        "literal_backslashes = true" + NL
        + "message = " + Q3 + "about " + BS * 2 + "d+ paths" + Q3 + NL
        + 'paths = ["a.txt"]' + NL
    )
    code, out, err = _run(["git-commit:@-"], cwd=work, stdin=payload)
    assert code == 0, f"stdout={out} stderr={err}"
    body = subprocess.run(
        ["git", "log", "-1", "--pretty=format:%B"],
        cwd=work, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout
    assert "about " + BS * 2 + "d+ paths" in body, body


def test_a_single_backslash_in_a_message_is_not_flagged(tmp_path: Path) -> None:
    """The common, correct case -- a genuine single backslash (e.g. quoting a
    regex pattern in a commit body) must not be refused."""
    work = _repo(tmp_path)
    payload = (
        "message = " + Q3 + "about " + BS + "d+ paths" + Q3 + NL
        + 'paths = ["a.txt"]' + NL
    )
    code, out, err = _run(["git-commit:@-"], cwd=work, stdin=payload)
    assert code == 0, f"stdout={out} stderr={err}"
    assert _log_count(work) == 1
