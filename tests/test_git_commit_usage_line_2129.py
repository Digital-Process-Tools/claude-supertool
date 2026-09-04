"""#2129 -- git-commit's usage line must not read as a literal to paste.

An agent reading `help:git-commit` at speed, just after a refusal, pasted
the literal string `MESSAGE` as a commit subject: the syntax line's own
placeholder convention (`git-commit:::MESSAGE:::path1:::path2`, as the
issue reported it) mixed an uppercase MESSAGE with lowercase path1/path2
neighbours, which teaches no consistent "uppercase means substitute" rule.

The live syntax line (`presets/git.json`) is already fully uppercase --
`git-commit:::MESSAGE[:::PATHS...|:::--all][:::--no-verify]` -- matching
every other op in this file (PATH, PATTERN, BASE, LINE, N, REF, BRANCH,
SIDE, BLOCKS, SECONDS all uppercase, no angle brackets: introducing a
second placeholder spelling for one op would be the "third spelling" the
issue itself asked not to invent). What closes the loop for a reader
moving fast is the `example` field this test pins -- the same mechanism
`presets/vim.json`, `presets/gitlab.json`, `presets/bluesky.json` and
others already use, rendered as an `Example:` line in `help:OP` -- a
concrete, real-looking call that cannot be read as a template to paste
verbatim, because MESSAGE never appears in it.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_GIT_JSON = _ROOT / "presets" / "git.json"


def _git_commit_op() -> dict:
    return json.loads(_GIT_JSON.read_text(encoding="utf-8"))["ops"]["git-commit"]


def test_syntax_line_placeholders_are_all_uppercase() -> None:
    """No lowercase placeholder sits beside an uppercase one (the reported defect)."""
    syntax = _git_commit_op()["syntax"]
    assert "path1" not in syntax and "path2" not in syntax, syntax
    assert syntax == "git-commit:::MESSAGE[:::PATHS...|:::--all][:::--no-verify]", syntax


def test_example_field_shows_a_real_message_not_a_placeholder() -> None:
    """A worked example, so a fast reader has a real call to copy instead of
    guessing whether MESSAGE is literal (#2129) -- same field vim/gitlab/
    bluesky presets already use."""
    example = _git_commit_op()["example"]
    assert example == "git-commit:::fix: correct the off-by-one:::src/foo.py", example
    assert "MESSAGE" not in example
    assert "PATHS" not in example


def test_help_renders_the_example_line() -> None:
    """The registry field actually reaches `help:git-commit`'s output."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(_ROOT / "supertool.py"), "help:git-commit"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    out = proc.stdout + proc.stderr
    assert "Example: git-commit:::fix: correct the off-by-one:::src/foo.py" in out, out
