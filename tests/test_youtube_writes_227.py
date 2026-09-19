"""youtube_auth + youtube_comment -- the OAuth2 write half of #227.

Nothing here reaches the network. The HTTP seam is `_yt.authorized` and
`_oauth._post_form`, both monkeypatched; the filesystem seam is
`~/.config/youtube`, redirected into `tmp_path` per test.

What these are mostly about is the refusals. The op posts a public comment
under the operator's own name, on somebody else's video, and this preset has
no delete op -- so every guard is tested in BOTH directions, because an
assertion that a guard refused also passes when the guard refuses everything,
and one that it allowed also passes when nothing is guarded at all.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from _preset_loader import load_preset_module

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "presets"))
import _publish_safety  # noqa: E402


def _load(name: str):
    return load_preset_module("youtube", name, "yt_")


sentinel = _load("_sentinel")
oauth = _load("_oauth")
comment_op = _load("comment")
auth_op = _load("auth")


def _module_aliases(name: str, loaded) -> list:
    """Every live object for one preset module, deduplicated by identity.

    `load_preset_module` imports under a prefixed name (`yt__oauth`), while
    `comment.py` and `auth.py` do a plain `from _oauth import ...` off their
    own `sys.path` entry. Those are two distinct module objects holding two
    distinct `TOKEN_PATH`s, so patching only the one this file loaded leaves
    the op reading the real `~/.config/youtube`. Three status tests failed
    that way before this existed -- and the failure mode is worse than a red
    test, because a test that patched nothing would have exercised, and could
    have written to, the developer's actual credential directory.
    """
    import sys as _sys
    out = [loaded]
    other = _sys.modules.get(name)
    if other is not None and other is not loaded:
        out.append(other)
    return out


@pytest.fixture()
def config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every live copy of every module's ~/.config/youtube at tmp_path."""
    cfg = tmp_path / "config" / "youtube"
    cfg.mkdir(parents=True)
    for mod in _module_aliases("_sentinel", sentinel):
        monkeypatch.setattr(mod, "CONFIG_DIR", cfg)
        monkeypatch.setattr(mod, "LOG_PATH", cfg / "sent.jsonl")
    for mod in _module_aliases("_oauth", oauth):
        monkeypatch.setattr(mod, "CONFIG_DIR", cfg)
        monkeypatch.setattr(mod, "TOKEN_PATH", cfg / "oauth_token.json")
        monkeypatch.setattr(mod, "CLIENT_SECRET_PATH", cfg / "client_secret.json")
    monkeypatch.setattr(auth_op, "TOKEN_PATH", cfg / "oauth_token.json")
    monkeypatch.setattr(auth_op, "CLIENT_SECRET_PATH", cfg / "client_secret.json")
    return cfg


def test_the_fixture_actually_redirects_every_alias(config_home: Path) -> None:
    """Positive control for the fixture itself. If this file's `oauth` and the
    one `auth.py` imported ever stop being redirected together, the status
    tests below start reading the real home directory and passing or failing
    for reasons that have nothing to do with the code.
    """
    import sys as _sys
    for name, loaded in (("_oauth", oauth), ("_sentinel", sentinel)):
        for mod in _module_aliases(name, loaded):
            attr = "TOKEN_PATH" if name == "_oauth" else "LOG_PATH"
            assert str(getattr(mod, attr)).startswith(str(config_home)), (
                f"{name} alias {mod!r} still points at {getattr(mod, attr)}")
        assert _sys.modules.get(name) is not None or loaded is not None


# --- the sentinel: three states, both directions -------------------------

def test_no_log_yet_is_ok_not_unknown(config_home: Path) -> None:
    """A missing file is a real answer: nothing was ever written."""
    verdict, _ = sentinel.check("vid1")
    assert verdict == "ok"


def test_a_video_already_commented_on_is_refused(config_home: Path) -> None:
    sentinel.record(op="youtube_comment", video_id="vid1", comment_id="c1",
                    url="u", verification="verified")
    verdict, reason = sentinel.check("vid1")
    assert verdict == "refuse"
    assert "vid1" in reason and "c1" in reason


