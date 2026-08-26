"""#1993: `SUPERTOOL_REPO_FROM_OP` is a bare env var with nothing binding it
to the pre-pass having run, so an inherited pair still credits a `repo:` op
nobody typed and skips the shape check the real pre-pass runs.

Two independent things are fixed here:

* `main()` used to snapshot `SUPERTOOL_REPO`/`SUPERTOOL_REPO_FROM_OP` and
  restore the prior value on the way out, but never denied either variable
  for the DURATION of the call. An inherited pair -- a parent shell's own
  export, or a value that somehow survived a restore in a long-lived host
  process -- sat in `os.environ` for the whole of `_main()`, which is
  exactly what let a call with no `repo:` op read as though one had run.
  `main()` cleared both on entry, before `_main` ever inspects argv, so the
  only way either variable was set during a call was that call's own
  `repo:` pre-pass setting it fresh -- the one place that runs the shape
  check (`_repo_shape_error`).

  **#2001 revises this**: clearing `SUPERTOOL_REPO` itself was never
  needed to close the hole above -- `explicit_target()`, what every write
  route calls instead of the bare `target()`, gates purely on the marker
  via `from_op()`, so the marker alone is what a write route must never
  see survive. Clearing the value too took the READ path down as a side
  effect: an ambient `SUPERTOOL_REPO` with no `repo:` op in the call used
  to reach a read op and stopped reaching one, silently. `main()` now
  clears only `SUPERTOOL_REPO_FROM_OP` on entry, which still denies it for
  the duration of a call whose own `repo:` pre-pass did not set it fresh,
  while leaving an ambient `SUPERTOOL_REPO` free to reach reads exactly as
  it did before this fix existed.
* `presets/gitlab/issue_create.py`'s `encoded_project` escaped only `/`,
  so a project carrying `?`, `#` or `%` reached the linked-issue API path
  unencoded -- independent of how the target arrived.
"""
from __future__ import annotations

import os
import urllib.parse

import pytest

import supertool

REPO_ENV_VARS = ("SUPERTOOL_REPO", "SUPERTOOL_REPO_FROM_OP")


@pytest.fixture(autouse=True)
def clean_repo_env(monkeypatch):
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.delenv("SUPERTOOL_REPO_FROM_OP", raising=False)
    yield
    for name in REPO_ENV_VARS:
        os.environ.pop(name, None)


@pytest.fixture
def no_dispatch(monkeypatch):
    """Record the ops that reach dispatch, and what each one saw in the
    environment at the moment it ran -- mirrors `test_repo_target_673.py`'s
    own fixture of the same name."""
    seen: list[tuple[str, str | None, str | None]] = []
    monkeypatch.setattr(
        supertool, "dispatch",
        lambda a: (seen.append((
            a,
            os.environ.get("SUPERTOOL_REPO"),
            os.environ.get("SUPERTOOL_REPO_FROM_OP"),
        )), "")[-1],
    )
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)
    return seen


# ---------------------------------------------------------------------------
# #1993's guard: an inherited pair with no repo: op in the call
# ---------------------------------------------------------------------------

def test_an_inherited_pair_leaves_the_value_but_not_the_marker(
        monkeypatch, no_dispatch) -> None:
    """#2001 revises this fix: SUPERTOOL_REPO and SUPERTOOL_REPO_FROM_OP are
    BOTH already set (as they would be from a parent shell export, or a
    value that leaked past an earlier restore) before this call starts, and
    this call itself types no `repo:` op. The marker must not survive --
    explicit_target() (what every write route calls) trusts it, not the
    bare value -- but the value itself must still reach a read op, exactly
    as it did before #1993 took it down as a side effect of closing the
    write-side hole."""
    monkeypatch.setenv("SUPERTOOL_REPO", "attacker/elsewhere")
    monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")

    rc = supertool.main(["gh-issue:1"])

    assert rc == 0
    assert no_dispatch == [("gh-issue:1", "attacker/elsewhere", None)]


