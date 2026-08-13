"""`channel:health` said FORWARDING with nobody subscribed to the channel (#1543).

The producer half was healthy in every particular and no event could ever have
arrived. A consumer held the socket, published fresh counters and verified as
its own socket-holder; the session that spawned it had refused the channel tag
at startup, so the consumer read events and handed them to a transport nothing
was listening on.

**Subscription is partly observable from outside the session, and this file
pins which part.** Two facts are readable: the process that spawned the
socket-holder, and whether the channel tag in that process's argv names an MCP
server the harness has *configured* — `claude mcp get NAME`, the same question
`bin/supertool-workspace` asks before it registers the consumer. Neither is a
statement about what the session did with the notification, which stays
unobservable and stays in `CEILING`.

So three answers, never two:

* the spawning session carries no channel tag, or was told to subscribe to a
  server the harness has never heard of, or has exited — **definite negative**,
  `BOUND, NOT SUBSCRIBED`, and the events are read and discarded;
* the tag names a configured server — `FORWARDING`, unchanged;
* anything that could not be asked — no `ps`, no `claude` on PATH, an argv that
  does not parse — **`CANNOT DETERMINE` with the reason**. Never `FORWARDING`:
  a verdict that reads as delivery is the whole defect.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
import radar  # noqa: E402
import transport  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

CONSUMER_ARGV = "bun /Users/x/notifiers/claude-channel/channel.ts"
SESSION_PID = 4242
TAGGED = ("claude /opensource-manager "
          "--dangerously-load-development-channels server:supertool-channel")
UNTAGGED = "claude /opensource-manager"


def _sock_path() -> str:
    """System temp dir, not `tmp_path`: macOS caps an AF_UNIX path near 104
    bytes and pytest's is long enough to turn every test here into a skip."""
    return str(Path(tempfile.gettempdir())
               / f"st1543-{os.getpid()}-{time.time_ns()}.sock")


def _can_bind_af_unix() -> bool:
    if not hasattr(socket, "AF_UNIX"):
        return False
    probe = _sock_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(probe)
        return True
    except OSError:
        return False
    finally:
        sock.close()
        try:
            os.unlink(probe)
        except OSError:
            pass


