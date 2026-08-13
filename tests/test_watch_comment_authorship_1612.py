"""A comment event says whether the watching account wrote it (#1612).

`comment_added` carried `author` and nothing else, so a comment the session had
posted itself thirty seconds earlier arrived shaped exactly like somebody
answering a question — which is the one event the watcher exists to deliver.
The harm is the ratio: a maintainer session comments as part of its normal loop,
and an event stream where half the comment events are its own trains the reader
to skim the ones that are not.

**A field, not a filter.** Suppression is a decision the emitter must not make:
a session that posts a comment and then wants confirmation it landed is a real
case, the event is *true*, and a consumer can filter on a field it can see. So
the fix is that the ambiguity is disclosed, and the pins below assert the event
is still emitted in every case.

**What the field can honestly mean.** GitHub answers `viewerDidAuthor` per
comment, against the token the poller authenticates as. That is not "this
session wrote it" — the same account is what a human maintainer comments under
by hand — so the key is `author_is_viewer` rather than `by_you`, which would
claim more than anything here can know. It is four states, not two:

    true     every comment new since the last poll is the viewer's
    false    none of them is
    mixed    some are and some are not — a batch, and the reader must look
    unknown  the payload does not carry the flag, so the poller cannot tell

`mixed` exists because `new_count` can exceed 1 and `author` names only the
*last* of them. Collapsing a batch to `true` because its last comment was the
viewer's is how a real third-party comment becomes invisible, which is the
failure this repository has already paid for once in a filter that quietly
narrowed a population.

`github-issue-feed` sits at the other end: its one REST page carries a comment
*count* and no authorship at all, so `issue_comment_added` says `unknown`
always. Saying so is the point — absent would leave the default reading
("someone replied") standing unchallenged.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

REPO = Path(__file__).parent.parent
PR_POLLER = REPO / "presets" / "watch" / "sources" / "github-pr" / "poller.py"
FEED_POLLER = REPO / "presets" / "watch" / "sources" / "github-issue-feed" / "poller.py"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


poller = _module("github_pr_poller_1612", PR_POLLER)
feed = _module("github_issue_feed_poller_1612", FEED_POLLER)


def _comment(login: str, viewer: object = "omit") -> dict:
    """One entry of gh's `comments` array.

    `viewer="omit"` leaves `viewerDidAuthor` out entirely — the shape a gh old
    enough to predate the field returns, and the reason `unknown` is a state.
    """
    row: dict = {"author": {"login": login}, "body": "hi"}
    if viewer != "omit":
        row["viewerDidAuthor"] = viewer
    return row


def _pr(comments: list) -> dict:
    return {
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "title": "some PR",
        "url": "https://github.com/org/repo/pull/42",
        "number": 42,
        "headRefName": "feature/x",
        "isDraft": False,
        "reviewDecision": "",
        "statusCheckRollup": [],
        "comments": comments,
    }


def _comment_event(prev_count: int, comments: list) -> dict:
    with mock.patch.object(poller, "_fetch", return_value=(_pr(comments), "")):
        events, _ = poller.poll({"comments_count": prev_count}, {"id": "42"})
    matches = [e for e in events if e["event"] == "comment_added"]
    assert len(matches) == 1, f"expected one comment_added, got {events}"
    return matches[0]


# ---- github-pr --------------------------------------------------------------

def test_a_comment_the_viewer_wrote_is_marked_as_theirs() -> None:
    """The #1612 case, verbatim: the session comments and the watcher echoes it."""
    event = _comment_event(0, [_comment("fdaviddpt", viewer=True)])
    assert event["payload"]["author_is_viewer"] == "true"


