"""Regression tests for issue #353 — watch:gl-pipeline:<ID> failed silently.

The watch dispatcher had no `gl-pipeline` source registered, so
`watch:gl-pipeline:<ID>` resolved to an unknown source and exited 1 without
ever polling. These tests assert:

  1. The dispatcher can now resolve the `gl-pipeline` source (it was missing).
  2. The new poller diffs pipeline status and emits on transitions /
     terminal states, mirroring the gitlab-mr source contract.

glab/network is mocked at the poller's `_fetch` seam — same style as
tests/test_watch_gitlab_mr_poller.py — so these are hermetic and never
touch a real pipeline (and are isolated from the #352/#359 JSON-parse bug).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

_d_spec = importlib.util.spec_from_file_location("watch_dispatcher_353", WATCH_DIR / "dispatcher.py")
assert _d_spec is not None and _d_spec.loader is not None
dispatcher = importlib.util.module_from_spec(_d_spec)
_d_spec.loader.exec_module(dispatcher)

POLLER = WATCH_DIR / "sources" / "gl-pipeline" / "poller.py"


def _load_poller():
    spec = importlib.util.spec_from_file_location("gl_pipeline_poller", POLLER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Red proof: source must be resolvable by the dispatcher --------------------

def test_dispatcher_resolves_gl_pipeline_source() -> None:
    """RED before fix: no sources/gl-pipeline/poller.py -> _load_source is None,
    so watch:gl-pipeline:<ID> printed 'unknown source' and exited 1."""
    mod = dispatcher._load_source("gl-pipeline")
    assert mod is not None
    assert hasattr(mod, "poll")
    assert hasattr(mod, "INTERVAL")
    assert hasattr(mod, "is_terminal")


# --- Poll / diff behaviour -----------------------------------------------------

def _pipe(status="running", pipeline_id="151111", web_url="https://ex/p/151111"):
    return {"id": pipeline_id, "status": status, "web_url": web_url}


def test_first_poll_baselines_without_event() -> None:
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch", return_value=_pipe(status="running")):
        events, new_state = poller.poll({}, {"id": "151111"})
    assert events == []
    assert new_state["status"] == "running"


def test_running_to_success_emits_succeeded() -> None:
    poller = _load_poller()
    state = {"status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_pipe(status="success")):
        events, _ = poller.poll(state, {"id": "151111"})
    assert len(events) == 1
    assert events[0]["event"] == "pipeline_succeeded"
    assert events[0]["payload"]["pipeline_id"] == "151111"
    assert events[0]["notify_title"]


def test_running_to_failed_emits_failed() -> None:
    poller = _load_poller()
    state = {"status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_pipe(status="failed")):
        events, _ = poller.poll(state, {"id": "151111"})
    assert any(e["event"] == "pipeline_failed" for e in events)


def test_running_to_canceled_emits_canceled() -> None:
    poller = _load_poller()
    state = {"status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_pipe(status="canceled")):
        events, _ = poller.poll(state, {"id": "151111"})
    assert any(e["event"] == "pipeline_canceled" for e in events)


def test_no_change_emits_nothing() -> None:
    poller = _load_poller()
    state = {"status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_pipe(status="running")):
        events, _ = poller.poll(state, {"id": "151111"})
    assert events == []


def test_fetch_failure_preserves_state_no_events() -> None:
    poller = _load_poller()
    with mock.patch.object(poller, "_fetch", return_value=None):
        events, new_state = poller.poll({"status": "running"}, {"id": "151111"})
    assert events == []
    assert new_state == {"status": "running"}


def test_is_terminal_for_terminal_states() -> None:
    poller = _load_poller()
    for st in ("success", "failed", "canceled", "skipped"):
        assert poller.is_terminal({"status": st}) is True


def test_is_not_terminal_while_running() -> None:
    poller = _load_poller()
    assert poller.is_terminal({"status": "running"}) is False
    assert poller.is_terminal({"status": "pending"}) is False
    assert poller.is_terminal({}) is False


def test_glab_helper_reused_from_mr_op() -> None:
    """The poller must reuse the shared _glab_api CLI wrapper, not duplicate it."""
    poller = _load_poller()
    assert poller._glab_api_cli.__module__ == "gitlab_mr_op"
