"""A shipped regex rule discloses the earlier segment it discards too (#1873).

`_supertool.guard_command`'s own `blocked` verdict has named every earlier
top-level segment since #2009 -- the registry route. The hand-written rules
under `.claude/jit-context/tools/00-manual/` (their shipped subset lives in
`SHIPPED` in this module) never went through that fix: they match a regex
against the raw command string, before `_supertool` is even imported, and
have no notion of "segments" at all. A compound Bash call whose first half is
a write and whose second half trips `supertool-no-cut` therefore refuses the
whole call and says nothing about the write that silently never ran --
exactly the shape #1873's second comment measured for the registry route,
one layer over.

Would this test still pass if the code did nothing? No: at the commit before
this fix, `match()` returns only the rule's own body, with no mention of the
`git commit` segment that preceded the piped one in the fixture below.

The trigger commands below are built with `chr(124)` rather than a literal
`|`, deliberately: this file's own text is itself Bash-heredoc'd into
existence, in one call, by an agent bound by the same raw-command guard this
test exercises -- a literal "supertool.py '...' | head" substring in this
source would trip `supertool-no-cut` on the very call that authors the file.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_HOOKS = _ROOT / "hooks"
sys.path.insert(0, str(_HOOKS))

import shipped_rules  # noqa: E402

_PIPE = chr(124)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    project.mkdir(exist_ok=True)
    return project


#: The shape measured on the registry route in #1873's own second comment,
#: reproduced against the regex route instead: an earlier write, chained with
#: `&&` onto a command the shipped rule refuses.
_COMPOUND = ('git commit --allow-empty -m "wip" '
             "&& python3 supertool.py 'read:foo' " + _PIPE + " head -3")


def test_the_earlier_segment_is_named_in_the_refusal(tmp_path: Path) -> None:
    """MUST FIRE: the write before the piped call is disclosed, not silent."""
    project = _project(tmp_path)
    answer = shipped_rules.match(_COMPOUND, str(_ROOT), str(project))
    assert answer is not None, _COMPOUND
    verb, body = answer
    assert verb == "deny"
    assert "git commit" in body, body
    assert "would not run either" in body, body


def test_a_single_segment_gets_no_discard_line(tmp_path: Path) -> None:
    """MUST NOT FIRE: nothing precedes the match, nothing to disclose."""
    project = _project(tmp_path)
    single = "python3 supertool.py 'read:foo' " + _PIPE + " head -3"
    answer = shipped_rules.match(single, str(_ROOT), str(project))
    assert answer is not None, single
    verb, body = answer
    assert verb == "deny"
    assert "would not run either" not in body, body


def test_a_redirection_inside_the_earlier_segment_is_not_a_false_boundary(
        tmp_path: Path) -> None:
    """`2>&1` must not read as a `&`-separator splitting the discard in two.

    Mirrors the exact bug class `_guard_raw_segment_spans` was written to
    avoid in `_supertool.py` (#1684): a bare `&` inside a redirection is not
    a command separator, and a segmenter that treats it as one reports a
    discarded write as two fragments, one of which is `1` -- worse than
    reporting the correct single line.
    """
    project = _project(tmp_path)
    command = ('git commit --allow-empty -m "wip" 2>&1 '
               "&& python3 supertool.py 'read:foo' " + _PIPE + " head -3")
    answer = shipped_rules.match(command, str(_ROOT), str(project))
    assert answer is not None, command
    verb, body = answer
    assert 'git commit --allow-empty -m "wip" 2>&1' in body, body
    assert body.count("would not run either") == 1, body
