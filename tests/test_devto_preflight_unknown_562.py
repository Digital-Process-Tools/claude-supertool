"""A devto pre-flight that could not run must not read as "no duplicate" (#562).

`presets/devto/comment.py::preflight_comment` wrapped its own sibling imports —
`from _auth import get_api_key`, `from _rest import request` — in the same
`except Exception` as the HTTP call, and returned `(False, [], "")` for both.
That tuple is also the answer for *the check ran and found nothing*, so a
swallowed `ImportError` was indistinguishable from a clean article, and the
caller went on to post a comment it had never checked for a duplicate of.

#555 is what made the `ImportError` reachable. Before it, `tests/test_devto.py`
left `presets/devto` on `sys.path` for the rest of the process, so the shim
eviction every preset load performs was harmless — the import simply re-resolved
from disk. `tests/_preset_loader.py` now restores `sys.path`, correctly, and the
two mechanisms meet here:

    load_preset_module("devto", "comment")   # devto's shims cached
    load_preset_module("hashnode", "comment")  # pops _auth/_rest (loader:77)
    comment.preflight_comment(999, "me")     # -> (False, [], "")  ← the bug

The fix is hashnode's, already written: `preflight_comment` there returns
`bool | None` and `main` declines to post on `None` (`presets/hashnode/comment.py`
lines 104-147). devto now says the same thing, and the shim imports moved out of
the `try` — a missing `_auth` is a broken checkout, not a platform hiccup, and
conflating the two is the whole defect.

The discriminating test is `test_lookup_failure_is_not_no_duplicate`: it asserts
the two outcomes differ. Every other assertion here would survive a
`preflight_comment` that did nothing but `return False, [], ""`.
"""
from __future__ import annotations

import sys
import types

import pytest

from _preset_loader import load_preset_module

comment_op = load_preset_module("devto", "comment", "dt562_")

#: A comment by somebody else — the check ran, and there is no duplicate.
OTHERS_COMMENT = [{"id": 1, "id_code": "xyz99", "created_at": "2026-05-01T10:00:00Z",
                   "user": {"username": "alice"}, "children": []}]

#: A comment by us — the check ran, and there is a duplicate.
OWN_COMMENT = [{"id": 2, "id_code": "abc12", "created_at": "2026-05-01T10:00:00Z",
                "user": {"username": "max-ai-dev"}, "children": []}]


def _stub_shims(monkeypatch: pytest.MonkeyPatch, request_impl) -> None:
    """Put throwaway `_auth`/`_rest` modules in `sys.modules`, restored after.

    `monkeypatch.setitem` rather than mutating the real shim objects: the
    per-preset modules are shared process-wide and #555's loader hands them
    between test modules, so an unrestored `request` stub leaks into whoever
    imports `_rest` next.
    """
    auth = types.ModuleType("_auth")
    auth.get_api_key = lambda: "fake-key"  # type: ignore[attr-defined]
    rest = types.ModuleType("_rest")
    rest.request = request_impl  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_auth", auth)
    monkeypatch.setitem(sys.modules, "_rest", rest)


def _raise_timeout(*_a, **_kw):
    raise Exception("timeout")


# the discriminating pair ---------------------------------------------------

def test_lookup_failure_is_not_no_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two outcomes must not be the same value.

    This is the assertion the old code could not satisfy, and the reason the
    suite was green: `test_comment_preflight_not_commented` and
    `test_comment_preflight_api_error_degrades` asserted the same tuple.
    """
    _stub_shims(monkeypatch, lambda *a, **kw: OTHERS_COMMENT)
    ran = comment_op.preflight_comment(999, "max-ai-dev")

    _stub_shims(monkeypatch, _raise_timeout)
    failed = comment_op.preflight_comment(999, "max-ai-dev")

    assert ran[0] is False, "an article with only other people's comments is 'no duplicate'"
    assert failed[0] is None, "a lookup that raised has no answer, and must not claim one"
    assert ran != failed


def test_api_error_is_unknown_not_no_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_shims(monkeypatch, _raise_timeout)
    already, ids, last = comment_op.preflight_comment(999, "max-ai-dev")
    assert already is None and ids == [] and last == ""


def test_unparsable_response_is_unknown_not_no_duplicate(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A body that is not a list of comments is an answer we cannot read."""
    _stub_shims(monkeypatch, lambda *a, **kw: {"error": "rate limited"})
    already, _ids, _last = comment_op.preflight_comment(999, "max-ai-dev")
    assert already is None