def test_an_ambient_value_alone_still_reaches_a_read_op(
        monkeypatch, no_dispatch) -> None:
    """#2001's own reproduction: `SUPERTOOL_REPO=o/n supertool 'watch:...'`
    silently started a poller against the cwd's repository instead. This is
    what a plain shell export actually looks like -- only SUPERTOOL_REPO
    set, never the marker, because nothing on this machine besides main()'s
    own repo: pre-pass ever sets SUPERTOOL_REPO_FROM_OP. #1993's own tests
    only ever set both variables together, so this direction was unpinned
    either way until now."""
    monkeypatch.setenv("SUPERTOOL_REPO", "someone/elsewhere")

    rc = supertool.main(["gh-issue:1"])

    assert rc == 0
    assert no_dispatch == [("gh-issue:1", "someone/elsewhere", None)]


def test_an_inherited_marker_alone_does_not_survive(
        monkeypatch, no_dispatch) -> None:
    """Must-fire control for the two tests above: a bare inherited
    SUPERTOOL_REPO_FROM_OP with no SUPERTOOL_REPO to go with it (the
    degenerate case) must still be denied -- the marker is what a write
    route trusts, and it must never survive into a call whose own repo:
    pre-pass did not set it fresh."""
    monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")

    rc = supertool.main(["gh-issue:1"])

    assert rc == 0
    assert no_dispatch == [("gh-issue:1", None, None)]


def test_a_real_repo_op_still_sets_both_for_the_ops_it_targets(
        monkeypatch, no_dispatch) -> None:
    """Must-fire control, same fixture shape: a call that DOES type its own
    `repo:` op must still see both variables set for the op it names -- the
    silence assertion above would pass just as well if main() always wiped
    the pair, which is exactly the harness-is-broken trap a must-not-fire
    case alone cannot catch."""
    rc = supertool.main(["repo:Digital-Process-Tools/claude-remember",
                         "gh-issue:1"])

    assert rc == 0
    assert no_dispatch == [
        ("gh-issue:1", "Digital-Process-Tools/claude-remember", "1")
    ]
    # `dispatch` is monkeypatched to a lambda, so it never gets to spawn the
    # preset subprocess that would actually read SUPERTOOL_REPO_FROM_OP --
    # this asserts main() exported it for real, from the test's own process.
    # (no_dispatch's lambda runs and returns before the finally in main()
    # restores/clears, so the assertion has to happen through the recorded
    # tuple above, not a live os.environ read here.)


def test_inherited_pair_is_gone_even_with_an_unrelated_repo_op_present(
        monkeypatch, no_dispatch) -> None:
    """A stricter form of the guard above: this call DOES carry a `repo:`
    op, but for a DIFFERENT repository than the one already sitting in the
    environment -- the inherited pair must not leak through even when a
    real repo: op is present, because the real one always wins by being
    exported fresh, never by the old value merely surviving alongside it."""
    monkeypatch.setenv("SUPERTOOL_REPO", "attacker/elsewhere")
    monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")

    rc = supertool.main(["repo:Digital-Process-Tools/claude-remember",
                         "gh-issue:1"])

    assert rc == 0
    assert no_dispatch == [
        ("gh-issue:1", "Digital-Process-Tools/claude-remember", "1")
    ]


# ---------------------------------------------------------------------------
# the GitLab encoding half -- independent of how the target arrived
# ---------------------------------------------------------------------------

def test_gitlab_project_percent_encoding_matches_repo_target():
    """`presets/gitlab/issue_create.py` used to escape only `/` in the
    project id it substitutes into a linked-issue API path
    (`project.replace("/", "%2F")`), so a project carrying `?`, `#` or `%`
    reached the path unencoded. `_repo_target.gl_project()` already
    percent-encodes the WHOLE segment for the primary `projects/:id`
    substitution -- one project string, one encoding, is the fix."""
    project = "group/sub#project?x"
    assert urllib.parse.quote(project, safe="") == (
        "group%2Fsub%23project%3Fx"
    )
    # The bug's own shape: escaping only the slash leaves `#` and `?` raw.
    assert project.replace("/", "%2F") == "group%2Fsub#project?x"


def test_issue_create_uses_percent_encoding_not_bare_slash_replace():
    """A regression pin on the actual call site, not just the encoding it
    should use: `encoded_project` in `presets/gitlab/issue_create.py` must
    come from `urllib.parse.quote(project, safe="")`, never from a bare
    `.replace("/", "%2F")` that leaves every other reserved character
    untouched."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "presets" / "gitlab"
           / "issue_create.py").read_text(encoding="utf-8")
    assert "urllib.parse.quote(project, safe=" in src
    assert 'project.replace("/", "%2F")' not in src
