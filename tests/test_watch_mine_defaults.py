"""Tests for presets/watch/watch-mine.sh defaults (issue #417 items 1 + 2).

The feed and only= defaults are exactly the kind of thing that regresses
silently: a wrong feed still spawns watchers, a wrong filter still notifies —
just not about the thing you needed. So these pin the *exact* `watch:`
invocations the script emits for a stubbed feed, byte for byte.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
WATCH_DIR = REPO / "presets" / "watch"
SCRIPT = WATCH_DIR / "watch-mine.sh"

_spec = importlib.util.spec_from_file_location("watch_defaults", WATCH_DIR / "defaults.py")
assert _spec is not None and _spec.loader is not None
defaults = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(defaults)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="watch-mine.sh is a bash script"
)

EXPECTED_FEED = "gl-mrs:author=@me,state=opened,iids"
# comment_added joined the set in #519, after the reason it was excluded —
# "user_notes_count counts system notes" — was checked against the live API and
# turned out to be false. See tests/test_watch_gitlab_mr_poller.py for the
# twelve-MR derivation.
# mr_unreachable joined in #541. It is the one event about the watcher rather
# than the MR, and a default filter that drops it keeps the defect in the
# default configuration: every "watch everything of mine" flow spawns with this
# string, so a board of live-looking rows observing nothing is what you get out
# of the box. Edge-triggered, so it costs one line per outage.
#
# It is appended, never inserted: `only=` strings live in user config and the
# #439/#464 invariant is that no existing name moves.
EXPECTED_ONLY = ("pipeline_failed,pipeline_succeeded,comment_added,"
                 "merged,closed,conflicts_appeared,mr_unreachable")


# ---------------------------------------------------------------------------
# defaults.py — the single source of truth both callers read
# ---------------------------------------------------------------------------

def test_default_feed_is_every_open_mr() -> None:
    assert defaults.DEFAULT_FEED == EXPECTED_FEED


def test_the_feed_op_is_built_from_the_shared_filter() -> None:
    """radar reads DEFAULT_FILTER and the shell reads DEFAULT_FEED. Equal
    values are not enough — two literals that happen to agree today are the
    drift this module exists to prevent, so pin that one is *derived* from
    the other rather than merely matching it."""
    assert defaults.DEFAULT_FILTER == "author=@me,state=opened"
    assert defaults.DEFAULT_FEED == f"gl-mrs:{defaults.DEFAULT_FILTER},iids"

    source = (WATCH_DIR / "defaults.py").read_text(encoding="utf-8")
    assert 'DEFAULT_FEED = f"gl-mrs:{DEFAULT_FILTER},iids"' in source


def test_defaults_cli_prints_filter() -> None:
    r = _run_defaults("filter")
    assert r.returncode == 0
    assert r.stdout == defaults.DEFAULT_FILTER + "\n"


def test_default_only_includes_success_and_terminal_events() -> None:
    assert defaults.DEFAULT_ONLY == EXPECTED_ONLY


def test_default_only_excludes_noise_events() -> None:
    events = set(defaults.DEFAULT_ONLY.split(","))
    assert "pipeline_running" not in events


def test_default_only_carries_comment_added() -> None:
    """This assertion used to run the other way. `comment_added` was excluded
    on the grounds that `user_notes_count` counts system notes, so enabling it
    would double-fire on every pipeline transition. It does not — GitLab scopes
    that counter over `where(system: false)` — so the exclusion was resting on
    an unchecked source comment rather than on observed behaviour (#519)."""
    assert "comment_added" in set(defaults.DEFAULT_ONLY.split(","))


def _run_defaults(key: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WATCH_DIR / "defaults.py"), key],
        capture_output=True, text=True, timeout=20,
    )


def test_defaults_cli_prints_feed() -> None:
    r = _run_defaults("feed")
    assert r.returncode == 0
    assert r.stdout == EXPECTED_FEED + "\n"


def test_defaults_cli_prints_only() -> None:
    r = _run_defaults("only")
    assert r.returncode == 0
    assert r.stdout == EXPECTED_ONLY + "\n"


def test_defaults_cli_rejects_unknown_key() -> None:
    r = _run_defaults("nope")
    assert r.returncode == 1
    assert r.stdout == ""


# ---------------------------------------------------------------------------
# watch-mine.sh — exact emitted invocations against a stubbed supertool
# ---------------------------------------------------------------------------

STUB = """#!/usr/bin/env bash
printf '%s\\n' "$1" >> "$STUB_LOG"
case "$1" in
  gl-mrs*|gh-prs*)
    printf 'supertool header line\\n33161\\n33167\\nPASS\\n'
    ;;
  watches)
    printf 'no active watchers\\n'
    ;;
esac
exit 0
"""


def _stub_supertool(tmp_path: Path) -> tuple[Path, Path]:
    stub = tmp_path / "supertool-stub.sh"
    stub.write_text(STUB)
    stub.chmod(0o755)
    return stub, tmp_path / "invocations.log"


def _run_watch_mine(tmp_path: Path, *args: str) -> list[str]:
    stub, log = _stub_supertool(tmp_path)
    env = dict(os.environ, SUPERTOOL=str(stub), STUB_LOG=str(log))
    r = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert r.returncode == 0, r.stderr
    return log.read_text(encoding="utf-8").splitlines()


def test_watch_mine_emits_exact_default_invocations(tmp_path) -> None:
    assert _run_watch_mine(tmp_path) == [
        EXPECTED_FEED,
        f"watch:gitlab-mr:33161:only={EXPECTED_ONLY}",
        f"watch:gitlab-mr:33167:only={EXPECTED_ONLY}",
        "watches",
    ]


def test_watch_mine_does_not_use_the_old_failing_only_feed(tmp_path) -> None:
    """The pre-#417 default watched only already-failing MRs — the blind spot."""
    calls = _run_watch_mine(tmp_path)
    assert "gl-mrs:author=@me,failed,iids" not in calls
    assert calls[0] == EXPECTED_FEED


def test_watch_mine_arguments_override_every_default(tmp_path) -> None:
    calls = _run_watch_mine(
        tmp_path, "gh-prs:author=@me,failed,iids", "github-pr", "merged"
    )
    assert calls == [
        "gh-prs:author=@me,failed,iids",
        "watch:github-pr:33161:only=merged",
        "watch:github-pr:33167:only=merged",
        "watches",
    ]


def test_watch_mine_defaults_match_radar_heal_filter() -> None:
    """watch-mine.sh and radar must spawn identical watchers.

    Both read defaults.py, so this asserts the shell script really consumes it
    rather than carrying its own copy of the literal.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    assert "defaults.py" in text
    assert EXPECTED_ONLY not in text
    assert EXPECTED_FEED not in text
