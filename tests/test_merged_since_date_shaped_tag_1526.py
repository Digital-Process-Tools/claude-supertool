"""A repo that tags releases by date could not name its own tag, and two
interpreters disagreed about which values are date-shaped (#1526).

## The version disagreement, measured

`datetime.fromisoformat`'s grammar WIDENED in 3.11. Measured 2026-08-13 on this
machine, CPython 3.9.6 (the CI floor) and 3.11.11:

    value         3.9.6                3.11.11
    20260809      ValueError           2026-08-09 00:00:00
    2026-W32-1    ValueError           2026-08-03 00:00:00

`gh-prs:merged-since=` routes on `parse_iso_instant(value) is None`, so the same
call was a *date boundary at midnight UTC* on 3.11+ and a *tag name to hunt in
the local clone* on 3.9 — and `2026-W32-1` was a date on 3.11+ and a refused
value on 3.9. Nothing disclosed either. The grammar is pinned in this module now
rather than inherited: the accepted spelling is the extended ISO-8601 form and
nothing else, on every interpreter.

## The tag nobody could name

`looks_like_ref` rejects a date-shaped value on purpose — `2026-13-45` is a typo
in a date, and "no tag by that name" is the wrong sentence for it. But that left
a repository tagging releases `2026-08-09` with no spelling at all that means
"this is a ref", while the refusal named one. `refs/tags/NAME` is that spelling:
git's own, colon-free (supertool splits an op argument on ':'), never parseable
as a date, and it was already accepted by the filter — only `select_tag` failed
to match it against `for-each-ref`'s short names.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PRESETS = Path(__file__).parent.parent / "presets"
sys.path.insert(0, str(PRESETS))
sys.path.insert(0, str(PRESETS / "github"))


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ft = _load("_filter_tokens.py", "filter_tokens_1526")
rg = _load("github/_release_gate.py", "release_gate_1526")
prs = _load("github/prs.py", "gh_prs_1526")


# ---------------------------------------------------------------------------
# One grammar, whatever interpreter is running
# ---------------------------------------------------------------------------

#: Accepted by `fromisoformat` from 3.11, rejected on the 3.9 floor. Each of
#: these routed differently depending on which interpreter answered the call.
VERSION_DEPENDENT = ["20260809", "2026-W32-1", "20260809T101112Z",
                     "2026-W32", "2026W321"]


@pytest.mark.parametrize("value", VERSION_DEPENDENT)
def test_a_spelling_the_floor_rejects_is_not_a_date_on_any_interpreter(value) -> None:
    assert ft.parse_iso_instant(value) is None, (
        f"{value!r} parses here and raises on 3.9 — the same call would answer "
        f"two different questions depending on the interpreter")


@pytest.mark.parametrize("value,iso", [
    ("2026-08-09", "2026-08-09T00:00:00+00:00"),
    ("2026-08-09T16:07:45Z", "2026-08-09T16:07:45+00:00"),
    ("2026-08-09T16:07:45z", "2026-08-09T16:07:45+00:00"),
    ("2026-08-09T18:07:45+02:00", "2026-08-09T16:07:45+00:00"),
    ("2026-08-09T18:07:45+0200", "2026-08-09T16:07:45+00:00"),
    ("2026-08-09 16:07:45", "2026-08-09T16:07:45+00:00"),
    ("2026-08-09T16:07", "2026-08-09T16:07:00+00:00"),
    ("2026-08-09T16:07:45.123456Z", "2026-08-09T16:07:45.123456+00:00"),
])
def test_every_spelling_git_and_gh_emit_still_parses(value, iso) -> None:
    """The pin is on the grammar this repo actually reads, not on a rewrite."""
    parsed = ft.parse_iso_instant(value)
    assert parsed is not None, value
    assert parsed.isoformat() == iso


@pytest.mark.parametrize("value", ["", "   ", "not a date", "2026-13-45",
                                   "2026-08", "2026", "v0.38.0", None])
def test_a_value_that_is_no_instant_at_all_is_still_none(value) -> None:
    assert ft.parse_iso_instant(value) is None


# ---------------------------------------------------------------------------
# The date-shaped tag now has a spelling
# ---------------------------------------------------------------------------

DATED_TAG = {"name": "2026-08-09", "objtype": "commit", "sha": "c" * 40,
             "full_sha": "c" * 40, "commit_date": "2026-08-09T10:00:00+00:00",
             "reachable": True}


def test_a_bare_date_is_still_a_date_and_not_a_ref() -> None:
    """Unchanged on purpose: `2026-13-45` is a typo, not a tag to go hunting."""
    assert ft.looks_like_ref("2026-08-09") is False
    assert ft.parse_iso_instant("2026-08-09") is not None


def test_the_ref_spelling_is_accepted_by_the_filter() -> None:
    assert ft.looks_like_ref("refs/tags/2026-08-09") is True
    assert ft.bad_values({"merged-since": "refs/tags/2026-08-09"},
                         {"merged-since": ft.ISO_INSTANT_OR_TAG}) == []


def test_the_ref_spelling_routes_to_the_tag_branch() -> None:
    plan = prs._gate_plan({"merged-since": "refs/tags/2026-08-09"})
    assert plan is not None and plan.is_tag is True


def test_a_date_shaped_tag_resolves_under_its_full_ref_name() -> None:
    chosen, state, notes = rg.select_tag([DATED_TAG], "refs/tags/2026-08-09")
    assert chosen is DATED_TAG, notes
    assert state == rg.BOUNDARY_RESOLVED


def test_the_short_name_still_resolves_under_the_full_ref_name() -> None:
    tag = dict(DATED_TAG, name="v0.38.0")
    chosen, state, _notes = rg.select_tag([tag], "refs/tags/v0.38.0")
    assert chosen is tag
    assert state == rg.BOUNDARY_RESOLVED


def test_a_ref_name_that_does_not_exist_is_still_refused_by_the_name_typed() -> None:
    chosen, state, notes = rg.select_tag([DATED_TAG], "refs/tags/2026-08-10")
    assert chosen is None
    assert state == rg.BOUNDARY_UNRESOLVED
    assert "refs/tags/2026-08-10" in " ".join(notes), notes


def test_the_refusal_names_the_spelling_that_exists() -> None:
    """The class is `misdirects`: the old text named a remedy that did not."""
    assert "refs/tags/" in ft.ISO_INSTANT_OR_TAG
