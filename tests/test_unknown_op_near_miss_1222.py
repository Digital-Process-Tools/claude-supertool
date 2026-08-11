"""#1222 + #1303 — the unknown-op refusal names the near miss, or says nothing.

`supertool 'worktrees'` printed 40 builtin names, none of which was the answer,
and then told the reader to make a second call to see the 47 preset ops — one of
which was `git-worktrees`. The name was already in the process; only the
printing was missing.

The bar is not "suggest something". A wrong suggestion sends the reader one
round-trip *further* away than silence does, so every rule here has to be one
that cannot fire on a coincidence:

* an explicit synonym, of which there are two and both are evidenced;
* a candidate that is exactly `<prefix>-<typed>` — `worktrees` -> `git-worktrees`,
  `blame` -> `git-blame`. A whole-segment match, never a substring;
* edit distance 1 (transposition counted as one), and only for names of four
  characters or more, where a coincidence needs the typo to land on a real op.

Nothing else is guessed at, and when nothing qualifies the message is exactly
what it was before.
"""
from __future__ import annotations

from pathlib import Path

import supertool

ROOT = Path(__file__).resolve().parent.parent


def _with_config(monkeypatch) -> None:
    """Load this repo's own config back — conftest pins `_CONFIG = {}`.

    Without it the candidate set is the builtins alone, and the preset half of
    #1222 (the whole point) could not be tested at all.
    """
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    assert supertool._load_config().get("ops"), (
        "no preset ops loaded — the near-miss set would be builtins only and "
        "the assertions below would be vacuous"
    )


def _suggestion_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("Did you mean:"):
            return line
    return ""


def test_a_preset_op_one_prefix_away_is_named(monkeypatch) -> None:
    """The filed case: `worktrees` is an exact segment of a loaded preset op."""
    _with_config(monkeypatch)
    line = _suggestion_line(supertool._unknown_op_message("worktrees"))
    assert "git-worktrees" in line, (
        f"the op the caller meant is loaded in this process and unnamed: {line!r}")


def test_blame_is_carried_to_the_op_that_replaced_it(monkeypatch) -> None:
    """`blame` was moved to the git preset as `git-blame` in b4099a5 (#1285)."""
    _with_config(monkeypatch)
    line = _suggestion_line(supertool._unknown_op_message("blame"))
    assert "git-blame" in line, line


def test_write_is_carried_to_paste() -> None:
    """#1303: the harness tool is `Write`, and the op that does it is `paste`.

    The mapping is already taught by `harness-tools-blocked.md`, which fires on
    every `Write` attempt in this repo. The refusal is the other place it is
    needed and the one place it was missing.
    """
    line = _suggestion_line(supertool._unknown_op_message("write"))
    assert "paste" in line, line


def test_vi_is_carried_to_vim() -> None:
    """`vi` sits in `_BUILTIN_OPS` from before the rename and dispatches nowhere.

    `_valid_op_names()` drops it on purpose, so `vi:f` is an unknown op whose
    intended target is not a guess but a documented fact about this file.
    """
    line = _suggestion_line(supertool._unknown_op_message("vi"))
    assert "vim" in line, line


def test_a_wrong_case_name_is_carried_to_itself_not_to_a_neighbour() -> None:
    """`Read:x` is not a typo of `head`. Found reviewing the first commit.

    Case is the one difference that is certainly not a typo of something else,
    and the distance rule could not see it: lowercased, `Read` is distance 0
    from `read`, which the "a name is not a suggestion for itself" guard threw
    out — leaving `head` at distance 1 as the top candidate. Naming the wrong op
    with a straight face is worse than the wall of names this replaced.
    """
    line = _suggestion_line(supertool._unknown_op_message("Read"))
    assert "read" in line, line
    assert "head" not in line, line


def test_a_transposed_builtin_is_named() -> None:
    line = _suggestion_line(supertool._unknown_op_message("raed"))
    assert "read" in line, line


def test_nothing_close_is_left_unguessed() -> None:
    """Silence is the correct third state, and the roster still follows."""
    out = supertool._unknown_op_message("zqxwvy")
    assert _suggestion_line(out) == "", out
    assert "Valid operations:" in out


def test_a_short_name_gets_no_distance_guess() -> None:
    """`lsx` is one edit from `ls`, and that is a coincidence, not a typo.

    Three characters is where edit distance stops carrying information; the
    floor is four. Without it, `abc`, `cwd`-adjacent junk and half the two-letter
    ops become each other's suggestions.
    """
    assert _suggestion_line(supertool._unknown_op_message("lsx")) == ""


def test_the_roster_is_not_replaced_by_the_suggestion() -> None:
    """The guess can still be wrong, so the list that cannot be stays."""
    out = supertool._unknown_op_message("raed")
    assert out.index("Did you mean:") < out.index("Valid operations:"), out
    assert "read" in out.split("Valid operations:", 1)[1]
