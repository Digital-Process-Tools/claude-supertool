"""#1207 — `gh-prs` is the board, so its default population is the repo.

#1071 reported the empty-looking board; #1072 made the implicit `author=@me`
*honest* and left the default itself open. Measured on 2026-08-09: two
dependabot bumps at 5h and an external contributor's PR at 1 day were invisible
on every board read that day. The rows `author=@me` removes are exactly the ones
that need a decision from someone other than their author.

What this file pins:

* bare `gh-prs` asks `gh` for every author's open PRs
* `gh-prs:author=@me` still asks for one person's, and still discloses what that
  cost when the answer is empty — #1072's machinery survives the flip, pointed
  at the filter that is now explicit
* `_build_list_cmd(filters, per_page)` called with two positional arguments —
  the radar tier's call in `presets/watch/tiers/gh_prs.py` — is **unchanged**.
  Radar's snapshot is keyed on the filter string, so a silent widening would
  reuse a snapshot taken under the narrow scope and render every
  previously-hidden PR as newly arrived, once, on every watcher. That half of
  #1207 is a separate change in a directory this branch does not touch.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "prs.py"
_spec = importlib.util.spec_from_file_location("github_prs_1207", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
prs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prs)


def _pr(number: int, login: str = "outsider") -> dict:
    return {
        "number": number,
        "title": f"pr {number}",
        "state": "OPEN",
        "author": {"login": login},
        "headRefName": f"feat/{number}",
        "headRefOid": "0" * 40,
        "baseRefName": "master",
        "labels": [],
        "assignees": [],
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "statusCheckRollup": [],
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "url": f"https://github.com/o/n/pull/{number}",
    }


def _drive(monkeypatch: pytest.MonkeyPatch, arg_str: str,
           responder: Callable[[list[str]], object]) -> tuple[int, str, str, list]:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        answer = responder(cmd)
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(cmd, 0, str(answer), "")

    monkeypatch.setattr(prs.subprocess, "run", fake_run)
    monkeypatch.setattr(prs, "_watched_numbers", lambda *a, **k: set(), raising=False)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = prs.main_with_args(arg_str)
    return code, out.getvalue(), err.getvalue(), seen


def _by_author(mine: list[dict], everyone: list[dict]) -> Callable[[list[str]], object]:
    def responder(cmd: list[str]) -> object:
        return json.dumps(mine if "--author" in cmd else everyone)
    return responder


# ---------------------------------------------------------------------------
# the flip
# ---------------------------------------------------------------------------

def test_bare_gh_prs_asks_for_every_author(monkeypatch: pytest.MonkeyPatch) -> None:
    code, _out, _err, seen = _drive(monkeypatch, "", _by_author([], [_pr(1)]))
    assert code == 0
    assert seen, "no gh call was made at all"
    assert "--author" not in seen[0], (
        f"bare gh-prs must not narrow to one author; got {seen[0]!r}"
    )


def test_bare_gh_prs_renders_a_pr_it_did_not_write(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured failure: an external PR absent from the default board."""
    code, out, _err, _seen = _drive(monkeypatch, "",
                                    _by_author([], [_pr(323, "contributor")]))
    assert code == 0
    assert "323" in out, f"the external PR must be on the default board; got {out!r}"
    assert "No PRs match." not in out


def test_the_default_board_names_its_own_scope(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """An unlabelled board spells both 'this is everything' and 'nobody said'."""
    _code, out, _err, _seen = _drive(monkeypatch, "", _by_author([], [_pr(1)]))
    low = out.lower()
    assert "every author" in low, (
        f"the default board must say it is not narrowed; got {out!r}"
    )
    assert "gh-prs:author=@me" in low, (
        f"and the way back to a personal queue; got {out!r}"
    )


# ---------------------------------------------------------------------------
# what the flip must not break
# ---------------------------------------------------------------------------

def test_the_radar_tiers_two_positional_call_is_unchanged() -> None:
    """`watch/tiers/gh_prs.py` calls `_build_list_cmd(filters, per_page)`.

    Radar keys its departure snapshot on the filter string, so widening this
    without widening the key replays the whole repo as new arrivals.
    """
    assert "--author" in prs._build_list_cmd({}, 50), (
        "the radar tier's positional call must keep the author=@me default "
        "until radar's own scope label and snapshot key move with it"
    )


def test_explicit_author_still_narrows(monkeypatch: pytest.MonkeyPatch) -> None:
    _code, out, _err, seen = _drive(monkeypatch, "author=@me",
                                    _by_author([_pr(9, "me")], [_pr(9), _pr(8)]))
    assert "--author" in seen[0] and "@me" in seen[0]
    assert "9" in out


def test_explicit_author_with_an_empty_board_says_what_it_excluded(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """#1072's disclosure, pointed at the filter that is now explicit."""
    _code, out, _err, _seen = _drive(monkeypatch, "author=@me",
                                     _by_author([], [_pr(325), _pr(323)]))
    assert "2" in out.split("0 PR(s)")[-1], (
        f"it must say how many rows the filter excluded; got {out!r}"
    )


def test_explicit_reviewer_probe_drops_every_role_key(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe must widen on *all* role keys, not only on `author`.

    Stripping `author` alone leaves `--search review-requested:...` on the probe
    argv, so the probe returns the same narrowed population and the board
    reports `excluded none` — an absence produced by the tool, read as an
    absence in the world.
    """
    def responder(cmd: list[str]) -> object:
        narrowed = "--author" in cmd or "--search" in cmd
        return json.dumps([] if narrowed else [_pr(325), _pr(323)])

    _code, out, _err, _seen = _drive(monkeypatch, "reviewer=@me", responder)
    assert "none" not in out.lower(), (
        f"the probe must not answer from a still-narrowed query; got {out!r}"
    )
    assert "2" in out.split("0 PR(s)")[-1], f"got {out!r}"


def test_anyauthor_is_still_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is the default now, but it was documented and must not start refusing."""
    code, _out, err, seen = _drive(monkeypatch, "anyauthor", _by_author([], [_pr(1)]))
    assert code == 0, f"anyauthor must not refuse; err={err!r}"
    assert "--author" not in seen[0]
