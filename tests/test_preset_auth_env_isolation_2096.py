"""#2096 -- an unstubbed test reaching a preset auth reader must not resolve
the maintainer's own on-disk credential (`~/.config/<preset>/<name>` or a
`.{preset}-...` file in cwd). Without a global guard, whether a test's
"does not raise" or "still delivers the event" assertion covers anything
depends on whose machine ran it -- a green local run that is not evidence
the CI-only code path (no token configured) was ever exercised.

Reproduction, not simulation: this repo's own maintainer machine has all
four presets' real credentials under ~/.config, exactly as #2096 describes.
The positive control below proves the leak is real *right now* on such a
machine, without printing or asserting on the secret value itself -- only
that a value came back. The guard test proves the fix: the same call, made
the way an ordinary unstubbed test would make it (no monkeypatching at all
in the test body), must come back empty once the autouse isolation in
conftest.py is in place.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import presets.bluesky._auth as bsky_auth
import presets.devto._auth as devto_auth
import presets.hashnode._auth as hn_auth
import presets.slack._auth as slack_auth

REAL_HOME = Path(os.path.expanduser("~"))

#: Whether *this* machine actually has the credential files #2096 describes.
#: The positive control is a reproduction only when this is true; on a
#: machine without them it would trivially "pass" for the wrong reason, so
#: it is skipped rather than silently asserting nothing.
_HAS_REAL_SLACK_TOKEN = (REAL_HOME / ".config" / "slack" / "bot_token").is_file()


@pytest.mark.skipif(
    not _HAS_REAL_SLACK_TOKEN,
    reason="no ~/.config/slack/bot_token on this machine -- nothing to leak",
)
def test_positive_control_the_leak_is_real_on_a_machine_with_a_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the vulnerability class, isolated from the conftest fixture
    under test: point HOME back at the *real* home and unset the env var --
    the same two conditions #2096's own repro names -- and the reader must
    still resolve a token from disk. If this ever starts returning None, the
    class this issue describes no longer exists and the guard below is
    testing nothing."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(REAL_HOME))
    monkeypatch.setenv("USERPROFILE", str(REAL_HOME))

    # Reduced to a bare bool before the assertion: pytest's assertion
    # rewriting prints the compared *values* on failure, and the value here
    # is a real credential -- asserting on it directly would put a live
    # token into the test report and the terminal. `resolved` never touches
    # the secret itself, only whether one came back.
    resolved = bool(slack_auth.get_bot_token_or_none())

    assert resolved


def test_the_autouse_fixture_actually_redirected_home_away_from_the_real_one() -> None:
    """The four guard tests below are only meaningful *because* of this one.

    On a machine with no real credential configured -- every CI runner, and
    most contributors' machines -- `_read_first` already returns `None` with
    or without `conftest.py`'s `_no_real_preset_credentials` fixture, because
    there is nothing on disk to fall through to either way. A guard asserting
    only the *usual consequence* (`get_bot_token_or_none() is None`) would
    still pass there if the fixture were deleted tomorrow -- it is only a
    real regression test on the one machine, this one, that happens to carry
    a live credential, and that machine's own positive-control test is itself
    `skipif`-gated on the same condition.

    This test asserts the *mechanism* instead, which holds everywhere: HOME
    really moved away from the machine's genuine home directory, to a
    directory with none of the four presets' `.config/<preset>/` subtrees.
    Delete the autouse fixture and this fails on every machine, CI included
    -- `current_home` reverts to `REAL_HOME` and the equality check catches
    it directly, with no dependency on any file happening to exist there.
    """
    current_home = os.environ.get("HOME")
    assert current_home is not None
    assert current_home != str(REAL_HOME)
    fake_home = Path(current_home)
    for preset in ("slack", "bluesky", "devto", "hashnode"):
        assert not (fake_home / ".config" / preset).exists()


def test_slack_auth_reader_is_not_reachable_without_any_stub_in_the_test_itself() -> None:
    """The guard: an ordinary test that never mentions Slack, never sets
    SLACK_BOT_TOKEN and never touches HOME must still get `None` back from
    `get_bot_token_or_none()` -- because the autouse isolation fixture in
    conftest.py has already cleared the env var and pointed HOME somewhere
    with no `slack/bot_token` file, on every machine, maintainer's included.

    This is the test that was red before the fix on a machine carrying a
    real token: with no isolation fixture, this call falls straight through
    to the real ~/.config/slack/bot_token and returns it. On a machine with
    no real token configured this assertion alone would pass regardless of
    the fixture -- see
    `test_the_autouse_fixture_actually_redirected_home_away_from_the_real_one`
    above for the check that is not vacuous there.
    """
    assert os.environ.get("SLACK_BOT_TOKEN") is None
    # Computed on its own line, then asserted as a bare name -- see the
    # positive control's comment above. `assert bool(f())` still lets
    # pytest's assertion rewriter decompose the call and print its *return
    # value* on failure; only a plain `assert name` with the call already
    # resolved on a prior statement keeps a real credential out of the
    # failure diff.
    resolved = bool(slack_auth.get_bot_token_or_none())
    assert resolved is False


def test_bluesky_auth_reader_is_not_reachable_without_any_stub_in_the_test_itself() -> None:
    assert os.environ.get("BLUESKY_HANDLE") is None
    assert os.environ.get("BLUESKY_APP_PASSWORD") is None
    handle_resolved = bool(bsky_auth._read_first(
        "BLUESKY_HANDLE", "~/.config/bluesky/handle", ".bluesky-handle"))
    password_resolved = bool(bsky_auth._read_first(
        "BLUESKY_APP_PASSWORD", "~/.config/bluesky/app_password",
        ".bluesky-app-password"))
    assert handle_resolved is False
    assert password_resolved is False


def test_devto_auth_reader_is_not_reachable_without_any_stub_in_the_test_itself() -> None:
    assert os.environ.get("DEVTO_API_KEY") is None
    resolved = bool(devto_auth._read_first(
        "DEVTO_API_KEY", "~/.config/devto/token", ".devto-token"))
    assert resolved is False


def test_hashnode_auth_reader_is_not_reachable_without_any_stub_in_the_test_itself() -> None:
    assert os.environ.get("HASHNODE_TOKEN") is None
    assert os.environ.get("HASHNODE_PUBLICATION_ID") is None
    token_resolved = bool(hn_auth._read_first(
        "HASHNODE_TOKEN", "~/.config/hashnode/token", ".hashnode-token"))
    pub_id_resolved = bool(hn_auth._read_first(
        "HASHNODE_PUBLICATION_ID", "~/.config/hashnode/publication_id",
        ".hashnode-publication-id"))
    assert token_resolved is False
    assert pub_id_resolved is False


def test_a_stray_credential_file_in_the_repo_cwd_is_not_silently_picked_up() -> None:
    """The third arm from #2096: `.slack-bot-token` and its siblings resolve
    against the *current working directory*, so a credential file dropped in
    a worktree root would be picked up by any run from there. This asserts
    none of the six known filenames exist at the process cwd during a test
    run -- loud, rather than a silent read, if one is ever planted."""
    stray_names = (
        ".slack-bot-token",
        ".bluesky-handle",
        ".bluesky-app-password",
        ".devto-token",
        ".hashnode-token",
        ".hashnode-publication-id",
    )
    cwd = Path(os.getcwd())
    present = [name for name in stray_names if (cwd / name).is_file()]
    assert present == [], (
        f"stray preset credential file(s) in cwd, would be read by any "
        f"unstubbed test run from here: {present}"
    )
