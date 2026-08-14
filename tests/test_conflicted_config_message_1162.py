"""#1162 — a config found and skipped must not be reported as absent.

Git writes conflict markers into `.supertool.json` during a rebase. The file
stops parsing, so `_load_config` skips it and keeps walking; with no usable
config above, no preset ops load. The refusal a caller then meets said

    No .supertool.json was found from X or any parent

one line under a stderr warning naming the file it had just skipped. Two lines
of one render disagreeing about whether the config exists — and the reader who
sees only the ERROR goes looking for a missing file that is present.

Worse, the ops that are missing are `git-conflicts`, `git-resolve` and
`git-status`: the situation removes exactly the tools for the situation. The
message cannot fix that circularity, so it says what is true instead — which
file, that it holds markers, and on which lines.

Deliberately **not** fixed by loading the git preset from defaults. Running ops
out of a config the user cannot see is a guess, and this repo's contract is
against guessing (`docs/validators.md`, "Declining instead of guessing").
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import supertool


CONFLICTED = """{
  "presets": ["git"],
<<<<<<< HEAD
  "validators": {"html-check": {"match": "*.html"}}
=======
  "validators": {"changelog-fragment": {"match": "changelog.d/*.md"}}
>>>>>>> origin/master
}
"""


def _names_the_config(out: str, cwd: Path) -> bool:
    """The message builds its path from `os.getcwd()`, which may not be spelled
    the way `tmp_path` is — `/var` against `/private/var` on macOS, and a short
    8.3 form is possible on Windows. Same hedge `test_op_unavailable_here_614`
    already uses."""
    return (str(cwd / ".supertool.json") in out
            or os.path.join(os.path.realpath(str(cwd)), ".supertool.json") in out)


def _stand_in(monkeypatch: pytest.MonkeyPatch, cwd: Path) -> None:
    """Force a real config walk from *cwd* instead of conftest's pinned {}."""
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)


def test_a_conflicted_config_is_not_reported_as_a_missing_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".supertool.json").write_text(CONFLICTED, encoding="utf-8")
    _stand_in(monkeypatch, tmp_path)
    out = supertool._unknown_op_message("git-conflicts")
    assert "No .supertool.json was found" not in out, out
    assert _names_the_config(out, tmp_path), out


def test_the_conflict_is_named_as_a_conflict_and_the_marker_lines_given(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one fact `git-conflicts` would have given, from the op that cannot."""
    (tmp_path / ".supertool.json").write_text(CONFLICTED, encoding="utf-8")
    _stand_in(monkeypatch, tmp_path)
    out = supertool._unknown_op_message("git-conflicts")
    assert "conflict marker" in out, out
    # The whole rendered span, not the digits: a pytest tmp path holds digits
    # of its own, so `"3" in out` would have passed against a message that
    # named no lines at all.
    assert "(lines 3, 5, 7)" in out, out


def test_the_dead_remedies_are_not_offered_for_a_conflicted_config(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`cwd:<other-project>` answers about a different repository (#678's shape).

    The git ops act on the working directory, so retargeting reports another
    repo's conflicts. There is also no other root to move to: the conflict is
    in the repo the caller is standing in.
    """
    (tmp_path / ".supertool.json").write_text(CONFLICTED, encoding="utf-8")
    _stand_in(monkeypatch, tmp_path)
    out = supertool._unknown_op_message("git-conflicts")
    assert "cwd:<project-path>" not in out, out
    assert "Run it from a project that enables" not in out, out


def test_an_unparseable_config_that_is_not_conflicted_says_so_without_guessing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain broken JSON is a different sentence: found, unusable, not a merge."""
    (tmp_path / ".supertool.json").write_text('{"presets": ["git",}',
                                              encoding="utf-8")
    _stand_in(monkeypatch, tmp_path)
    out = supertool._unknown_op_message("git-conflicts")
    assert "No .supertool.json was found" not in out, out
    assert _names_the_config(out, tmp_path), out
    assert "does not parse" in out, out
    assert "conflict marker" not in out, out


def test_a_genuinely_absent_config_still_says_absent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction. A found-and-skipped note on an empty directory
    would be the same defect mirrored — a presence the tool invented."""
    _stand_in(monkeypatch, tmp_path)
    out = supertool._unknown_op_message("git-conflicts")
    assert "No .supertool.json was found" in out, out
    assert "conflict marker" not in out, out


def test_a_valid_config_that_omits_the_preset_is_untouched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".supertool.json").write_text(json.dumps({"presets": []}),
                                              encoding="utf-8")
    _stand_in(monkeypatch, tmp_path)
    out = supertool._unknown_op_message("git-conflicts")
    assert "does not" in out and "enable" in out, out
    assert "conflict marker" not in out, out
    assert "does not parse" not in out, out


def test_the_preset_disclosure_line_agrees_with_the_refusal(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ops` renders `_preset_disclosure()`, which carried the same sentence.

    Fixing one render and leaving the other is how the two disagree again.
    """
    (tmp_path / ".supertool.json").write_text(CONFLICTED, encoding="utf-8")
    _stand_in(monkeypatch, tmp_path)
    line = supertool._preset_disclosure()
    assert "No .supertool.json was found" not in line, line
    assert "conflict marker" in line, line