def test_a_comment_the_viewer_wrote_is_still_emitted() -> None:
    """A field, not a filter — the event must survive being recognised.

    This is the assertion that fails first if anyone later turns the field into
    a suppression, and it is separate from the one above on purpose: a dropped
    event is invisible, so nothing else in this file would notice.
    """
    with mock.patch.object(
        poller, "_fetch", return_value=(_pr([_comment("fdaviddpt", viewer=True)]), "")
    ):
        events, _ = poller.poll({"comments_count": 0}, {"id": "42"})
    assert [e["event"] for e in events if e["event"] == "comment_added"] == [
        "comment_added"
    ]


def test_somebody_elses_comment_is_marked_as_theirs() -> None:
    event = _comment_event(0, [_comment("alice", viewer=False)])
    assert event["payload"]["author_is_viewer"] == "false"
    assert event["payload"]["author"] == "alice"


def test_a_payload_without_the_flag_says_unknown_not_false() -> None:
    """The third state. Absent is not `false`, and must never render as one."""
    event = _comment_event(0, [_comment("alice")])
    assert event["payload"]["author_is_viewer"] == "unknown"


def test_a_flag_that_is_not_a_boolean_says_unknown() -> None:
    event = _comment_event(0, [_comment("alice", viewer="yes")])
    assert event["payload"]["author_is_viewer"] == "unknown"


def test_a_batch_of_the_viewers_own_comments_is_theirs() -> None:
    event = _comment_event(
        0, [_comment("fdaviddpt", viewer=True), _comment("fdaviddpt", viewer=True)]
    )
    assert event["payload"]["new_count"] == 2
    assert event["payload"]["author_is_viewer"] == "true"


def test_a_batch_whose_last_comment_is_the_viewers_is_mixed_not_theirs() -> None:
    """The trap the `mixed` state exists for.

    `author` reports the *last* comment, so a batch ending on the viewer's own
    reply would read as entirely self-authored — and the stranger's comment
    underneath it, the one thing worth waking up for, would be the part that
    disappeared.
    """
    event = _comment_event(
        0, [_comment("alice", viewer=False), _comment("fdaviddpt", viewer=True)]
    )
    assert event["payload"]["new_count"] == 2
    assert event["payload"]["author_is_viewer"] == "mixed"


def test_only_the_comments_new_since_the_last_poll_are_read() -> None:
    """An old comment of the viewer's does not colour a stranger's new one."""
    event = _comment_event(
        1, [_comment("fdaviddpt", viewer=True), _comment("alice", viewer=False)]
    )
    assert event["payload"]["new_count"] == 1
    assert event["payload"]["author_is_viewer"] == "false"


def test_one_unreadable_row_in_a_batch_makes_the_whole_answer_unknown() -> None:
    event = _comment_event(
        0, [_comment("alice", viewer=False), _comment("bob")]
    )
    assert event["payload"]["author_is_viewer"] == "unknown"


def test_the_view_fields_still_ask_for_comments() -> None:
    """The flag rides on the `comments` array the poller already requests.

    No extra API call and no identity lookup: `viewerDidAuthor` is part of gh's
    comment struct. If this field list ever loses `comments`, the whole event
    goes with it, and this says so rather than leaving a silent `unknown`.
    """
    assert "comments" in poller._VIEW_FIELDS


# ---- github-issue-feed ------------------------------------------------------

def _feed_row(comments: int) -> dict:
    return {
        "title": "an issue",
        "url": "https://github.com/o/r/issues/7",
        "labels": [],
        "assignees": [],
        "comments": comments,
        "created_at": "2026-01-01T00:00:00Z",
        "state": "open",
        "state_reason": "",
    }


def test_the_issue_feed_says_it_cannot_tell_who_commented() -> None:
    """One REST page carries a count and no author, so the feed declines.

    Declining out loud rather than staying silent: absent would leave "someone
    replied" as the reader's default, which is the inference #1612 is about.
    """
    events = feed._changes("7", _feed_row(1), _feed_row(2))
    matches = [e for e in events if e["event"] == "issue_comment_added"]
    assert len(matches) == 1
    assert matches[0]["payload"]["author_is_viewer"] == "unknown"