@pytest.fixture()
def forwarding(monkeypatch):
    """The exact state #1543 was filed from, minus the subscription question.

    A live socket held by this process, and a health file this process wrote
    naming itself — every check before the new one passes, so the arm under
    test is the one that used to print `FORWARDING` unconditionally.
    """
    if not _can_bind_af_unix():
        pytest.skip("this platform cannot bind an AF_UNIX socket")
    path = _sock_path()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    Path(f"{path}{channel.HEALTH_SUFFIX}").write_text(json.dumps({
        "pid": os.getpid(),
        "started": "2026-08-13T05:26:44Z",
        "updated": now,
        "last_forwarded": now,
        "lines_read": 8,
        "forwarded": 8,
        "dropped": 0,
    }), encoding="utf-8")
    # The holder is this process either way; stubbing it keeps the test on the
    # subscription arm on a platform with no peer credentials (FreeBSD), where
    # the fallback reads the pid out of the health file instead.
    monkeypatch.setattr(channel, "peer_pid", lambda _p: (os.getpid(), ""))
    try:
        yield path
    finally:
        srv.close()
        for leftover in (path, f"{path}{channel.HEALTH_SUFFIX}"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


def _process_table(monkeypatch, session_argv: str, *, session_pid: int = SESSION_PID):
    """`ps` for exactly two processes: this one, and the session that spawned it."""
    table = {
        os.getpid(): (session_pid, CONSUMER_ARGV, ""),
        session_pid: (1, session_argv, ""),
    }

    def fields(pid: int):
        return table.get(pid, (None, "", f"no process {pid}"))

    monkeypatch.setattr(channel, "_ps_fields", fields)


def _configured(monkeypatch, answer, why: str = ""):
    monkeypatch.setattr(channel, "_configured", lambda _name: (answer, why))


# --- the filed incident ------------------------------------------------------

def test_a_tag_naming_no_configured_server_is_not_subscribed(forwarding, monkeypatch):
    """#1543 itself. The server came from `--mcp-config`, so it bound the socket
    and the harness refused the tag — `no MCP server configured with that name`.
    Every producer-side fact was healthy and nothing could ever arrive."""
    _process_table(monkeypatch, TAGGED)
    _configured(monkeypatch, False)
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: BOUND, NOT SUBSCRIBED", report
    assert rc == channel.RC_NOT_SUBSCRIBED
    assert "supertool-channel" in report, report


def test_a_session_with_no_channel_tag_is_not_subscribed(forwarding, monkeypatch):
    """The other half of the same negative: the consumer was spawned by a
    session that never armed a channel, so its notifications go nowhere."""
    _process_table(monkeypatch, UNTAGGED)
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: BOUND, NOT SUBSCRIBED", report
    assert rc == channel.RC_NOT_SUBSCRIBED


def test_a_consumer_whose_session_has_exited_is_not_subscribed(forwarding, monkeypatch):
    """Reparented to init: whatever it is handing events to, no session is
    reading them."""
    monkeypatch.setattr(channel, "_ps_fields",
                        lambda pid: (1, CONSUMER_ARGV, ""))
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: BOUND, NOT SUBSCRIBED", report
    assert rc == channel.RC_NOT_SUBSCRIBED


def test_the_counters_are_still_printed_under_the_new_verdict(forwarding, monkeypatch):
    """They are the producer half and they are still true. An operator
    comparing this consumer against another needs them."""
    _process_table(monkeypatch, TAGGED)
    _configured(monkeypatch, False)
    _, report = channel.health(forwarding)
    assert "8 forwarded" in report, report
    assert channel.CEILING in report, report


# --- what must still be FORWARDING -------------------------------------------

def test_a_configured_tag_is_still_forwarding(forwarding, monkeypatch):
    """The known-good state measured on 2026-08-13: `supertool-workspace`
    registers the consumer at local scope and tags it, and events arrive."""
    _process_table(monkeypatch, TAGGED)
    _configured(monkeypatch, True)
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: FORWARDING", report
    assert rc == channel.RC_FORWARDING
    # The residual is named rather than implied: that the configured server is
    # the one holding this socket is not checked.
    assert "subscribed" in report, report


# --- the third state, which is the point -------------------------------------

def test_an_unanswerable_registry_probe_is_cannot_determine(forwarding, monkeypatch):
    """No `claude` on PATH. The subscription question was asked and not
    answered, which is not the same as answered yes."""
    _process_table(monkeypatch, TAGGED)
    _configured(monkeypatch, None, "`claude mcp get` could not be run (FileNotFoundError)")
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: CANNOT DETERMINE", report
    assert rc == channel.RC_UNKNOWN
    assert "FileNotFoundError" in report, report


def test_an_unreadable_process_table_is_cannot_determine(forwarding, monkeypatch):
    """Windows has no `ps`, and this arm is reached wherever the spawn fails."""
    monkeypatch.setattr(
        channel, "_ps_fields",
        lambda pid: (None, "", "`ps` could not be run (FileNotFoundError)"))
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: CANNOT DETERMINE", report
    assert rc == channel.RC_UNKNOWN
    assert "FileNotFoundError" in report, report


def test_a_spawner_that_is_not_recognisably_a_session_is_cannot_determine(
        forwarding, monkeypatch):
    """A consumer started by hand from a shell, or a harness launched through
    `node .../cli.js`. Claiming the negative here would be a guess about an
    argv this reader does not know how to read."""
    _process_table(monkeypatch, "/bin/zsh -l")
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: CANNOT DETERMINE", report
    assert rc == channel.RC_UNKNOWN
    assert "zsh" in report, report


def test_a_tag_that_could_not_be_looked_up_does_not_bury_a_later_one(
        forwarding, monkeypatch):
    """The flag is variadic and takes several tags. One lookup that settled
    nothing is not a reason to stop asking: a session subscribed through the
    second tag is subscribed, and reporting `CANNOT DETERMINE` off the first
    would be an absence produced by the order of the list."""
    _process_table(
        monkeypatch,
        "claude --dangerously-load-development-channels server:flaky server:good")
    answers = {"flaky": (None, "the lookup failed"), "good": (True, "")}
    monkeypatch.setattr(channel, "_configured", lambda name: answers[name])
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: FORWARDING", report
    assert rc == channel.RC_FORWARDING


def test_every_tag_unresolved_is_still_cannot_determine(forwarding, monkeypatch):
    """And the reason survives: an unasked question must not read as an
    answered one just because the list had more than one entry."""
    _process_table(
        monkeypatch,
        "claude --dangerously-load-development-channels server:a server:b")
    monkeypatch.setattr(channel, "_configured",
                        lambda name: (None, f"{name} lookup failed"))
    rc, report = channel.health(forwarding)
    assert report.splitlines()[0] == "channel: CANNOT DETERMINE", report
    assert rc == channel.RC_UNKNOWN
    assert "lookup failed" in report, report


# --- the parser --------------------------------------------------------------

def test_an_ambiguous_tag_list_declines_rather_than_truncating_a_name():
    """`--dangerously-load-development-channels` is variadic, and a server name
    with a space would arrive as two tokens. Reading the first would produce a
    name the harness has never heard of and a confident false negative."""
    tags, why = channel._channel_tags(
        "claude --dangerously-load-development-channels server:claude.ai Gmail")
    assert tags is None, tags
    assert why


def test_a_tag_after_the_flag_is_read_and_the_next_option_ends_it():
    tags, why = channel._channel_tags(
        "claude --dangerously-load-development-channels server:a --verbose x")
    assert tags == ["a"], why


# --- radar, the second instance ----------------------------------------------

def test_the_delivery_banner_says_when_nothing_is_subscribed(monkeypatch):
    """`radar: delivery — all N accepted by a listener` is the same
    producer-only claim one surface up, and it inherits the same blind spot."""
    monkeypatch.setattr(transport, "delivery_survey",
                        lambda: [("gitlab-mr", "1", transport.EMIT_ACCEPTED)])
    monkeypatch.setattr(transport, "emit_destinations",
                        lambda: [("gitlab-mr", "1", transport.SOCK_PATH)])
    monkeypatch.setattr(
        channel, "subscription_for_socket",
        lambda _p: channel.Subscription(
            channel.SUB_NOT_SUBSCRIBED,
            ["             no session is subscribed to this channel"]))
    text = "\n".join(radar.delivery_banner())
    assert "NOT SUBSCRIBED" in text, text


def test_the_delivery_banner_is_quiet_when_a_session_is_subscribed(monkeypatch):
    """Agreement is not news, and a line printed every tick is one nobody
    reads. The `accepted` head stands on its own."""
    monkeypatch.setattr(transport, "delivery_survey",
                        lambda: [("gitlab-mr", "1", transport.EMIT_ACCEPTED)])
    monkeypatch.setattr(transport, "emit_destinations",
                        lambda: [("gitlab-mr", "1", transport.SOCK_PATH)])
    monkeypatch.setattr(
        channel, "subscription_for_socket",
        lambda _p: channel.Subscription(channel.SUB_SUBSCRIBED, []))
    text = "\n".join(radar.delivery_banner())
    assert "NOT SUBSCRIBED" not in text, text


def test_the_delivery_banner_names_a_subscription_it_could_not_establish(monkeypatch):
    monkeypatch.setattr(transport, "delivery_survey",
                        lambda: [("gitlab-mr", "1", transport.EMIT_ACCEPTED)])
    monkeypatch.setattr(transport, "emit_destinations",
                        lambda: [("gitlab-mr", "1", transport.SOCK_PATH)])
    monkeypatch.setattr(
        channel, "subscription_for_socket",
        lambda _p: channel.Subscription(
            channel.SUB_UNKNOWN,
            ["             `ps` could not be run (FileNotFoundError)"]))
    text = "\n".join(radar.delivery_banner())
    assert "FileNotFoundError" in text, text


# --- documentation -----------------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(1543)