def test_no_username_is_unknown_not_no_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a username there is nobody to match against — so, no answer."""
    _stub_shims(monkeypatch, lambda *a, **kw: OTHERS_COMMENT)
    already, _ids, _last = comment_op.preflight_comment(999, "")
    assert already is None


# the import, specifically -------------------------------------------------

def test_unreachable_shim_raises_instead_of_answering(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """#562's exact sequence: shims evicted, preset dir off `sys.path`.

    An `ImportError` naming `_rest` sends the reader to `sys.path`; a
    `(False, [], "")` sends them to the assertion, which is the wrong file.
    """
    monkeypatch.setattr(
        sys, "path",
        [p for p in sys.path if "presets" not in p.replace("\\", "/")])
    monkeypatch.delitem(sys.modules, "_auth", raising=False)
    monkeypatch.delitem(sys.modules, "_rest", raising=False)
    with pytest.raises(ImportError) as exc:
        comment_op.preflight_comment(999, "max-ai-dev")
    assert "_auth" in str(exc.value) or "_rest" in str(exc.value)


# main() declines rather than posting unchecked -----------------------------

def _stub_post_path(monkeypatch: pytest.MonkeyPatch, posted: list[str]) -> None:
    monkeypatch.setattr(comment_op, "resolve_article_id", lambda raw: 999)
    monkeypatch.setattr(comment_op, "get_session_cookie", lambda: "cookie-val")
    monkeypatch.setattr(comment_op, "fetch_csrf_token", lambda c: "csrf-tok")
    monkeypatch.setattr(comment_op, "_print_post_confirmation", lambda aid, cid: None)
    monkeypatch.setattr(comment_op, "track_append", lambda rec: None)

    def _post(path, cookie, csrf, body):
        posted.append(str(body))
        return '{"id_code": "new01", "path": "/p"}', 200
    monkeypatch.setattr(comment_op, "web_post_json", _post)


def _stub_me(monkeypatch: pytest.MonkeyPatch, username: str = "max-ai-dev") -> None:
    me = types.ModuleType("_me")
    me.get_username = lambda key: username  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_me", me)


def test_main_declines_to_post_when_the_check_could_not_run(
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    posted: list[str] = []
    _stub_shims(monkeypatch, _raise_timeout)
    _stub_me(monkeypatch)
    _stub_post_path(monkeypatch, posted)
    with pytest.raises(SystemExit) as exc:
        comment_op.main("999|Hello world")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ABORT" in err
    assert "cannot verify" in err, "the reason has to name what was not established"
    assert "force" in err, "a decline the caller cannot override is a dead end"
    assert posted == [], "nothing may be posted on the strength of a check that failed"


def test_main_declines_when_the_user_cannot_be_identified(
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """No username means the duplicate check cannot run at all.

    This path used to print a WARNING and post anyway — announced, but in a way
    that scrolls past, and the comment went out unchecked either way.
    """
    posted: list[str] = []
    _stub_shims(monkeypatch, lambda *a, **kw: OTHERS_COMMENT)
    broken = types.ModuleType("_me")

    def _boom(key):
        raise Exception("401 unauthorized")
    broken.get_username = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_me", broken)
    _stub_post_path(monkeypatch, posted)
    with pytest.raises(SystemExit) as exc:
        comment_op.main("999|Hello world")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ABORT" in err and "401 unauthorized" in err
    assert posted == []


# guards against this fix over-reaching ------------------------------------
# These four pass before the fix as well. They are here so a change that turns
# every pre-flight outcome into "unknown" — trading the quiet bug for a loud
# one — fails instead of looking like progress.

def test_a_real_duplicate_still_aborts(monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    posted: list[str] = []
    _stub_shims(monkeypatch, lambda *a, **kw: OWN_COMMENT)
    _stub_me(monkeypatch)
    _stub_post_path(monkeypatch, posted)
    with pytest.raises(SystemExit) as exc:
        comment_op.main("999|Hello world")
    assert exc.value.code == 1
    assert "already commented" in capsys.readouterr().err
    assert posted == []


def test_a_clean_article_still_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[str] = []
    _stub_shims(monkeypatch, lambda *a, **kw: OTHERS_COMMENT)
    _stub_me(monkeypatch)
    _stub_post_path(monkeypatch, posted)
    comment_op.main("999|Hello world")
    assert len(posted) == 1 and "Hello world" in posted[0]


def test_a_real_duplicate_is_still_reported_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_shims(monkeypatch, lambda *a, **kw: OWN_COMMENT)
    already, ids, last = comment_op.preflight_comment(999, "max-ai-dev")
    assert already is True and ids == ["abc12"] and last == "2026-05-01"


def test_force_still_skips_the_pre_flight_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[str] = []
    looked: list[str] = []

    def _track(*_a, **_kw):
        looked.append("lookup")
        return OWN_COMMENT
    _stub_shims(monkeypatch, _track)
    _stub_me(monkeypatch)
    _stub_post_path(monkeypatch, posted)
    comment_op.main("999|Hello world||force")
    assert looked == [] and len(posted) == 1
