"""#601 — a pre-flight that could not run is not "no duplicate".

#562/#599 fixed one instance (`presets/devto/comment.py`). This file covers the
four production instances of the same shape that were left: `devto/publish.py`,
`bluesky/follow.py`, `bluesky/like.py`, `bluesky/publish.py`. Each returned the
*negative answer* on its own failure path, so a lookup that blew up read as
"nothing there" and authorised the write it was supposed to guard.

The bar is #599's: a test that cannot tell "checked, nothing there" apart from
"could not check" *is* the bug, so every op here gets a paired assertion that
the two outcomes **differ**. That is the one assertion the old code could not
satisfy — `False` was both answers.

Some tests here pass before the fix as well as after. They are guards against
*this* change: a patch that turns every pre-flight outcome into "unknown" trades
the quiet bug for a loud one, and would otherwise read as progress.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from _preset_loader import load_preset_module

SESSION = {"did": "did:plc:me", "accessJwt": "tok"}


def _boom(*_a, **_kw):
    raise RuntimeError("network down")


def _exit_boom(*_a, **_kw):
    """What `xrpc` actually does on an HTTP or network error: sys.exit(1).

    `SystemExit` subclasses `BaseException`, so this is the failure mode a bare
    `except Exception` in a pre-flight does *not* catch and the reason three of
    these functions name it explicitly.
    """
    sys.stderr.write("ERROR: xrpc: 502 Bad Gateway\n")
    raise SystemExit(1)


# devto_publish -----------------------------------------------------------
# A failed /articles/me lookup used to read as "no article with this
# canonical_url", and the op published a duplicate article.

devto_publish = load_preset_module("devto", "publish", "dv601_")


def _devto_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devto_publish, "get_api_key", lambda: "fake")
    monkeypatch.setattr(devto_publish, "request", lambda *a, **kw: [
        {"canonical_url": "https://other.io", "url": "https://dev.to/u/o", "slug": "u-o"}])


def test_devto_publish_lookup_failure_is_not_no_duplicate(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The assertion the old code could not satisfy: the two outcomes differ."""
    _devto_clean(monkeypatch)
    checked = devto_publish.preflight_publish("https://x.io", "fake")

    monkeypatch.setattr(devto_publish, "request", _boom)
    unchecked = devto_publish.preflight_publish("https://x.io", "fake")

    assert checked[0] is False
    assert unchecked[0] is None
    assert checked[0] is not unchecked[0]


def test_devto_publish_api_error_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devto_publish, "get_api_key", lambda: "fake")
    monkeypatch.setattr(devto_publish, "request", _boom)
    already, url, slug = devto_publish.preflight_publish("https://x.io", "fake")
    assert already is None and url == "" and slug == ""