def test_a_different_video_is_still_allowed(config_home: Path) -> None:
    """The must-not-fire half. Without it the test above passes against a
    sentinel that refuses everything, which would be a broken op and a green
    suite."""
    sentinel.record(op="youtube_comment", video_id="vid1", comment_id="c1",
                    url="u", verification="verified")
    verdict, _ = sentinel.check("vid2")
    assert verdict == "ok"


def test_the_hourly_cap_refuses_the_sixth_write(config_home: Path) -> None:
    now = time.time()
    for i in range(sentinel.MAX_WRITES_PER_HOUR):
        sentinel.record(op="youtube_comment", video_id=f"v{i}",
                        comment_id=f"c{i}", url="u", verification="verified")
    verdict, reason = sentinel.check("fresh-video", now=now)
    assert verdict == "refuse"
    assert str(sentinel.MAX_WRITES_PER_HOUR) in reason


def test_the_hourly_cap_forgets_writes_older_than_an_hour(config_home: Path) -> None:
    """The must-not-fire half of the cap: it is a rate, not a lifetime total."""
    for i in range(sentinel.MAX_WRITES_PER_HOUR):
        sentinel.record(op="youtube_comment", video_id=f"v{i}",
                        comment_id=f"c{i}", url="u", verification="verified")
    verdict, _ = sentinel.check("fresh-video", now=time.time() + 3601)
    assert verdict == "ok"


def test_a_corrupt_log_is_cannot_tell_and_never_ok(config_home: Path) -> None:
    """The defect class this repository keeps filing. An unreadable log is
    not an empty one, and must not render as a clean result."""
    (config_home / "sent.jsonl").write_text("{not json\n", encoding="utf-8")
    verdict, reason = sentinel.check("vid1")
    assert verdict == "cannot-tell"
    assert verdict != "ok"
    assert "UNKNOWN" in reason


def test_the_log_is_written_0600(config_home: Path) -> None:
    """Where the bits mean anything. Gated on a capability probe rather than
    a platform name -- Windows reports 0o666 for every file and `os.chmod`
    cannot change it, so asserting 0o600 there tests the OS, not this code.
    """
    if not _publish_safety._mode_bits_are_enforced():
        pytest.skip("this filesystem does not enforce POSIX mode bits")
    sentinel.record(op="youtube_comment", video_id="v", comment_id="c",
                    url="u", verification="verified")
    mode = os.stat(config_home / "sent.jsonl").st_mode & 0o777
    assert mode == 0o600, oct(mode)


# --- argument parsing ----------------------------------------------------

def test_parse_args_splits_video_and_body() -> None:
    vid, body, force, force_dup = comment_op.parse_args("dQw4w9WgXcQ|nice work")
    assert (vid, body, force, force_dup) == ("dQw4w9WgXcQ", "nice work", False, False)


