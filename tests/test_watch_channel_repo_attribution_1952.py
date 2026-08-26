"""End-to-end tests for repository attribution on a watch event (#1952).

A `github-pr` (or any other) watcher's event carried the PR number and never
the repository, so a desktop notification or an MCP `<channel>` tag read as
`github-pr 527: merged` — ambiguous across every repository, by construction,
because every repository has a `#527`. `repo` is recoverable from `url`, but
`url` never reaches the body line a human actually reads.

`repo` is written by the poller from its own configuration (a `git remote`,
never the forge object being polled), so it is treated the same way as
`watcher_source` and `id`: always an attribute, never evicted by the size
clamp, never overridable by a payload key of the same name.
"""
from __future__ import annotations

import shutil
import socket as _socket

import pytest

from _toolchain_gate import js_promised, require_or_skip
from test_notifiers_claude_channel_554 import Channel, NODE_MODULES  # noqa: F401
from test_notifiers_claude_channel_554 import _meta  # noqa: F401

pytestmark = [
    require_or_skip(
        hasattr(_socket, "AF_UNIX"),
        "claude-channel binds an AF_UNIX socket — not available on this platform",
        promised=js_promised(),
    ),
    require_or_skip(
        shutil.which("bun") is not None,
        "claude-channel runs under bun; no bun on PATH",
        promised=js_promised(),
    ),
    require_or_skip(
        NODE_MODULES.exists(),
        "channel deps not installed — run notifiers/claude-channel/install.sh",
        promised=js_promised(),
    ),
]


@pytest.fixture()
def channel():
    ch = Channel()
    try:
        yield ch
    finally:
        ch.close()


def test_repo_reaches_the_attribute_and_the_body_line(channel: Channel) -> None:
    channel.emit({
        "ts": "2026-08-25T12:01:49Z", "source": "github-pr", "id": "527",
        "event": "merged", "repo": "OWNER/REPO",
        "payload": {"title": "Resolve a defect", "url": "https://github.com/OWNER/REPO/pull/527"},
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert meta["repo"] == "OWNER/REPO", meta
    body = msg["params"]["content"]
    assert body.startswith("github-pr OWNER/REPO#527: merged"), body


def test_a_missing_repo_is_absent_not_a_guess(channel: Channel) -> None:
    """A poller that predates the field, or could not resolve its own
    repository, must not have one invented for it — the id-only line is the
    honest answer for that case, not a wrong or blank repository."""
    channel.emit({
        "ts": "2026-08-25T12:01:49Z", "source": "github-pr", "id": "527",
        "event": "merged",
        "payload": {"title": "Resolve a defect"},
    })
    msg = channel.next_message()
    meta = _meta(msg)
    assert "repo" not in meta, meta
    body = msg["params"]["content"]
    assert body.startswith("github-pr 527: merged"), body


def test_a_payload_repo_cannot_override_the_written_one(channel: Channel) -> None:
    """Same guard as #609's `id`/`event`/`ts` collision: a payload key must
    never be able to change what an event *is*, and repo is exactly as much
    an identity field as those — it decides which project a reader attributes
    the event to."""
    channel.emit({
        "ts": "2026-08-25T12:01:49Z", "source": "github-pr", "id": "527",
        "event": "merged", "repo": "REAL/REPO",
        "payload": {"repo": "FORGED/REPO", "title": "x"},
    })
    meta = _meta(channel.next_message())
    assert meta["repo"] == "REAL/REPO", meta


def test_repo_is_never_evicted_by_the_size_clamp(channel: Channel) -> None:
    """`repo` is tiny and load-bearing, same footing as `watcher_source` and
    `id`: a routing key is never the reason an event goes over budget."""
    channel.emit({
        "ts": "2026-08-25T12:01:49Z", "source": "github-pr", "id": "527",
        "event": "merged", "repo": "OWNER/REPO",
        "payload": {"title": "x" * 3000},
    })
    meta = _meta(channel.next_message())
    assert meta["repo"] == "OWNER/REPO", meta
