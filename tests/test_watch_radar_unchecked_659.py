"""The radar MR board must not render MRs it never checked as green (#659).

`presets/watch/tiers/gl_mrs.py` calls the same capped `mrs._enrich` as `gl-mrs`
and then sorts by `mrs._sort_key` -> `_is_failing` -> `_pipeline`. Past the cap
`_pipeline` was never set, so `_is_failing` answers False — "not failing" —
rather than "unknown", and nothing on the board said so. #652 fixed that on the
`gl-mrs` surface; a radar is the surface someone glances at to decide nothing
needs attention, so a false green here is the more expensive one.

Every case is driven through `radar_report()` — the layer radar renders — with
the two real API boundaries faked (`glab mr list -F json` on stdout, and the
per-MR detail/approvals endpoint), so `live_open_mrs` and the cap inside
`_enrich` genuinely run. A test that pinned `_is_failing` or handed the tier a
pre-enriched list would pass on the broken code, which is the trap #425
documents. The fakes assert the *contract* (what the enricher was allowed to
ask for), not the current parsing, and every case asserts the fixture really
hit the cap before asserting anything about the output — a fixture that quietly
enriched everything would make this whole file green while testing nothing.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


radar = _module("watch_radar_659", WATCH_DIR / "radar.py")
mr_tier = _module("watch_radar_gl_mrs_659", WATCH_DIR / "tiers" / "gl_mrs.py")
mrs = mr_tier.mrs

CAP = mrs.ENRICH_CAP


def _row(iid: int) -> dict:
    """One row as `glab mr list -F json` returns it — never a pipeline field.

    The list endpoint carries no pipeline status at all; that is the whole
    reason enrichment exists, and the reason an unenriched row is unknown
    rather than green.
    """
    return {
        "iid": iid,
        "title": f"mr {iid}",
        "source_branch": f"b{iid}",
        "target_branch": "master",
        "updated_at": "2026-07-27T10:00:00Z",
        "blocking_discussions_resolved": True,
    }


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Drive the tier over faked GitLab, with all state redirected to tmp_path.

    Returns a callable: run(rows, failing=(), broken=()) -> (lines, healthy).
    `failing` are iids whose pipeline is red; `broken` are iids whose detail
    lookup fails the way a timeout or a 5xx does — `_fetch_mr_detail` returns
    {}. Both are things the board can only learn by asking, so whether it asked
    is exactly what is under test.
    """
    monkeypatch.setattr(mr_tier.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mr_tier.dispatcher, "_spawn_poller",
                        lambda source, watcher_id, only: os.getpid())
    monkeypatch.delenv(mr_tier.EXCLUSIONS_ENV, raising=False)

    state: dict = {"fetched": []}

    def run(rows, failing=(), broken=()):
        failing, broken = set(failing), set(broken)
        state["fetched"] = []

        monkeypatch.setattr(
            mrs, "_run",
            lambda cmd, timeout=25: subprocess.CompletedProcess(
                cmd, 0, json.dumps(rows), ""),
        )

        def _api(endpoint, timeout=10):
            if endpoint.endswith("/approvals"):
                return {"approved": True, "approved_by": []}
            if "/pipelines/" in endpoint:
                return [{"name": "phpstan2"}]
            iid = endpoint.rsplit("/", 1)[-1]
            state["fetched"].append(iid)
            if int(iid) in broken:
                return {}
            status = "failed" if int(iid) in failing else "success"
            return {"head_pipeline": {"status": status, "id": iid},
                    "changes_count": "3"}

        monkeypatch.setattr(mrs, "_api_json", _api)
        return mr_tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})

    run.fetched = state  # type: ignore[attr-defined]
    return run


def _fetched(board) -> list[str]:
    return board.fetched["fetched"]


def _text(lines) -> str:
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The load-bearing one: more MRs than the cap, and the red one is past it.
# ---------------------------------------------------------------------------

def test_a_failing_mr_past_the_enrichment_cap_is_not_rendered_as_a_green_board(
    board,
) -> None:
    """The contract in one case. The tier asked about `cap` MRs and was handed
    `cap + 5`; the red one is among the five it never asked about. Either it is
    on the board as red, or the board says it could not see it. Rendering the
    remaining rows with no marker is the defect."""
    rows = [_row(i) for i in range(1, CAP + 6)]
    beyond = CAP + 3
    lines, _healthy = board(rows, failing={beyond})

    assert len(_fetched(board)) == CAP, "fixture must actually hit the enrichment cap"
    assert str(beyond) not in _fetched(board), \
        "fixture is wrong: the MR past the cap was checked after all"

    out = _text(lines)
    assert "5 of 45 MRs" in out and "not checked" in out, (
        "the radar rendered a board of 45 MRs having checked 40, and said "
        "nothing about the 5 whose pipeline it never read"
    )