def test_parse_args_accepts_a_url_for_the_video() -> None:
    vid, _, _, _ = comment_op.parse_args(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ|hi")
    assert vid == "dQw4w9WgXcQ"


def test_parse_args_keeps_a_pipe_inside_the_body() -> None:
    """A body is prose and prose contains pipes. `split("|", 2)` would drop
    everything after the second one, silently publishing a truncated comment.
    """
    _, body, force, _ = comment_op.parse_args("vid|before | after")
    assert body == "before | after"
    assert force is False


def test_parse_args_reads_force_from_the_last_field() -> None:
    _, body, force, _ = comment_op.parse_args("vid|before | after|force")
    assert force is True
    assert body == "before | after"


def test_parse_args_triple_colon_separator_avoids_the_pipe_ambiguity() -> None:
    """':::' is not a character an operator would type into ordinary prose,
    so a body whose own text happens to contain '|force' as a trailing
    segment survives intact under it -- the exact string #2600 reports
    (`vid|may the|force` silently drops "force" from the body and forges
    the confirmation) is unambiguous once ':::' picks the field, because
    the whole thing is then a single body field with no ':::' in it at
    all."""
    vid, body, force, force_dup = comment_op.parse_args("vid:::may the|force")
    assert vid == "vid"
    assert body == "may the|force"
    assert (force, force_dup) == (False, False)


def test_parse_args_triple_colon_separator_still_reads_an_explicit_flag() -> None:
    _, body, force, force_dup = comment_op.parse_args(
        "vid:::may the force:::force")
    assert body == "may the force"
    assert (force, force_dup) == (True, False)


def test_parse_args_triple_colon_separator_reads_both_flags_either_order() -> None:
    _, body, force, force_dup = comment_op.parse_args(
        "vid:::hello:::force-dup:::force")
    assert body == "hello"
    assert (force, force_dup) == (True, True)


def test_parse_args_pipe_mode_is_unchanged_by_the_triple_colon_addition() -> None:
    """Documents the limitation ':::' exists to route around rather than
    remove: default '|' parsing still cannot tell a body's own trailing
    '|force' segment from the flag, because changing that default would
    silently reinterpret every already-shipped '|force' call site (#2600's
    fix is the new ':::' field, not a change to '|' semantics)."""
    _, body, force, _ = comment_op.parse_args("vid|may the|force")
    assert body == "may the"
    assert force is True


def test_parse_args_rejects_an_empty_body() -> None:
    with pytest.raises(SystemExit) as e:
        comment_op.parse_args("vid|   ")
    assert e.value.code == 2


def test_parse_args_rejects_an_over_long_body() -> None:
    with pytest.raises(SystemExit) as e:
        comment_op.parse_args("vid|" + "x" * (comment_op.MAX_LEN + 1))
    assert e.value.code == 2


# --- the read-back: three verdicts ---------------------------------------

def _stub_authorized(monkeypatch: pytest.MonkeyPatch, result) -> None:
    def fake(endpoint, token, params, **kw):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(comment_op, "authorized", fake)


def test_verify_confirms_matching_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_authorized(monkeypatch, {"items": [{"snippet": {
        "topLevelComment": {"snippet": {"textOriginal": "hello"}}}}]})
    verdict, detail = comment_op.verify("t1", "tok", "hello")
    assert verdict == "verified"
    assert "5 characters" in detail


def test_verify_reports_a_mismatch_rather_than_a_pass(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_authorized(monkeypatch, {"items": [{"snippet": {
        "topLevelComment": {"snippet": {"textOriginal": "something else"}}}}]})
    verdict, _ = comment_op.verify("t1", "tok", "hello")
    assert verdict == "MISMATCH"


def test_verify_says_could_not_verify_when_the_read_back_fails(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a pass, and not a failure of the write either -- the comment is
    published. The third state exists so those two cannot render alike."""
    _stub_authorized(monkeypatch, comment_op.YouTubeAPIError("commentThreads", "503"))
    verdict, detail = comment_op.verify("t1", "tok", "hello")
    assert verdict == "could-not-verify"
    assert "503" in detail


def test_verify_treats_an_empty_result_as_unverified(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """An insert that returns an id and a list that returns nothing is the
    shape of a comment held for review. It is not a confirmation."""
    _stub_authorized(monkeypatch, {"items": []})
    verdict, _ = comment_op.verify("t1", "tok", "hello")
    assert verdict == "could-not-verify"


def test_could_not_verify_detail_has_no_raw_newline(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The could-not-verify arm must flatten/escape the error body the same
    way the MISMATCH arm already does two lines below it (trap.d/
    227.oauth-error-body-unescaped-in-receipt.md) -- a newline embedded in an
    HTTP error body must not put the remainder at column 0 of a receipt the
    calling agent parses."""
    _stub_authorized(monkeypatch, comment_op.YouTubeAPIError(
        "commentThreads", "503\nSecond line pretending to be a new field"))
    verdict, detail = comment_op.verify("t1", "tok", "hello")
    assert verdict == "could-not-verify"
    assert "\n" not in detail, f"raw newline leaked into the receipt: {detail!r}"
    assert "503" in detail


# --- OAuth ---------------------------------------------------------------

def test_authorize_reports_a_loopback_port_race_as_oautherror(
        config_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_free_loopback_port` frees the socket before `HTTPServer` re-binds
    it, and another local process can take the port in that window --
    `HTTPServer(...)` then raises a bare OSError. `auth.py:main` only catches
    `OAuthError` (trap.d/227.oauth-loopback-port-race-raw-traceback.md), so
    an uncaught OSError here would surface as a raw traceback instead of the
    sentence-naming-the-next-command contract this module's docstring
    promises for every failure path."""
    (config_home / "client_secret.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "s"}}),
        encoding="utf-8")
    (config_home / "client_secret.json").chmod(0o600)

    def boom(*_a, **_k):
        raise OSError(48, "Address already in use")
    monkeypatch.setattr(oauth.http.server, "HTTPServer", boom)

    with pytest.raises(oauth.OAuthError) as e:
        oauth.authorize(open_browser=False)
    assert "youtube_auth" in str(e.value)


def test_authorize_does_not_blame_the_port_race_for_an_unrelated_oserror(
        config_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The must-not-misreport half. `HTTPServer(...)` can raise `OSError` for
    reasons that have nothing to do with the free-port race -- file
    descriptor exhaustion, a sandbox denial -- and asserting the race
    narrative unconditionally would be a false claim about the cause,
    exactly the misreport class this module otherwise exists to avoid. The
    real `OSError` must still reach the operator; the port-race sentence
    must not.
    """
    (config_home / "client_secret.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "s"}}),
        encoding="utf-8")
    (config_home / "client_secret.json").chmod(0o600)

    def boom(*_a, **_k):
        raise OSError(24, "Too many open files")
    monkeypatch.setattr(oauth.http.server, "HTTPServer", boom)

    with pytest.raises(oauth.OAuthError) as e:
        oauth.authorize(open_browser=False)
    message = str(e.value)
    assert "Too many open files" in message
    assert "another local process took the port" not in message.lower(), (
        "a non-EADDRINUSE OSError must not be narrated as the port race: "
        f"{message!r}")


def test_an_absent_token_names_youtube_auth(config_home: Path) -> None:
    """A write op must never open a browser: see _oauth's docstring."""
    with pytest.raises(oauth.OAuthError) as e:
        oauth.get_access_token()
    assert "youtube_auth" in str(e.value)


def test_a_live_token_is_returned_without_a_refresh(
        config_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    oauth._write_token_file({
        "client_id": "cid", "refresh_token": "r", "access_token": "live",
        "expires_at": time.time() + 3600, "scope": oauth.SCOPE})

    def explode(*_a, **_k):
        raise AssertionError("refreshed a token that was still valid")
    monkeypatch.setattr(oauth, "_post_form", explode)
    assert oauth.get_access_token() == "live"


def test_an_expired_token_is_refreshed(
        config_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    oauth._write_token_file({
        "client_id": "cid", "refresh_token": "r", "access_token": "stale",
        "expires_at": time.time() - 10, "scope": oauth.SCOPE})
    (config_home / "client_secret.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "s"}}),
        encoding="utf-8")
    (config_home / "client_secret.json").chmod(0o600)
    monkeypatch.setattr(oauth, "_post_form",
                        lambda *_a, **_k: {"access_token": "fresh", "expires_in": 3600})
    assert oauth.get_access_token() == "fresh"
    stored = json.loads((config_home / "oauth_token.json").read_text(encoding="utf-8"))
    assert stored["access_token"] == "fresh"
    assert stored["refresh_token"] == "r", "the refresh token must survive a refresh"


def test_the_token_file_is_0600_from_creation(config_home: Path) -> None:
    """Not chmod-ed after the write: on a shared machine the gap between
    creating a world-readable file and tightening it is the vulnerability.

    Gated on the probe, like the log above. Where the bits are not enforced
    the mode is not a claim about anything and the credential is protected
    by the profile ACL instead, which this tool cannot read.
    """
    if not _publish_safety._mode_bits_are_enforced():
        pytest.skip("this filesystem does not enforce POSIX mode bits")
    oauth._write_token_file({"refresh_token": "r"})
    mode = os.stat(config_home / "oauth_token.json").st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_a_corrupt_token_cache_is_an_error_not_an_absence(
        config_home: Path) -> None:
    (config_home / "oauth_token.json").write_text("{nope", encoding="utf-8")
    (config_home / "oauth_token.json").chmod(0o600)
    with pytest.raises(oauth.OAuthError):
        oauth.get_access_token()


# --- youtube_auth:status -------------------------------------------------

def test_status_reports_not_authorised_when_nothing_is_cached(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    assert auth_op._status() == 1
    assert "NOT AUTHORISED" in capsys.readouterr().out


def test_status_reports_authorised_on_a_good_cache(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    oauth._write_token_file({
        "client_id": "cid", "refresh_token": "r", "access_token": "a",
        "expires_at": time.time() + 3600, "scope": oauth.SCOPE})
    assert auth_op._status() == 0
    assert "AUTHORISED for write ops" in capsys.readouterr().out


def test_status_says_cannot_tell_on_an_unreadable_cache(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """Three states here too. A cache that will not parse is not the same
    claim as a machine that was never authorised, and the remedies differ."""
    oauth.TOKEN_PATH.write_text("{nope", encoding="utf-8")
    oauth.TOKEN_PATH.chmod(0o600)
    assert auth_op._status() == 1
    out = capsys.readouterr().out
    assert "CANNOT TELL" in out
    assert "NOT AUTHORISED" not in out


def test_status_flags_a_cache_missing_the_write_scope(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """A read-only grant looks authorised and 403s on the first write."""
    oauth._write_token_file({
        "client_id": "cid", "refresh_token": "r", "access_token": "a",
        "expires_at": time.time() + 3600,
        "scope": "https://www.googleapis.com/auth/youtube.readonly"})
    assert auth_op._status() == 1
    assert "not in the recorded scope" in capsys.readouterr().out


# --- registration --------------------------------------------------------

def test_both_write_ops_are_declared_and_point_at_a_real_file() -> None:
    root = Path(__file__).resolve().parent.parent
    decl = json.loads((root / "presets" / "youtube.json").read_text(encoding="utf-8"))
    for name, script in (("youtube_auth", "auth.py"), ("youtube_comment", "comment.py")):
        assert name in decl["ops"], f"{name} is not declared"
        op = decl["ops"][name]
        assert op["safety"] == "acts", (
            f"{name} publishes under the operator's account; declaring it "
            "read-only would let it be called blind")
        assert (root / "presets" / "youtube" / script).is_file()
        assert script in op["cmd"]


# --- main(), wired end to end --------------------------------------------

#: `readback=ECHO` makes the fake API return whatever was POSTed, which is
#: what a healthy YouTube does. Spelling the expected text out in the stub
#: instead means the test also encodes how `apply_disclosure` joins its
#: marker -- so a change to that spacing turns this into a MISMATCH about
#: nothing, which is exactly what happened on the first run of this file.
ECHO = object()


def _wire(monkeypatch: pytest.MonkeyPatch, *, insert=None, readback=None):
    """Stub the two network seams comment.main touches."""
    calls = []

    def fake_authorized(endpoint, token, params, *, method="GET", body=None, **kw):
        calls.append({"endpoint": endpoint, "method": method, "body": body})
        if method == "POST":
            if isinstance(insert, Exception):
                raise insert
            return insert
        if isinstance(readback, Exception):
            raise readback
        if readback is ECHO:
            posted = [c for c in calls if c["method"] == "POST"]
            text = (posted[-1]["body"]["snippet"]["topLevelComment"]
                    ["snippet"]["textOriginal"])
            return _readback(text)
        return readback

    monkeypatch.setattr(comment_op, "authorized", fake_authorized)
    monkeypatch.setattr(comment_op, "get_access_token", lambda: "tok")
    return calls


_INSERT_OK = {"id": "thread1", "snippet": {"topLevelComment": {"id": "Ugw123"}}}


def _readback(text: str) -> dict:
    return {"items": [{"snippet": {
        "topLevelComment": {"snippet": {"textOriginal": text}}}}]}


def test_main_publishes_verifies_and_records(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    calls = _wire(monkeypatch, insert=_INSERT_OK, readback=ECHO)
    # `tests/conftest.py` sets SUPERTOOL_NO_PUBLISH_DISCLOSURE=1 for the whole
    # suite, so the marker assertion below is vacuous without this line -- it
    # was, on the first run, and passed the moment the stub stopped echoing.
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", raising=False)
    comment_op.main("vid9|nice work|force")
    out = capsys.readouterr().out

    assert "youtube_comment OK video=vid9 comment_id=Ugw123" in out
    assert "read-back: verified" in out
    assert "NOTE:" in out and "not visibility" in out

    post = [c for c in calls if c["method"] == "POST"]
    assert len(post) == 1, "exactly one comment per call -- #227 forbids batching"
    sent = post[0]["body"]["snippet"]["topLevelComment"]["snippet"]["textOriginal"]
    assert sent.endswith("[AI-generated]"), (
        "the authorship marker must reach the API, not just the preview")

    logged = [json.loads(line) for line
              in (config_home / "sent.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(logged) == 1
    assert logged[0]["video_id"] == "vid9"
    assert logged[0]["verification"] == "verified"


def test_main_refuses_a_second_comment_on_the_same_video(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    _wire(monkeypatch, insert=_INSERT_OK, readback=_readback("x"))
    sentinel.record(op="youtube_comment", video_id="vid9", comment_id="c",
                    url="u", verification="verified")
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_CONFIRM", "1")
    with pytest.raises(SystemExit) as e:
        comment_op.main("vid9|another one")
    assert e.value.code == 1
    assert "ABORT" in capsys.readouterr().err


def test_main_refuses_when_the_sentinel_cannot_answer(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """`cannot-tell` is a refusal, and says so in different words than a
    duplicate does -- the operator's next move differs."""
    calls = _wire(monkeypatch, insert=_INSERT_OK, readback=_readback("x"))
    (config_home / "sent.jsonl").write_text("{broken\n", encoding="utf-8")
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_CONFIRM", "1")
    with pytest.raises(SystemExit) as e:
        comment_op.main("vid9|hello")
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "cannot tell" in err
    assert not calls, "nothing may be published while the duplicate state is unknown"


def test_main_still_reports_a_failed_read_back_as_published(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """The write happened. The op must say so AND say the check did not, in
    one output, rather than picking one of the two."""
    _wire(monkeypatch, insert=_INSERT_OK,
          readback=comment_op.YouTubeAPIError("commentThreads", "503 upstream"))
    comment_op.main("vid9|hello|force")
    out = capsys.readouterr().out
    assert "youtube_comment OK" in out
    assert "read-back: could-not-verify" in out
    logged = json.loads((config_home / "sent.jsonl").read_text(encoding="utf-8"))
    assert logged["verification"] == "could-not-verify", (
        "the log must record that this one was never confirmed")


def test_main_requires_confirmation_without_force(
        config_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The must-fire half of require_confirm: every other main() test passes
    `|force` or sets the env opt-out, so without this one the guard could be
    absent and the file would still be green."""
    calls = _wire(monkeypatch, insert=_INSERT_OK, readback=_readback("x"))
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_CONFIRM", raising=False)
    monkeypatch.chdir(config_home)
    with pytest.raises(SystemExit) as e:
        comment_op.main("vid9|hello")
    assert e.value.code == 2
    assert not calls, "nothing may be published before confirmation"


def test_main_honours_the_disclosure_opt_out(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """The other direction. The marker is a default, not a policy this op
    enforces over the operator -- `no_publish_disclosure` has to actually
    reach the published text, and the output has to say it was suppressed
    rather than silently shipping an unmarked comment.
    """
    calls = _wire(monkeypatch, insert=_INSERT_OK, readback=ECHO)
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", "1")
    comment_op.main("vid9|nice work|force")
    sent = ([c for c in calls if c["method"] == "POST"][0]
            ["body"]["snippet"]["topLevelComment"]["snippet"]["textOriginal"])
    assert sent == "nice work"
    assert "(disclosure: suppressed)" in capsys.readouterr().out


# --- the Windows arm, reproduced on every platform -----------------------

@pytest.fixture()
def bits_not_enforced(monkeypatch: pytest.MonkeyPatch):
    """Make every mode check behave as it does on Windows.

    Forced rather than waited for. CI found this on four windows legs and
    nowhere else: `check_token_file_mode` refused any file with `mode &
    0o077`, and Windows reports `0o666` for every file, so it exited 2 on
    every credential it was ever handed. Eight tests in this file died of
    it. A regression test that only one platform in twelve can run is a
    regression test that finds the next one the same expensive way.
    """
    for mod in _module_aliases("_publish_safety", _publish_safety):
        monkeypatch.setattr(mod, "_mode_bits_are_enforced",
                             lambda *a, **k: False)


def test_a_token_file_is_usable_where_mode_bits_are_not_enforced(
        config_home: Path, bits_not_enforced, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """The write path must WORK there, not merely fail politely."""
    oauth._write_token_file({
        "client_id": "cid", "refresh_token": "r", "access_token": "live",
        "expires_at": time.time() + 3600, "scope": oauth.SCOPE})
    os.chmod(oauth.TOKEN_PATH, 0o666)

    assert oauth.get_access_token() == "live"
    assert "could not verify" in capsys.readouterr().err, (
        "an unverifiable credential must say so -- passing silently is the "
        "absence-read-as-clean defect, and refusing is what broke Windows")


def test_the_client_secret_is_readable_where_mode_bits_are_not_enforced(
        config_home: Path, bits_not_enforced) -> None:
    """The same arm on the other file `_oauth` mode-checks. Two call sites,
    and a fix that reached only one would have moved the exit 2 rather than
    removed it."""
    (config_home / "client_secret.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "s"}}),
        encoding="utf-8")
    os.chmod(config_home / "client_secret.json", 0o666)
    assert oauth.load_client_secret() == ("cid", "s")


def test_a_loose_credential_is_still_refused_where_bits_are_enforced(
        config_home: Path) -> None:
    """The must-fire half. Without it the two tests above are satisfied by a
    check that never refuses anything, which is the opposite defect and the
    one the guard exists for.
    """
    if not _publish_safety._mode_bits_are_enforced():
        pytest.skip("this filesystem does not enforce POSIX mode bits")
    oauth._write_token_file({
        "client_id": "cid", "refresh_token": "r", "access_token": "live",
        "expires_at": time.time() + 3600, "scope": oauth.SCOPE})
    os.chmod(oauth.TOKEN_PATH, 0o644)
    with pytest.raises(SystemExit) as e:
        oauth.get_access_token()
    assert e.value.code == 2


# --- |force confirms, |force-dup overrides the sentinel (review of #2543) --

def test_parse_args_reads_the_two_override_tokens_separately() -> None:
    """`force` and `force-dup` are different permissions and are parsed as
    two flags, in either order, from the trailing fields."""
    _, body, force, force_dup = comment_op.parse_args(
        "vid|before | after|force|force-dup")
    assert body == "before | after"
    assert (force, force_dup) == (True, True)
    _, _, force, force_dup = comment_op.parse_args("vid|hi|force-dup|force")
    assert (force, force_dup) == (True, True)


def test_parse_args_keeps_a_lone_force_out_of_the_sentinel_flag() -> None:
    _, _, force, force_dup = comment_op.parse_args("vid|hi|force")
    assert (force, force_dup) == (True, False)


def test_force_alone_does_not_bypass_the_duplicate_guard(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """The finding this section exists for. `|force` is the token the usage
    string tells an operator to pass to confirm a publish, so if it also
    disarmed the sentinel the duplicate guard would be off on every normal
    invocation -- and this preset has no delete op.
    """
    calls = _wire(monkeypatch, insert=_INSERT_OK, readback=_readback("x"))
    sentinel.record(op="youtube_comment", video_id="vid9", comment_id="c",
                    url="u", verification="verified")
    with pytest.raises(SystemExit) as e:
        comment_op.main("vid9|another one|force")
    assert e.value.code == 1
    assert "force-dup" in capsys.readouterr().err
    assert not calls, "a confirmed publish is not a licence to double-post"


def test_force_alone_does_not_bypass_an_unreadable_sentinel(
        config_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire(monkeypatch, insert=_INSERT_OK, readback=_readback("x"))
    (config_home / "sent.jsonl").write_text("{broken\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        comment_op.main("vid9|hello|force")
    assert e.value.code == 1
    assert not calls


def test_force_dup_does_bypass_the_duplicate_guard(
        config_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The must-fire half. Without this the two above would also pass if
    `force-dup` overrode nothing at all."""
    calls = _wire(monkeypatch, insert=_INSERT_OK, readback=ECHO)
    sentinel.record(op="youtube_comment", video_id="vid9", comment_id="c",
                    url="u", verification="verified")
    comment_op.main("vid9|another one|force|force-dup")
    assert [c for c in calls if c["method"] == "POST"], (
        "force-dup is the documented override and has to actually override")


def test_force_dup_alone_does_not_confirm_the_publish(
        config_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction of the split: overriding the sentinel is not
    consent to publish either."""
    calls = _wire(monkeypatch, insert=_INSERT_OK, readback=_readback("x"))
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_CONFIRM", raising=False)
    monkeypatch.chdir(config_home)
    with pytest.raises(SystemExit) as e:
        comment_op.main("vid9|hello|force-dup")
    assert e.value.code == 2
    assert not calls


# --- #2599: the three remaining raw error-body interpolations ------------

def test_main_escapes_a_newline_in_the_oauth_error_when_getting_the_token(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """Same shape as verify()'s could-not-verify arm
    (trap.d/227.oauth-error-body-unescaped-in-receipt.md): an OAuth error
    body is Google's and can carry a newline, which must not put the
    remainder at column 0 of this op's own stderr."""
    def boom():
        raise comment_op.OAuthError(
            "token refresh failed\nSecond line pretending to be a new field")
    monkeypatch.setattr(comment_op, "get_access_token", boom)
    with pytest.raises(SystemExit) as e:
        comment_op.main("vid9|hello|force")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "\n" not in err.rstrip("\n"), f"raw newline leaked into stderr: {err!r}"
    assert "token refresh failed" in err


def test_main_escapes_a_newline_in_the_api_error_from_the_insert(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """Same shape, the other exception arm: the insert's own YouTubeAPIError
    carries _yt._format_oauth_http_error's raw body[:300]."""
    _wire(monkeypatch, insert=comment_op.YouTubeAPIError(
        "commentThreads", "400\nSecond line pretending to be a new field"),
        readback=_readback("x"))
    with pytest.raises(SystemExit) as e:
        comment_op.main("vid9|hello|force")
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "\n" not in err.rstrip("\n"), f"raw newline leaked into stderr: {err!r}"
    assert "400" in err


def test_auth_status_escapes_a_newline_in_the_cached_scope(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """auth.py:_status prints the token cache's own 'scope' field verbatim --
    server-supplied, but the same misreports-not-forges reasoning as the
    OAuth error bodies applies, and a newline embedded in it must not split
    the receipt either."""
    oauth._write_token_file({
        "client_id": "cid", "refresh_token": "r", "access_token": "live",
        "expires_at": time.time() + 3600,
        "scope": "openid\nSecond line pretending to be a new field"})
    assert auth_op._status() == 1
    out = capsys.readouterr().out
    scope_line = next(line for line in out.splitlines() if line.startswith("scope:"))
    assert "Second line pretending to be a new field" in scope_line, (
        "the scope text must still be visible, just not on its own line")


def test_auth_main_escapes_a_newline_in_the_oauth_error(
        config_home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    def boom(*_a, **_k):
        # auth_op imports OAuthError off its own sys.path entry
        # (_module_aliases's issue) -- raise the class it actually catches.
        raise auth_op.OAuthError(
            "authorize failed\nSecond line pretending to be a new field")
    monkeypatch.setattr(auth_op, "authorize", boom)
    with pytest.raises(SystemExit) as e:
        auth_op.main("")
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "\n" not in err.rstrip("\n"), f"raw newline leaked into stderr: {err!r}"
    assert "authorize failed" in err
