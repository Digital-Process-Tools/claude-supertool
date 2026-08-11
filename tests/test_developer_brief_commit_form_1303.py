"""#1303 — the developer brief prescribes a `git-commit` form that always refuses.

`.claude/agents/opensource-developer.md` spells the commit step as
`git-commit:::MESSAGE` with no paths. `git-commit` never stages, so that form is
refused — correctly, and with a good receipt. But it is the form the repo hands
every delegated agent, so the first commit attempt of every run fails by
construction on the repo's own advice.

The refusal is not the bug; the doc is. Asserted by *running* each form the doc
prescribes against a scratch repo with a dirty file, rather than by pattern-
matching the prose — a prose assertion would go green on any rewording, and this
one only goes green when the command in the file actually commits.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPERTOOL = REPO / "supertool.py"
BRIEF = REPO / ".claude" / "agents" / "opensource-developer.md"

# Inline code spans. The op is usually not at the start of one — the brief writes
# `supertool 'git-commit:::MESSAGE'` — so the form is taken from `git-commit` to
# the end of the span, minus the quote that closed the shell argument.
_SPAN = re.compile(r"`([^`\n]+)`")
_QUOTES = "'" + '"'


def _prescribed_forms() -> list:
    """Every `git-commit:...` form the brief tells an agent to type.

    A bare `git-commit` with no `:` is a mention of the op, not a prescription of
    a form, and is skipped — running it would refuse and that says nothing about
    the sentence it sits in.
    """
    forms = []
    for m in _SPAN.finditer(BRIEF.read_text(encoding="utf-8")):
        span = m.group(1)
        at = span.find("git-commit")
        if at < 0:
            continue
        form = span[at:].strip().rstrip(_QUOTES)
        # `@`-routes read their fields from a payload the prose does not carry,
        # so there is no command to run here. The colon forms are the ones an
        # agent pastes verbatim, and the one that was wrong.
        if ":" in form and "@" not in form:
            forms.append(form)
    return forms


def _scratch_repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n',
                                          encoding="utf-8")
    (work / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    return work


def _head(work: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True,
        text=True, check=True).stdout.strip()


def test_the_brief_prescribes_at_least_one_commit_form() -> None:
    """The sweep below is vacuous if the regex stops matching the file."""
    assert _prescribed_forms(), (
        f"no `git-commit...` code span found in {BRIEF.name} — the check below "
        f"would pass over nothing")


def test_every_commit_form_the_brief_prescribes_actually_commits(
        tmp_path) -> None:
    work = _scratch_repo(tmp_path)
    for form in _prescribed_forms():
        arg = form.replace("MESSAGE", "fix: a real subject")
        arg = arg.replace("PATHS...", "a.txt").replace("PATHS", "a.txt")
        before = _head(work)
        env = dict(os.environ)
        env["SUPERTOOL_COAUTHOR"] = "Test Bot <bot@example.invalid>"
        proc = subprocess.run(
            [sys.executable, str(SUPERTOOL), arg],
            capture_output=True, text=True, timeout=120, cwd=str(work),
            encoding="utf-8", errors="replace", env=env,
        )
        out = proc.stdout + proc.stderr
        assert "ERROR" not in out, (
            f"{BRIEF.name} prescribes `{form}`, and running it refuses:" + out)
        assert _head(work) != before, (
            f"`{form}` produced no commit:" + out)
        (work / "a.txt").write_text(_head(work) + "\n", encoding="utf-8")
