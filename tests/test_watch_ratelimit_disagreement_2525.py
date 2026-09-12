"""`_active_gh_rate_limit_errors` -- feeding the fleet's currently-active
rate-limit-shaped failures (and the poller states that could not be read at
all) from this channel's own watcher rows into
`ratelimit.render_budget_lines`'s DISAGREEMENT line and its unreadable-state
caveat (#2525).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

_d_spec = importlib.util.spec_from_file_location("watch_dispatcher_2525", WATCH_DIR / "dispatcher.py")
assert _d_spec is not None and _d_spec.loader is not None
dispatcher = importlib.util.module_from_spec(_d_spec)
_d_spec.loader.exec_module(dispatcher)


def test_reads_state_only_for_gh_sources_rows_and_classifies_it() -> None:
    rows = [
        {"source": "gh-branch", "id": "42"},
        {"source": "gl-mr", "id": "7"},
    ]
    states = {
        ("gh-branch", "42"): ({"error": "ERROR: GitHub API rate limit exceeded. Wait a few minutes."}, ""),
        ("gl-mr", "7"): ({"error": "ERROR: GitHub API rate limit exceeded. Wait a few minutes."}, ""),
    }

    def fake_read_state_checked(source, watcher_id):
        return states.get((source, watcher_id), ({}, ""))

    with mock.patch.object(dispatcher.transport, "read_state_checked",
                            side_effect=fake_read_state_checked) as m:
        errors, unreadable = dispatcher._active_gh_rate_limit_errors(rows)
    assert errors == [("gh-branch", "42",
                       "ERROR: GitHub API rate limit exceeded. Wait a few minutes.")]
    assert unreadable == []
    # Must-not-fire half of the same call: the non-GH source's state was
    # never even read, let alone classified.
    m.assert_called_once_with("gh-branch", "42")


def test_a_clean_gh_source_row_contributes_nothing() -> None:
    """Must-not-fire twin: no error at all on the only GH_SOURCES row."""
    rows = [{"source": "gh-run", "id": "1"}]
    with mock.patch.object(dispatcher.transport, "read_state_checked",
                            return_value=({}, "")):
        errors, unreadable = dispatcher._active_gh_rate_limit_errors(rows)
    assert errors == []
    assert unreadable == []


def test_an_unreadable_state_file_is_reported_separately_not_as_no_error() -> None:
    """Self-review finding (auditor): a state file this call could not read
    (a symlink refusal, a corrupt/mid-write JSON) must not collapse into
    "no active error" -- `read_state_checked`'s refusal string is carried
    forward as its own `unreadable` entry."""
    rows = [{"source": "gh-branch", "id": "9"}]
    with mock.patch.object(dispatcher.transport, "read_state_checked",
                            return_value=(None, "state file is a symlink and was not followed")):
        errors, unreadable = dispatcher._active_gh_rate_limit_errors(rows)
    assert errors == []
    assert unreadable == [("gh-branch", "9", "state file is a symlink and was not followed")]