def test_devto_publish_unparsable_response_is_unknown(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A rate-limit body is not an empty article list."""
    monkeypatch.setattr(devto_publish, "get_api_key", lambda: "fake")
    monkeypatch.setattr(devto_publish, "request",
                        lambda *a, **kw: {"error": "Too Many Requests"})
    already, _url, _slug = devto_publish.preflight_publish("https://x.io", "fake")
    assert already is None


def test_devto_publish_main_declines_when_the_check_could_not_run(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        tmp_path: Path) -> None:
    md = tmp_path / "p.md"
    md.write_text("body")
    calls: list[str] = []

    def fake_request(method: str, *_a, **_kw):
        calls.append(method)
        if method == "GET":
            raise RuntimeError("500")
        return {"id": 1, "slug": "s", "url": "u", "title": "T"}

    monkeypatch.setattr(devto_publish, "get_api_key", lambda: "fake")
    monkeypatch.setattr(devto_publish, "request", fake_request)
    with pytest.raises(SystemExit) as exc:
        devto_publish.main(f"T|{md}|https://x.io")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ABORT" in err and "force" in err
    assert "POST" not in calls, "declined but published anyway"


def test_devto_publish_main_still_publishes_a_clean_article(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        tmp_path: Path) -> None:
    """Guard against over-reach: a check that ran must still let the op through."""
    md = tmp_path / "p.md"
    md.write_text("body")

    def fake_request(method: str, *_a, **_kw):
        if method == "GET":
            return [{"canonical_url": "https://other.io", "url": "o", "slug": "u-o"}]
        return {"id": 99, "slug": "new", "url": "https://dev.to/u/new", "title": "T"}

    monkeypatch.setattr(devto_publish, "get_api_key", lambda: "fake")
    monkeypatch.setattr(devto_publish, "request", fake_request)
    devto_publish.main(f"T|{md}|https://x.io")
    assert "published" in capsys.readouterr().out


def test_devto_publish_main_still_aborts_on_a_real_duplicate(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        tmp_path: Path) -> None:
    """Guard: the answer 'yes, a duplicate' must not become 'unknown'."""
    md = tmp_path / "p.md"
    md.write_text("body")
    monkeypatch.setattr(devto_publish, "get_api_key", lambda: "fake")
    monkeypatch.setattr(devto_publish, "request", lambda *a, **kw: [
        {"canonical_url": "https://x.io", "url": "https://dev.to/u/s", "slug": "u-s"}])
    with pytest.raises(SystemExit) as exc:
        devto_publish.main(f"T|{md}|https://x.io")
    assert exc.value.code == 1
    assert "u-s" in capsys.readouterr().err, "lost the duplicate's identity"


# bluesky_publish (reply) -------------------------------------------------
# The only one of the four whose duplicate is a public artifact on somebody
# else's thread — the #599 asymmetry, unchanged.

bsky_publish = load_preset_module("bluesky", "publish", "bs601_")

ROOT_URI = "at://root/post/1"
REPLY_URI = "at://parent/post/2"


def _thread(**_kw):
    return {"thread": {"post": {"uri": REPLY_URI, "cid": "c",
                                "record": {"reply": {"root": {"uri": ROOT_URI}}}}}}


def _bsky_feed(other_root: str):
    def fake_xrpc(nsid, _s, **_kw):
        if nsid == "app.bsky.feed.getPostThread":
            return _thread()
        return {"feed": [{"post": {"uri": "at://me/post/9",
                                   "record": {"reply": {"root": {"uri": other_root}}}}}]}
    return fake_xrpc


def test_bsky_reply_lookup_failure_is_not_no_duplicate(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bsky_publish, "xrpc", _bsky_feed("at://other/root/1"))
    checked = bsky_publish.preflight_publish(REPLY_URI, SESSION)

    monkeypatch.setattr(bsky_publish, "xrpc", _boom)
    unchecked = bsky_publish.preflight_publish(REPLY_URI, SESSION)

    assert checked is False
    assert unchecked is None
    assert checked is not unchecked


def test_bsky_reply_transport_exit_is_unknown_not_no_duplicate(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`xrpc` exits rather than raising; that is an accident, not an answer."""
    monkeypatch.setattr(bsky_publish, "xrpc", _exit_boom)
    assert bsky_publish.preflight_publish(REPLY_URI, SESSION) is None


def test_bsky_reply_unresolvable_root_is_unknown(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_root_uri returning None means the root is unknown, not absent.

    It already swallows `SystemExit` deliberately (a 404 reply target must not
    kill the op), so its None is the *only* way that decision reaches the
    caller. Reading it as "no duplicate" discarded it.
    """
    monkeypatch.setattr(bsky_publish, "_get_root_uri", lambda _s, _u: None)
    assert bsky_publish.preflight_publish(REPLY_URI, SESSION) is None


def test_bsky_reply_main_declines_when_the_check_could_not_run(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []

    def fake_xrpc(nsid, _s, **_kw):
        calls.append(nsid)
        if nsid == "app.bsky.feed.getPostThread":
            return _thread()
        raise RuntimeError("feed unavailable")

    monkeypatch.setattr(bsky_publish, "get_handle", lambda: "me.bsky.social")
    monkeypatch.setattr(bsky_publish, "get_app_password", lambda: "pw")
    monkeypatch.setattr(bsky_publish, "get_session", lambda h, p: SESSION)
    monkeypatch.setattr(bsky_publish, "xrpc", fake_xrpc)
    with pytest.raises(SystemExit) as exc:
        bsky_publish.main(f"Hello|{REPLY_URI}")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ABORT" in err and "force" in err
    assert "com.atproto.repo.createRecord" not in calls, "declined but posted anyway"


def test_bsky_reply_main_still_posts_when_the_thread_is_clean(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Guard against over-reach."""
    def fake_xrpc(nsid, _s, method="GET", params=None, body=None, **_kw):
        if nsid == "app.bsky.feed.getPostThread":
            return _thread()
        if nsid == "app.bsky.feed.getAuthorFeed":
            return {"feed": [{"post": {"uri": "at://me/post/9",
                                       "record": {"reply": {"root": {"uri": "at://o/r/1"}}}}}]}
        return {"uri": "at://me/app.bsky.feed.post/new", "cid": "cid1"}

    monkeypatch.setattr(bsky_publish, "get_handle", lambda: "me.bsky.social")
    monkeypatch.setattr(bsky_publish, "get_app_password", lambda: "pw")
    monkeypatch.setattr(bsky_publish, "get_session", lambda h, p: SESSION)
    monkeypatch.setattr(bsky_publish, "xrpc", fake_xrpc)
    bsky_publish.main(f"Hello|{REPLY_URI}")
    assert "published" in capsys.readouterr().out


# bluesky_like ------------------------------------------------------------

bsky_like = load_preset_module("bluesky", "like", "bs601_")

LIKE_URI = "at://did:plc:abc/app.bsky.feed.post/xyz"


def test_bsky_like_check_failure_is_not_not_liked(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bsky_like, "xrpc", lambda nsid, s, **kw: {
        "feed": [{"post": {"uri": "at://other/post/1"}}]})
    checked = bsky_like.preflight_like(LIKE_URI, SESSION)

    monkeypatch.setattr(bsky_like, "xrpc", _boom)
    unchecked = bsky_like.preflight_like(LIKE_URI, SESSION)

    assert checked is False
    assert unchecked is None
    assert checked is not unchecked


def test_bsky_like_transport_exit_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before: `xrpc`'s exit escaped `except Exception` and killed the op with
    `ERROR:` and no `|force` hint. Now it is a named third state."""
    monkeypatch.setattr(bsky_like, "xrpc", _exit_boom)
    assert bsky_like.preflight_like(LIKE_URI, SESSION) is None


def test_bsky_like_main_declines_when_the_check_could_not_run(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []

    def fake_xrpc(nsid, _s, **_kw):
        calls.append(nsid)
        raise RuntimeError("likes unavailable")

    monkeypatch.setattr(bsky_like, "get_handle", lambda: "me.bsky.social")
    monkeypatch.setattr(bsky_like, "get_app_password", lambda: "pw")
    monkeypatch.setattr(bsky_like, "get_session", lambda h, p: SESSION)
    monkeypatch.setattr(bsky_like, "to_at_uri", lambda arg, s: LIKE_URI)
    monkeypatch.setattr(bsky_like, "xrpc", fake_xrpc)
    with pytest.raises(SystemExit) as exc:
        bsky_like.main(LIKE_URI)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ABORT" in err and "force" in err
    assert "com.atproto.repo.createRecord" not in calls, "declined but liked anyway"


def test_bsky_like_main_still_likes_an_unliked_post(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Guard against over-reach."""
    def fake_xrpc(nsid, _s, method="GET", params=None, body=None, **_kw):
        if nsid == "app.bsky.feed.getActorLikes":
            return {"feed": [{"post": {"uri": "at://other/post/1"}}]}
        if nsid == "app.bsky.feed.getPostThread":
            return {"thread": {"post": {"uri": LIKE_URI, "cid": "cid1"}}}
        return {"uri": "at://me/app.bsky.feed.like/1"}

    monkeypatch.setattr(bsky_like, "get_handle", lambda: "me.bsky.social")
    monkeypatch.setattr(bsky_like, "get_app_password", lambda: "pw")
    monkeypatch.setattr(bsky_like, "get_session", lambda h, p: SESSION)
    monkeypatch.setattr(bsky_like, "to_at_uri", lambda arg, s: LIKE_URI)
    monkeypatch.setattr(bsky_like, "xrpc", fake_xrpc)
    bsky_like.main(LIKE_URI)
    assert "liked" in capsys.readouterr().out


# bluesky_follow ----------------------------------------------------------

bsky_follow = load_preset_module("bluesky", "follow", "bs601_")

TARGET_DID = "did:plc:alice"


def test_bsky_follow_check_failure_is_not_not_following(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bsky_follow, "xrpc", lambda nsid, s, **kw: {
        "follows": [{"did": "did:plc:bob"}]})
    checked = bsky_follow.preflight_follow(TARGET_DID, SESSION)

    monkeypatch.setattr(bsky_follow, "xrpc", _boom)
    unchecked = bsky_follow.preflight_follow(TARGET_DID, SESSION)

    assert checked is False
    assert unchecked is None
    assert checked is not unchecked


def test_bsky_follow_transport_exit_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bsky_follow, "xrpc", _exit_boom)
    assert bsky_follow.preflight_follow(TARGET_DID, SESSION) is None


def test_bsky_follow_main_declines_when_the_check_could_not_run(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []

    def fake_xrpc(nsid, _s, **_kw):
        calls.append(nsid)
        raise RuntimeError("follows unavailable")

    monkeypatch.setattr(bsky_follow, "get_handle", lambda: "me.bsky.social")
    monkeypatch.setattr(bsky_follow, "get_app_password", lambda: "pw")
    monkeypatch.setattr(bsky_follow, "get_session", lambda h, p: SESSION)
    monkeypatch.setattr(bsky_follow, "xrpc", fake_xrpc)
    with pytest.raises(SystemExit) as exc:
        bsky_follow.main(TARGET_DID)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ABORT" in err and "force" in err
    assert "com.atproto.repo.createRecord" not in calls, "declined but followed anyway"


def test_bsky_follow_main_still_follows_a_new_account(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Guard against over-reach."""
    def fake_xrpc(nsid, _s, method="GET", params=None, body=None, **_kw):
        if nsid == "app.bsky.graph.getFollows":
            return {"follows": [{"did": "did:plc:bob"}]}
        return {"uri": "at://me/app.bsky.graph.follow/1"}

    monkeypatch.setattr(bsky_follow, "get_handle", lambda: "me.bsky.social")
    monkeypatch.setattr(bsky_follow, "get_app_password", lambda: "pw")
    monkeypatch.setattr(bsky_follow, "get_session", lambda h, p: SESSION)
    monkeypatch.setattr(bsky_follow, "xrpc", fake_xrpc)
    bsky_follow.main(TARGET_DID)
    assert "followed" in capsys.readouterr().out


# force still bypasses everything ---------------------------------------
# Declining is only defensible while the documented way through still works.

def test_force_still_skips_the_bluesky_like_preflight(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []

    def fake_xrpc(nsid, _s, method="GET", params=None, body=None, **_kw):
        calls.append(nsid)
        if nsid == "app.bsky.feed.getPostThread":
            return {"thread": {"post": {"uri": LIKE_URI, "cid": "cid1"}}}
        return {"uri": "at://me/app.bsky.feed.like/1"}

    monkeypatch.setattr(bsky_like, "get_handle", lambda: "me.bsky.social")
    monkeypatch.setattr(bsky_like, "get_app_password", lambda: "pw")
    monkeypatch.setattr(bsky_like, "get_session", lambda h, p: SESSION)
    monkeypatch.setattr(bsky_like, "to_at_uri", lambda arg, s: LIKE_URI)
    monkeypatch.setattr(bsky_like, "xrpc", fake_xrpc)
    bsky_like.main(f"{LIKE_URI}|force")
    assert "app.bsky.feed.getActorLikes" not in calls
    assert "liked" in capsys.readouterr().out


def test_force_still_skips_the_bluesky_follow_preflight(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []

    def fake_xrpc(nsid, _s, method="GET", params=None, body=None, **_kw):
        calls.append(nsid)
        return {"uri": "at://me/app.bsky.graph.follow/1"}

    monkeypatch.setattr(bsky_follow, "get_handle", lambda: "me.bsky.social")
    monkeypatch.setattr(bsky_follow, "get_app_password", lambda: "pw")
    monkeypatch.setattr(bsky_follow, "get_session", lambda h, p: SESSION)
    monkeypatch.setattr(bsky_follow, "xrpc", fake_xrpc)
    bsky_follow.main(f"{TARGET_DID}|force")
    assert "app.bsky.graph.getFollows" not in calls
    assert "followed" in capsys.readouterr().out


def test_force_still_skips_the_devto_publish_preflight(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        tmp_path: Path) -> None:
    md = tmp_path / "p.md"
    md.write_text("body")
    calls: list[str] = []

    def fake_request(method: str, *_a, **_kw):
        calls.append(method)
        return {"id": 99, "slug": "new", "url": "https://dev.to/u/new", "title": "T"}

    monkeypatch.setattr(devto_publish, "get_api_key", lambda: "fake")
    monkeypatch.setattr(devto_publish, "request", fake_request)
    devto_publish.main(f"T|{md}|https://x.io||||force")
    assert "GET" not in calls
    assert "published" in capsys.readouterr().out
