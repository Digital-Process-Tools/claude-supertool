"""#1040 / #1487(b) -- `repo:OWNER/NAME`'s shape check is a pure SHAPE check
(one '/', both halves non-empty) and has no opinion on the character class, so
`repo:-x/y` satisfies it. The value then reaches a shipped op as a POSITIONAL
argv element to `gh` (`presets/github/branch.py`'s `gh repo view <target>`,
among others) -- exactly the slot where a `-`-prefixed token is read as a flag
rather than a value. `gh` currently rejects the unknown flag, so this is loud
and harmless today; the defect is that the validator's own claim ("this shape
is safe to use as an argv value") is not something it checked.

`#1487` finds the SAME character-class gap independently, from
`_REPO_SEGMENT_RE = [A-Za-z0-9._-]+` directly (rather than from the `gh`
callsite), while auditing `_repo_target_platform`'s separate "project op gets
the loose >=2-segments rule" finding. Both issues name the identical fix:
refuse a leading '-' in a repo path segment. They are closed together here,
by tightening `_REPO_SEGMENT_RE` once -- `_repo_shape_error`'s regex check is
unconditional (it runs before the platform-specific arity check), so this
closes the gap for every platform value (`None`, `"github"`, `"gitlab"`,
`"unknown"`) in one place, rather than needing a fix per caller.

`_repo_target_platform`'s OTHER #1487 finding -- a project-defined op with a
repo dimension resolves through `next(iter(found)) or None`, so it is
indistinguishable from "no repo-targetable op in this call at all" -- is
addressed separately below by making that state its own value (`"unknown"`)
rather than silently collapsing into `None`. This does NOT change what shape
is accepted for a project op (still the generic ">=2 segments" rule -- there
is no way to know which forge's stricter rule would apply), only makes the
"this call could not be classified" state a named, checkable one instead of
an accidental falsy fallthrough -- the same "three states, not two" shape
this codebase's own doctrine asks for everywhere else.
"""

from __future__ import annotations

import _supertool as core


# --- #1040: a leading '-' in either half must be refused -------------------

def test_leading_dash_in_owner_is_refused() -> None:
    err = core._repo_shape_error("-x/y", None)
    assert err is not None
    assert "-" in err


def test_leading_dash_in_name_is_refused() -> None:
    err = core._repo_shape_error("x/-y", "github")
    assert err is not None


def test_leading_dash_in_a_gitlab_subgroup_segment_is_refused() -> None:
    err = core._repo_shape_error("group/-sub/project", "gitlab")
    assert err is not None


# --- must-not-fire: an ordinary repo path, hyphen in the MIDDLE, is fine ----

def test_ordinary_hyphenated_repo_is_still_accepted() -> None:
    assert core._repo_shape_error(
        "Digital-Process-Tools/claude-remember", "github") is None


def test_ordinary_gitlab_subgroup_path_is_still_accepted() -> None:
    assert core._repo_shape_error("group/sub-group/project", "gitlab") is None


def test_a_bare_leading_dash_segment_is_refused() -> None:
    """A segment that is ONLY a dash (`repo:-/y`) is refused too -- it still
    satisfies the old regex (`-` is in the allowed class) and is exactly the
    single-character flag shape (`-x`) generalised to its shortest form."""
    assert core._repo_shape_error("-/y", None) is not None


# --- #1487(b): the platform selector names its own "cannot classify" state -

def test_no_repo_targetable_op_at_all_is_still_none() -> None:
    """Genuinely no repo-targetable op in the call -- `_repo_target_platform`
    must still say so with `None`, not with the new sentinel: nothing here
    is a project op with an unresolved platform, there is simply nothing to
    resolve."""
    assert core._repo_target_platform(["read:foo.py"]) is None


def test_a_project_op_with_no_known_platform_is_named_unknown(monkeypatch) -> None:
    """The exact shape #1487 names: an op IS repo-targetable (present in
    `_repo_target_modes()`) but `_shipped_preset_ops()` has no preset for it
    (a project-defined op, not one of the shipped GitHub/GitLab families) --
    `next(iter(found)) or None` used to collapse this into the SAME `None`
    that means "no repo-targetable op in this call at all", so a project op
    silently got the loose >=2-segments rule with no record that the platform
    could not be determined. It is now its own named value."""
    monkeypatch.setattr(core, "_repo_target_modes", lambda: {"my-project-op": "op"})
    monkeypatch.setattr(core, "_shipped_preset_ops", lambda: {"my-project-op": ""})
    assert core._repo_target_platform(["my-project-op:1"]) == "unknown"


def test_a_shipped_op_still_names_its_real_platform(monkeypatch) -> None:
    monkeypatch.setattr(core, "_repo_target_modes", lambda: {"gh-issue": "op"})
    monkeypatch.setattr(core, "_shipped_preset_ops", lambda: {"gh-issue": "github"})
    assert core._repo_target_platform(["gh-issue:1"]) == "github"


def test_mixed_still_wins_over_an_unknown_project_op_in_the_same_call(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        core, "_repo_target_modes",
        lambda: {"gh-issue": "op", "my-project-op": "op"})
    monkeypatch.setattr(
        core, "_shipped_preset_ops",
        lambda: {"gh-issue": "github", "my-project-op": ""})
    assert core._repo_target_platform(
        ["gh-issue:1", "my-project-op:1"]) == "mixed"
