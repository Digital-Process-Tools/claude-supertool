"""#2202: a file/image upload reaches the session as a filename and
nothing else -- no url, no mimetype, no bytes, and no way to ask for them.

`_content_summary` already names a file upload honestly (#2068's three-state
discipline: `content_kind == "files"` when `text` was empty but `files` was
not). What never survived past that naming is the metadata Slack already
handed this poller in the same `files[]` entry: `permalink` (a link a human
can open), `mimetype` and `size` (so a receiver can tell an image from a
400MB archive without fetching anything).

This is issue #2202's own route 1 -- carry the metadata, not the bytes.
Fetching bytes (route 2) needs a bearer token and a sizing policy against
the per-channel eviction pool #2149/#2197 is concurrently reworking; this
stays out of that file entirely.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

SOURCE_DIR = WATCH_DIR / "sources" / "slack"
POLLER = SOURCE_DIR / "poller.py"

CHANNEL = "C0123456"
BOT_UID = "U_BOT"
CTX = {"source": "slack", "id": CHANNEL, "only": []}


@pytest.fixture(autouse=True)
def _fake_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake-not-a-real-token-0006")


def _load_poller():
    spec = importlib.util.spec_from_file_location("slack_watch_poller_2202", POLLER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _event_for_msg(poller, msg: dict, *, tmp_path=None, monkeypatch=None) -> dict:
    if tmp_path is not None:
        monkeypatch.setattr(poller.transport, "STATE_DIR", str(tmp_path))
    msg = dict(msg)
    msg.setdefault("ts", "1.0")
    msg.setdefault("user", BOT_UID)
    with mock.patch.object(poller, "resolve_bot_user_id", return_value=BOT_UID), \
         mock.patch.object(poller, "_fetch", return_value=([msg], "")):
        events, _ = poller.poll({"cursor": "0.5", "bot_user_id": BOT_UID}, CTX)
    return events[0]["payload"]


def test_an_image_upload_carries_permalink_mimetype_and_size() -> None:
    """The exact gap the issue names: naming the file is not enough --
    the receiver must be able to reach it or at least know what it is."""
    poller = _load_poller()
    payload = _event_for_msg(poller, {
        "text": "",
        "files": [{
            "name": "screenshot.png",
            "mimetype": "image/png",
            "size": 48213,
            "permalink": "https://example.slack.com/files/U1/F1/screenshot.png",
            "url_private": "https://files.slack.com/files-pri/T1-F1/screenshot.png",
        }],
    })
    assert payload["content_kind"] == "files"
    assert len(payload["files"]) == 1
    meta = payload["files"][0]
    assert meta["name"] == "screenshot.png"
    assert meta["mimetype"] == "image/png"
    assert meta["size"] == 48213
    assert meta["permalink"] == "https://example.slack.com/files/U1/F1/screenshot.png"
    # Route 1, not route 2 -- the bearer-token-gated download URL never
    # rides in the emitted event, only the permalink a human can open.
    assert "url_private" not in meta


def test_multiple_files_each_carry_their_own_metadata() -> None:
    poller = _load_poller()
    payload = _event_for_msg(poller, {
        "text": "",
        "files": [
            {"name": "a.png", "mimetype": "image/png", "size": 10,
             "permalink": "https://x/a"},
            {"name": "b.pdf", "mimetype": "application/pdf", "size": 20,
             "permalink": "https://x/b"},
        ],
    })
    assert [f["name"] for f in payload["files"]] == ["a.png", "b.pdf"]
    assert [f["mimetype"] for f in payload["files"]] == ["image/png", "application/pdf"]
    assert [f["size"] for f in payload["files"]] == [10, 20]


def test_a_file_entry_missing_metadata_fields_defaults_rather_than_raises() -> None:
    """Slack's own docs do not guarantee every field on every file type
    (e.g. external/tombstoned files can lack `mimetype` or `size`). Must
    not raise, must not silently drop the file -- absence is named, not
    swallowed."""
    poller = _load_poller()
    payload = _event_for_msg(poller, {
        "text": "",
        "files": [{"name": "mystery"}],
    })
    assert len(payload["files"]) == 1
    meta = payload["files"][0]
    assert meta["name"] == "mystery"
    assert meta["mimetype"] == ""
    assert meta["size"] == 0
    assert meta["permalink"] == ""


def test_must_not_fire_a_text_message_carries_no_file_metadata() -> None:
    """Positive control: an ordinary text message must not start growing a
    `files` list it never had -- this is #2068's `content_kind == "text"`
    path, unrelated to this fix."""
    poller = _load_poller()
    payload = _event_for_msg(poller, {"text": "hello team"},
                             tmp_path=None, monkeypatch=None)
    assert payload["content_kind"] == "text"
    assert payload["files"] == []


def test_a_genuinely_empty_message_also_carries_no_file_metadata() -> None:
    """Positive control, #2068's other empty arm: `content_kind == "empty"`
    must not spuriously populate `files` either."""
    poller = _load_poller()
    payload = _event_for_msg(poller, {"text": ""})
    assert payload["content_kind"] == "empty"
    assert payload["files"] == []


def test_blocks_and_attachments_kinds_also_carry_no_file_metadata() -> None:
    """`files` is scoped to `content_kind == "files"` only -- a block-kit
    or attachment message has no `files[]` in the source message at all,
    so nothing here should invent one."""
    poller = _load_poller()
    blocks_payload = _event_for_msg(poller, {
        "text": "", "blocks": [{"type": "section"}],
    })
    assert blocks_payload["content_kind"] == "blocks"
    assert blocks_payload["files"] == []

    attach_payload = _event_for_msg(poller, {
        "text": "", "attachments": [{"fallback": "x"}],
    })
    assert attach_payload["content_kind"] == "attachments"
    assert attach_payload["files"] == []
