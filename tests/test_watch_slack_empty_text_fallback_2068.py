"""#2068: `msg.get("text")` alone is not enough. A file/image upload, a
snippet, a Block Kit message, or an app-posted attachment all arrive with an
empty `text`, and the poller used to emit `title=""` for every one of them --
indistinguishable from a message that really was empty, and from an
extraction that could not run at all. Three states, never two: real content,
a named fallback, or a title that is legitimately empty and says so via
`content_kind`.
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
    spec = importlib.util.spec_from_file_location("slack_watch_poller_2068", POLLER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _event_for_msg(poller, msg: dict) -> dict:
    msg = dict(msg)
    msg.setdefault("ts", "1.0")
    msg.setdefault("user", BOT_UID)
    with mock.patch.object(poller, "resolve_bot_user_id", return_value=BOT_UID), \
         mock.patch.object(poller, "_fetch", return_value=([msg], "")):
        events, _ = poller.poll({"cursor": "0.5", "bot_user_id": BOT_UID}, CTX)
    return events[0]["payload"]


def test_ordinary_text_message_is_unaffected() -> None:
    """Must-not-fire pair: a normal message with real `text` must not start
    naming a fallback it never used."""
    poller = _load_poller()
    payload = _event_for_msg(poller, {"text": "hello team"})
    assert payload["title"] == "hello team"
    assert payload["content_kind"] == "text"


def test_a_file_upload_with_no_text_names_the_file_rather_than_emitting_empty() -> None:
    """The exact case the issue asks for: `text` absent, `files` present,
    the emitted `title` must not be the empty string."""
    poller = _load_poller()
    payload = _event_for_msg(poller, {
        "text": "",
        "files": [{"name": "diagram.png"}],
    })
    assert payload["title"] != ""
    assert "diagram.png" in payload["title"]
    assert payload["content_kind"] == "files"


def test_multiple_files_are_all_named() -> None:
    poller = _load_poller()
    payload = _event_for_msg(poller, {
        "text": "",
        "files": [{"name": "a.png"}, {"name": "b.pdf"}],
    })
    assert "a.png" in payload["title"]
    assert "b.pdf" in payload["title"]
    assert payload["content_kind"] == "files"


def test_a_block_kit_message_with_no_text_is_named_not_emptied() -> None:
    poller = _load_poller()
    payload = _event_for_msg(poller, {
        "text": "",
        "blocks": [{"type": "section"}, {"type": "divider"}],
    })
    assert payload["title"] != ""
    assert "2" in payload["title"]
    assert payload["content_kind"] == "blocks"


def test_an_app_posted_attachment_with_no_text_is_named_not_emptied() -> None:
    poller = _load_poller()
    payload = _event_for_msg(poller, {
        "text": "",
        "attachments": [{"fallback": "some legacy attachment"}],
    })
    assert payload["title"] != ""
    assert payload["content_kind"] == "attachments"


def test_a_genuinely_empty_message_is_the_only_case_that_stays_empty() -> None:
    """Positive control for the whole file: an empty `title` must still be
    reachable, and must say so via `content_kind`, rather than this fix
    inventing text for a message that truly carried none (e.g. an edit or
    reaction-only context with no files/blocks/attachments either)."""
    poller = _load_poller()
    payload = _event_for_msg(poller, {"text": ""})
    assert payload["title"] == ""
    assert payload["content_kind"] == "empty"


def test_empty_title_and_unextracted_title_are_never_the_same_content_kind() -> None:
    """The governing point of the issue: an empty title from a genuinely
    empty message, and one whose content this poller found but named rather
    than reproduced, must be distinguishable by a downstream reader without
    guessing."""
    poller = _load_poller()
    genuinely_empty = _event_for_msg(poller, {"text": ""})
    had_a_file = _event_for_msg(poller, {"text": "", "files": [{"name": "x.png"}]})
    assert genuinely_empty["content_kind"] != had_a_file["content_kind"]
    assert genuinely_empty["title"] == ""
    assert had_a_file["title"] != ""
