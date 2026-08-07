"""`dashboard`'s lane vocabulary is configured, and an empty universe is a state (#1007).

`LANE_PREFIX = "lane:"` was hardcoded from the *title of #964* — an issue whose
whole subject is a colon that is not in any label name. This repository spells
its lanes `lane-watch`, `lane-release`; `claude-remember`, one repo away, spells
its priorities `priority:high`. There is no prefix that is right everywhere, so
there is no prefix to hardcode.

Three things are pinned here:

* **the vocabulary is configuration, and unconfigured refuses.** Radar's property
  (#528): an unconfigured radar that prints nothing is byte-identical to a healthy
  one. A lane prefix nobody configured must not quietly match zero labels.
* **an empty lane universe is its own state.** "no label matched my prefix" and
  "this repository has no lanes" rendered identically as `0 free, 0 occupied,
  0 unknown`. The first is a defect and the second is fine, and a tally cannot
  tell them apart — which is why #1007 needed someone to grep the source.
* **and none of that weakens the refusal that already worked.** Under a totally
  failed lane universe the op still declined `free`, said `unplaced`, and printed
  `'unknown' is NOT 'free'`. That is the behaviour that stopped this bug being a
  two-agents-one-file incident.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _preset_loader import load_preset_module  # noqa: E402

dashboard = load_preset_module("dashboard", "dashboard", prefix="dashboard_")

ROOT = Path(__file__).resolve().parents[1]

#: `gh label list --limit 200 --json name -q '.[].name' | grep -E '^lane'`, run
#: against Digital-Process-Tools/claude-supertool on 2026-08-07. Every one is
#: dash-separated; not one contains a colon.
REAL_LABELS = [
    "bug", "enhancement", "priority-high", "priority-medium", "priority-low",
    "lane-watch", "lane-tracker-ops", "lane-validators", "lane-ci-cost",
    "lane-containment", "lane-release", "lane-git-ops",
]
REAL_LANES = sorted(name for name in REAL_LABELS if name.startswith("lane-"))


def _labels(names):
    return [{"name": name} for name in names]


# ── the vocabulary is configured, not asserted ───────────────────────────

def test_this_repositorys_shipped_config_matches_its_actual_labels():
    """The whole bug, as one assertion against the real label set.

    Not "the constant is spelled `lane-`" — that is the one-character fix wearing
    a test. This reads the prefix this repository *ships* in `.supertool.json`
    and requires it to select every lane label the tracker actually declares.
    """
    cfg = json.loads((ROOT / ".supertool.json").read_text(encoding="utf-8"))
    prefix = cfg.get("ops", {}).get("dashboard", {}).get("lane_prefix")
    assert prefix, ("ops.dashboard.lane_prefix is unset in this repository's own "
                    ".supertool.json — dashboard would refuse the lane board here")
    selected = sorted(n for n in REAL_LABELS if n.startswith(prefix))
    assert selected == REAL_LANES, (
        f"prefix {prefix!r} selects {selected} out of the live label set")


def test_the_config_key_reaches_the_preset_under_the_name_it_reads():
    """The wiring, pinned — because every other test here passes without it.

    `ops.dashboard.<key>` reaches a preset as `SUPERTOOL_<KEY>`: the op runner
    uppercases the key alone and does not namespace it by op. A constant naming
    a variable the runner never sets makes a fully configured repository refuse,
    and the only thing that catches it is running the op. It was caught that
    way, once.
    """
    cfg = json.loads((ROOT / ".supertool.json").read_text(encoding="utf-8"))
    keys = [k for k in cfg["ops"]["dashboard"] if not k.startswith("_")]
    assert dashboard.LANE_PREFIX_ENV in {f"SUPERTOOL_{k.upper()}" for k in keys}


def test_an_unconfigured_prefix_refuses_and_names_the_key():
    prefix, complaint = dashboard.read_lane_prefix("")
    assert prefix is None
    assert "ops.dashboard.lane_prefix" in complaint


def test_a_configured_prefix_is_taken_verbatim_with_no_complaint():
    assert dashboard.read_lane_prefix("lane-") == ("lane-", "")
    assert dashboard.read_lane_prefix("  lane:  ") == ("lane:", "")


def test_there_is_no_default_prefix_anywhere_in_the_source():
    """A default is the thing that makes the *next* repository fail silently.

    Shipping `lane-` as a fallback would make this repo green and hand the next
    one the identical defect, minus the evidence that filed it.
    """
    src = (ROOT / "presets" / "dashboard" / "dashboard.py").read_text(encoding="utf-8")
    assert not re.search(r"^LANE_PREFIX\s*=", src, re.M)
    assert dashboard.read_lane_prefix("")[0] is None


def test_an_unconfigured_dashboard_declines_the_lane_board_rather_than_emptying_it():
    section = dashboard.render_lane_refusal(dashboard.read_lane_prefix("")[1])
    assert section.unread
    assert "ops.dashboard.lane_prefix" in section.error


# ── an empty universe is its own state ───────────────────────────────────

def test_a_prefix_that_matches_nothing_reports_that_and_not_a_tally():
    """Item 3 of #1007, against the exact input that produced the bug.

    `lane:` over this repository's real labels. Today that renders `0 free,
    0 occupied, 0 unknown` — a tally, indistinguishable from a repository that
    genuinely has no lanes.
    """
    lanes, near, err = dashboard.select_lane_universe(_labels(REAL_LABELS), "lane:")
    assert err == ""
    assert lanes == []
    assert near == REAL_LANES

    note = dashboard.lane_universe_note("lane:", lanes, near)
    assert note, "an empty universe must carry its own explanation"
    assert "lane:" in note
    assert "lane-" in note, "the spelling that would have matched must be named"


def test_the_empty_universe_note_says_so_when_there_is_no_near_miss_either():
    """A repository that genuinely has no lanes is fine, and says a different thing.

    Same shape, opposite meaning: the note must not accuse the operator of a
    typo when there is no evidence of one.
    """
    note = dashboard.lane_universe_note("lane-", [], [])
    assert note
    assert "lane-" in note
    assert "lane:" not in note


def test_the_footer_of_an_empty_universe_is_not_zero_free_zero_occupied():
    report = dashboard.Report(repo="r", sections={}, prs=[], lanes={},
                              lane_prefix="lane:")
    footer = dashboard.render(report).splitlines()[-1]
    assert "0 lanes free, 0 occupied, 0 unknown" not in footer
    assert "lane:" in footer


def test_the_footer_of_an_unconfigured_prefix_is_not_zero_free_zero_occupied():
    report = dashboard.Report(repo="r", sections={}, prs=[], lanes=None,
                              lane_prefix=None)
    footer = dashboard.render(report).splitlines()[-1]
    assert "0 lanes free" not in footer
    assert "UNCONFIGURED" in footer


def test_next_declines_to_send_anyone_anywhere_when_the_universe_is_empty():
    report = dashboard.Report(
        repo="r", sections={"board": dashboard.Section("board", lines=[])},
        prs=[], lanes={}, lane_prefix="lane:")
    line = dashboard.next_action(report)
    assert "lane:" in line
    assert "free" not in line.replace("no lane can be reported free", "")


def test_an_empty_universe_renders_degraded_rather_than_as_an_empty_section():
    section = dashboard.render_lanes({}, strays=(), prefix="lane:",
                                     note=dashboard.lane_universe_note(
                                         "lane:", [], REAL_LANES))
    assert not section.unread, "the labels were read; this is a finding, not a gap"
    assert section.warning and "lane:" in section.warning


# ── the refusal that already worked, kept ────────────────────────────────

def test_a_failed_universe_still_refuses_free_and_still_names_the_strays():
    """Do not trade the loud bug for the quiet one.

    Under the #1007 failure the op declined every lane, said `unplaced`, and
    printed `'unknown' is NOT 'free'`. A lane board that starts guessing `free`
    to look tidier sends two agents into the same files.
    """
    stray = dashboard.Worktree(path="/w/pr-ops", branch="feat/pr-ops",
                               state="occupied", lanes=[])
    states = dashboard.lane_states(REAL_LANES, prs=[], worktrees=[stray])
    assert all(state == dashboard.LANE_UNKNOWN for state, _e in states.values())
    section = dashboard.render_lanes(states, [stray], prefix="lane-")
    assert section.lines[0].startswith("unplaced:")
    assert "'unknown' is NOT 'free'" in section.lines[-1]
    assert dashboard.lane_states(None, prs=[], worktrees=[]) is None


@pytest.mark.parametrize("prefix", ["lane-", "lane:"])
def test_the_lane_column_strips_whatever_prefix_is_in_force(prefix):
    states = {f"{prefix}release": (dashboard.LANE_FREE, ["nothing points here"])}
    section = dashboard.render_lanes(states, (), prefix=prefix)
    assert any(line.split()[1] == "release" for line in section.lines[:-1])


# ── the false measurement ────────────────────────────────────────────────

def test_nothing_claims_to_have_measured_labels_that_never_existed():
    """`Measured on this repository on 2026-08-07: 7 `lane:*` labels exist`.

    Seven lane labels do exist. Not one is spelled that way. A false claim
    carrying the word *measured* is the most persuasive kind, and it sat in both
    the source and the docs.
    """
    for path in (ROOT / "presets" / "dashboard" / "dashboard.py",
                 ROOT / "docs" / "presets" / "dashboard.md"):
        text = path.read_text(encoding="utf-8")
        assert "lane:*" not in text, f"{path.name} still asserts the colon spelling"
