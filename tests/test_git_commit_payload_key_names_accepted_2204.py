"""#2204 part 2 -- git-commit's unknown-key refusal must name the accepted keys.

Filed from a requivo developer lane: `git-commit:@-` carrying `files` (a
reasonable guess from prose describing "which paths to commit") was
refused, and the ask was "if the op's own error message doesn't already
name the accepted key set on a rejected/unknown key, that would close this
one outright."

It already does. `_supertool.py`'s `_payload_unknown_fields`/
`_payload_accepted_fields` machinery (#1551) is generic across every op's
@payload route, not specific to `git-commit`, and the refusal already reads
`... has unknown field(s) files -- accepted: message, paths[, no_verify]`.
No code changed for this half of #2204 -- this test pins the behaviour so a
future refactor of the payload route cannot silently drop it for this op.

Part 1 of #2204 (`edit`'s backslash guard on an escaped-newline literal) is
a separate, unrelated defect and out of scope here.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n', encoding="utf-8")
    (work / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    return work


def test_a_guessed_files_key_is_refused_naming_the_accepted_keys(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    payload = 'message = "test"\nfiles = ["a.txt"]\n'

    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), "git-commit:@-"],
        input=payload, capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace", cwd=str(work),
    )
    out = proc.stdout + proc.stderr

    assert "unknown field(s) files" in out, out
    assert "accepted: message, paths" in out, out
    # And nothing was committed under the guessed key.
    head = subprocess.run(
        ["git", "log", "-1", "--pretty=format:%s"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout
    assert head == "seed"
