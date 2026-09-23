"""#2658: `channel:health` reported `FORWARDING` with `0 forwarded` / `last
forwarded never`, in the same session `channel:stranded` reported events being
LOST -- and separately printed a `.refused.json` marker under `refused:` as a
live #2133 collision finding after its own census had already concluded, one
paragraph below, that the marker was this report's own `claude mcp get` probe
residue and not a rival session.

Two defects, one function (`health()`), fixed together because both live in
the same final branch of it:

* `FORWARDING` used to be reachable with `forwarded == 0` and no
  `last_forwarded` -- a consumer that is bound, verified, counting and
  subscribed, but has never moved a single event, rendered under the same
  headline word as one that has. `BOUND, UNPROVEN` (`RC_UNPROVEN`) is the new
  sixth state for exactly that gap.
* the `refused:` line was built from the marker file alone, before
  `subscription()`'s own census (which already knows whether a standing
  `claude-channel` server is configured) had a chance to say the marker was
  residue rather than a finding. `sub.probe_residue` carries that conclusion
  back into `health()`, which now relabels the line instead of printing two
  disagreeing verdicts in one report.

Every "must not fire"/"must relabel" case below is paired with a positive
control on the same fixture, because an assertion that a verdict does not
fire also passes when the whole branch has been deleted.
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
from _changelog_findable import assert_change_is_findable  # noqa: E402

CONSUMER_ARGV = "bun /Users/x/notifiers/claude-channel/channel.ts"
SESSION_PID = 4242
TAGGED = ("claude /oss:tick "
          "--dangerously-load-development-channels server:supertool-channel")


def _sock_path() -> str:
    """System temp dir, not `tmp_path`: macOS caps an AF_UNIX path near 104
    bytes and pytest's is long enough to turn every test here into a skip."""
    return str(Path(tempfile.gettempdir())
               / f"st2658-{os.getpid()}-{time.time_ns()}.sock")


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


def _bound(monkeypatch, *, forwarded: int, last_forwarded: str | None):
    """A live socket held by this process, with a health file this process
    wrote naming itself, and the specific counter pair each test is about."""
    if not _can_bind_af_unix():
        pytest.skip("this platform cannot bind an AF_UNIX socket")
    path = _sock_path()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = {
        "pid": os.getpid(),
        "started": "2026-09-22T12:28:25Z",
        "updated": now,
        "lines_read": 0,
        "forwarded": forwarded,
        "dropped": 0,
    }
    if last_forwarded is not None:
        record["last_forwarded"] = last_forwarded
    Path(f"{path}{channel.HEALTH_SUFFIX}").write_text(
        json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(channel, "peer_pid", lambda _p: (os.getpid(), ""))
    return path, srv


@pytest.fixture()
def never_forwarded(monkeypatch):
    path, srv = _bound(monkeypatch, forwarded=0, last_forwarded=None)
    try:
        yield path
    finally:
        srv.close()
        for leftover in (path, f"{path}{channel.HEALTH_SUFFIX}",
                          f"{path}{channel.REFUSAL_SUFFIX}"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


@pytest.fixture()
def actually_forwarded(monkeypatch):
    path, srv = _bound(monkeypatch, forwarded=8,
                       last_forwarded=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        yield path
    finally:
        srv.close()
        for leftover in (path, f"{path}{channel.HEALTH_SUFFIX}",
                          f"{path}{channel.REFUSAL_SUFFIX}"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


def _process_table(monkeypatch, session_argv: str = TAGGED,
                    *, session_pid: int = SESSION_PID):
    table = {
        os.getpid(): (session_pid, CONSUMER_ARGV, ""),
        session_pid: (1, session_argv, ""),
    }

    def fields(pid: int):
        return table.get(pid, (None, "", f"no process {pid}"))

    monkeypatch.setattr(channel, "_ps_fields", fields)


def _configured(monkeypatch, answer, why: str = ""):
    monkeypatch.setattr(channel, "_configured",
                        lambda _name, _budget=None: (answer, why))


# --- the headline word: BOUND, UNPROVEN --------------------------------------

def test_a_subscribed_channel_that_never_forwarded_is_not_forwarding(
        never_forwarded, monkeypatch):
    """The transcript in #2658: bound, verified, subscribed, `0 forwarded`,
    `last forwarded never`. That combination must not render `FORWARDING`."""
    _process_table(monkeypatch)
    _configured(monkeypatch, True)
    rc, report = channel.health(never_forwarded)
    assert report.splitlines()[0] == "channel: BOUND, UNPROVEN", report
    assert rc == channel.RC_UNPROVEN
    assert "0 forwarded" in report, report
    assert "never" in report, report


def test_control_a_subscribed_channel_that_has_forwarded_is_still_forwarding(
        actually_forwarded, monkeypatch):
    """Positive control on the identical setup: only the counters differ, and
    the ordinary green state must still be reachable."""
    _process_table(monkeypatch)
    _configured(monkeypatch, True)
    rc, report = channel.health(actually_forwarded)
    assert report.splitlines()[0] == "channel: FORWARDING", report
    assert rc == channel.RC_FORWARDING


# --- the refusal marker: relabelled once the census explains it -------------

def test_a_refusal_marker_is_relabelled_once_the_census_calls_it_residue(
        actually_forwarded, monkeypatch):
    """`standing is False` -- #2182's exact census conclusion, "no standing
    claude-channel server is configured" -- must stop the marker from being
    printed as a live `refused:` finding beside a `#2133` citation."""
    _process_table(monkeypatch)

    def answer(name, _budget=None):
        if name == channel.CONSUMER_SERVER:
            return False, ""
        return True, ""
    monkeypatch.setattr(channel, "_configured", answer)
    Path(actually_forwarded + channel.REFUSAL_SUFFIX).write_text(json.dumps({
        "pid": 90927, "ts": "2026-09-22T14:11:26Z",
        "reason": "another claude-channel server is listening there",
        "sock_path": actually_forwarded,
    }), encoding="utf-8")
    rc, report = channel.health(actually_forwarded)
    assert report.splitlines()[0] == "channel: FORWARDING", report
    assert rc == channel.RC_FORWARDING
    assert "accounted for below" in report, report
    assert "#2133" not in report, report


def test_control_a_refusal_marker_with_a_configured_rival_stays_a_finding(
        actually_forwarded, monkeypatch):
    """Positive control on the identical fixture: a real standing rival
    server keeps the marker printed as a live finding, unchanged."""
    _process_table(monkeypatch)

    def answer(name, _budget=None):
        return True, ""
    monkeypatch.setattr(channel, "_configured", answer)
    Path(actually_forwarded + channel.REFUSAL_SUFFIX).write_text(json.dumps({
        "pid": 90927, "ts": "2026-09-22T14:11:26Z",
        "reason": "another claude-channel server is listening there",
        "sock_path": actually_forwarded,
    }), encoding="utf-8")
    rc, report = channel.health(actually_forwarded)
    assert report.splitlines()[0] == "channel: CANNOT DETERMINE", report
    assert "pid 90927 lost this socket" in report, report
    assert "accounted for below" not in report, report


# --- documentation -----------------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(2658)