def test_the_board_is_not_healthy_while_any_mr_went_unchecked(board) -> None:
    """`healthy` is the tier's own word for "coverage is known and complete"
    (radar_report's docstring). A board with unread pipelines is not complete,
    and it is the `quiet_when_healthy` flag's only input — so claiming health
    here is what lets a configured radar suppress the board entirely."""
    rows = [_row(i) for i in range(1, CAP + 6)]
    _lines, healthy = board(rows)

    assert len(_fetched(board)) == CAP, "fixture must actually hit the enrichment cap"
    assert healthy is False


def test_a_quiet_configured_radar_still_prints_an_incomplete_board(
    board, tmp_path, monkeypatch, capsys,
) -> None:
    """End to end through `radar.main`, the only place `healthy` is consumed.

    `quiet_when_healthy: true` suppresses a healthy tier's whole board. An
    incomplete board suppressed on the strength of a health claim it should
    never have made is the silence this repo rates worst — the tool's own
    omission rendering as an absence in the world."""
    monkeypatch.setattr(radar.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(radar.dispatcher, "_spawn_poller",
                        lambda source, watcher_id, only: os.getpid())
    monkeypatch.setenv(radar.TIERS_ENV,
                       json.dumps({"gl-mrs": {"quiet_when_healthy": True}}))
    monkeypatch.setattr(radar, "_tier_module",
                        lambda n: mr_tier if n == "gl-mrs" else None)

    rows = [_row(i) for i in range(1, CAP + 6)]
    board(rows, failing={CAP + 3})
    assert len(_fetched(board)) == CAP, "fixture must actually hit the enrichment cap"

    assert radar.main([]) == 0
    out = capsys.readouterr().out
    assert "not checked" in out, (
        "a quiet-configured radar printed nothing at all for a board whose "
        "pipeline coverage was incomplete"
    )


# ---------------------------------------------------------------------------
# Unchecked is a property of the read, not of the cap (#657's `bool(detail)`).
# ---------------------------------------------------------------------------

def test_a_detail_lookup_that_failed_inside_the_cap_is_also_unchecked(board) -> None:
    """Three MRs, cap forty. Nothing was truncated — one lookup simply failed,
    which lands as `_pipeline = ""` and reads as green exactly like a past-cap
    MR. And the disclosure must not blame a cap that never applied: pointing at
    a limit the reader could raise is advice that cannot work."""
    rows = [_row(i) for i in (1, 2, 3)]
    lines, healthy = board(rows, broken={2})

    assert sorted(_fetched(board)) == ["1", "2", "3"], \
        "fixture must have asked about every MR — the cap is not what cut here"

    out = _text(lines)
    assert "1 of 3 MRs" in out and "not checked" in out
    assert healthy is False
    assert mrs.ENRICH_CAP_KNOB not in out, (
        "the notice named the enrichment cap on a board of 3 MRs the cap never "
        "touched — a confidently wrong cause"
    )


def test_the_cap_is_named_as_the_escape_only_when_the_cap_is_what_cut(board) -> None:
    rows = [_row(i) for i in range(1, CAP + 6)]
    lines, _healthy = board(rows)
    assert len(_fetched(board)) == CAP
    out = _text(lines)
    assert mrs.ENRICH_CAP_KNOB in out and str(CAP) in out


# ---------------------------------------------------------------------------
# Silence is a positive claim. This is the half that rots.
# ---------------------------------------------------------------------------

def test_a_fully_checked_board_prints_no_marker_and_stays_healthy(board) -> None:
    """The absence of a marker is how the board claims it saw everything, so a
    complete board must print nothing extra. Without this, the honest fix for
    #659 degrades into a permanent warning, every board becomes a warning, and
    the marker stops carrying information — a loud failure traded for a quiet
    one."""
    rows = [_row(i) for i in (1, 2, 3)]
    lines, healthy = board(rows, failing={2})

    assert sorted(_fetched(board)) == ["1", "2", "3"]
    out = _text(lines)
    assert "not checked" not in out and "unchecked" not in out
    assert healthy is True
    assert "!2" in out, "fixture must render the red MR it did check"


# ---------------------------------------------------------------------------
# The footer is the tally line, and it silently dropped the unchecked rows:
# `_pipeline or "none"` is counted and then never printed.
# ---------------------------------------------------------------------------

def test_the_footer_accounts_for_the_mrs_it_could_not_classify(board) -> None:
    rows = [_row(i) for i in range(1, CAP + 6)]
    lines, _healthy = board(rows)
    assert len(_fetched(board)) == CAP

    footer = next(line for line in lines if "open" in line and "|" in line)
    assert "45 open" in footer
    assert "40 green" in footer
    assert "5 unchecked" in footer, (
        f"footer accounts for 40 of 45 open MRs and says nothing about the "
        f"other 5: {footer}"
    )
