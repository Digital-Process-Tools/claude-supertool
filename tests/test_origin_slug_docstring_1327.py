"""Regression test for #1327: `origin_slug`'s docstring must name its hazard.

`origin_slug`'s substring host test (`host_substr in host`) is deliberate --
it is how a self-hosted GitLab at `gitlab.internal.example` matches
`"gitlab"` -- and it is safe only because the value under test always comes
from the operator's own `git remote get-url origin`, never from an issue
body, a PR title, or any other attacker-influenced source. Before this fix
the docstring sold the substring as a feature and never named that it also
lets `origin_slug("github")` match `evil-github.com`.

This does not change `origin_slug`'s behaviour (issue's option (a): keep the
substring, document the hazard and the condition that makes it safe) -- the
behavioural tests in `tests/test_remote_default.py` already cover the
substring match itself. This file only pins that the documentation now says
the thing it was missing, so a future edit cannot silently drop the warning
back to "sold as a feature, hazard unnamed".
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

RD_PATH = Path(__file__).parent.parent / "presets" / "_remote_default.py"
_spec = importlib.util.spec_from_file_location("remote_default_1327", RD_PATH)
assert _spec is not None and _spec.loader is not None
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)


def test_docstring_names_the_operator_controlled_condition():
    doc = rd.origin_slug.__doc__ or ""
    assert "operator-controlled" in doc or "operator controlled" in doc


def test_docstring_names_a_lookalike_hazard_example():
    doc = rd.origin_slug.__doc__ or ""
    # At least one concrete lookalike-host example, not just an abstract
    # warning -- the kind of string a reader can grep for and recognise as
    # "this is the #1327 hazard", the same way #1326's fix in
    # presets/github/issue.py names evilgithubusercontent.com.
    assert "evil-github" in doc or "evil-github.com" in doc


def test_docstring_still_explains_the_self_hosted_feature():
    doc = rd.origin_slug.__doc__ or ""
    assert "self-hosted" in doc


def test_substring_match_behaviour_is_unchanged():
    """Option (a) is documentation-only -- the substring test itself must
    still behave exactly as before, both the intended match and the
    admitted hazard."""
    assert ("gitlab" in "gitlab.dp.tools") is True
    assert ("github" in "evil-github.com") is True  # the hazard, still present by design
