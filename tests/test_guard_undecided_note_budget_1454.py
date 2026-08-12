"""The undecided disclosure had no budget, so it amplified (#1454).

#1449 capped `guard_refusal()` after a reviewer produced a 61,942-character
refusal from 200 chained ambiguous segments. Two other renderers join
`verdict.notes` and neither was capped:

    200 x `git --unknownoptN commit -m x` joined with `&&`
      guard_undecided_note()               27,634 chars
      hooks/pre_bash_guard.py              27,632 chars   (its own join)

Nothing is blocked on that path and nothing runs that should not -- the cost is
a wall of text injected into the operator's context by a hook whose whole job is
a one-line disclosure. Size set by accident- or attacker-controlled input, paid
on every occurrence.

The repair is one bounded formatter that every renderer calls, rather than a
second and a third cap: the hook re-implemented the join instead of calling
`guard_undecided_note`, which is precisely how a third channel inherits the gap.

Would these pass if the code did nothing? No -- both lengths are five figures at
c685eaf.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def shipped_presets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["git", "github"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    return supertool._load_config()


def _amplified() -> str:
    """#1421's per-segment note, one distinct note per segment."""
    return " && ".join(f"git --unknownopt{i:05d} commit -m x"
                       for i in range(200))


class TestTheUndecidedNoteIsBounded:

    def test_two_hundred_notes_do_not_multiply_the_disclosure(
            self, shipped_presets):
        verdict = supertool.guard_command(_amplified())
        assert verdict.state == "undecided", verdict.state
        assert len(verdict.notes) == 200, len(verdict.notes)
        text = supertool.guard_undecided_note(verdict)
        # The same order of magnitude `guard_refusal` is held to: a budget of
        # registry-derived text plus framing, not 200 copies of it.
        assert len(text) < 3000, len(text)

    def test_what_it_dropped_is_counted_rather_than_silently_cut(
            self, shipped_presets):
        text = supertool.guard_undecided_note(
            supertool.guard_command(_amplified()))
        assert "further note" in text, text[-400:]

    def test_it_still_says_the_command_was_allowed(self, shipped_presets):
        # The sentence that makes this a statement about the guard rather than
        # about the command must survive the budget -- truncating a disclosure
        # into its own middle is the defect wearing the fix.
        text = supertool.guard_undecided_note(
            supertool.guard_command(_amplified()))
        assert "allowed" in text, text[-400:]

    def test_a_short_note_list_is_not_truncated(self, shipped_presets):
        verdict = supertool.GuardVerdict("undecided", (), ("alpha", "beta"))
        text = supertool.guard_undecided_note(verdict)
        assert "alpha" in text and "beta" in text, text
        assert "further note" not in text, text


class TestTheHookPrintsTheBoundedText:
    """The shipped `PreToolUse` hook is the renderer this actually reaches."""

    @staticmethod
    def _run(command: str, cwd: Path) -> Dict[str, Any]:
        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": command}})
        env = dict(os.environ)
        env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
        proc = subprocess.run(
            [sys.executable, str(_ROOT / "hooks" / "pre_bash_guard.py")],
            input=payload, capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(cwd), env=env, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def test_the_additional_context_is_bounded(self, tmp_path):
        (tmp_path / ".supertool.json").write_text(
            json.dumps({"presets": ["git", "github"]}), encoding="utf-8")
        out = self._run(_amplified(), tmp_path)
        context = out["hookSpecificOutput"]["additionalContext"]
        assert len(context) < 3000, len(context)
        assert "further note" in context, context[-400:]

    def test_it_still_discloses_and_does_not_deny(self, tmp_path):
        (tmp_path / ".supertool.json").write_text(
            json.dumps({"presets": ["git", "github"]}), encoding="utf-8")
        out = self._run(_amplified(), tmp_path)["hookSpecificOutput"]
        assert out.get("permissionDecision") is None, out
        assert "guard" in out["additionalContext"], out


class TestAntiVacuity:
    """A bound that holds because nothing was rendered is not a bound."""

    def test_one_note_is_rendered_in_full(self, shipped_presets):
        verdict = supertool.guard_command("git --unknownopt commit -m x")
        assert verdict.state == "undecided", verdict
        text = supertool.guard_undecided_note(verdict)
        assert verdict.notes[0] in text, (verdict.notes, text)
