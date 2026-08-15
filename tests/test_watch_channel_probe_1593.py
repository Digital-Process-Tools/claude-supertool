"""`channel:probe` — one synthetic event through the whole path, on demand (#1593).

`channel:health` and `radar`'s delivery line both report on traffic that
*already happened*. Neither can say whether the read-and-forward path works
**now**: a consumer that is bound, counted and subscribed publishes the same
numbers whether it is reading its socket or wedged on it, and those numbers were
written by whatever last happened to flow. The gap #1593 names is that there was
no way to put a byte through the path on demand — the one time it was needed it
was done with hand-written Python against `transport.emit_socket`, which is how a
private record shape (`ts`, `source`, `id`, `event`, `payload`, `first_tick`)
became something a caller had to learn by reading the producer.

**The whole value of this op is in a refusal it makes about its own result.**
The last leg — arrival inside a session — is not observable from here, from this
process or from any other: the bridge sends a JSON-RPC notification, which has
no id and no response to wait on. So a probe that renders `forwarded` as
`delivered` would rebuild, inside its own fix, the defect it was written to
close. `test_probe_never_renders_forwarded_as_delivered` is that assertion.

**The negative assertion here has a positive control, in the same fixture.**
`test_probe_reports_not_delivering_when_nothing_is_listening` passes for a
probe that reports failure for *any* reason — an unbound socket in the harness,
a path that does not exist, a fixture that died before it spoke. Paired with
`test_probe_reports_forwarded_when_the_consumer_advances_its_counter`, built
from the same `FakeConsumer` on the same socket path, a broken harness fails the
must-fire half loudly instead of passing the must-not-fire half quietly.

**`lines_read` is why the report has five outcomes and not three.** The real
consumer increments `linesRead` when it takes a line off the wire, `forwarded`
when it hands one to the transport, and `dropped` when it refuses one — and
publishes after each. Reading all three separates *never read it* from *read it
and handed it on* from *read it and discarded it*, which are three different
places to go looking. Ignoring `lines_read` would fold the first into the
second and report a wedged reader as one that declined.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WATCH_DIR = str(REPO / "presets" / "watch")
PRESETS_DIR = str(REPO / "presets")
CHANNEL_OP = REPO / "presets" / "watch" / "channel.py"
WATCH_PRESET = REPO / "presets" / "watch.json"

for _dir in (WATCH_DIR, PRESETS_DIR, str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
import transport  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

NL = chr(10).encode("ascii")


def _can_bind_af_unix() -> bool:
    """Measured once, not guessed from `os.name` — the same probe
    `test_watch_channel_health_554.py` makes, and for the same reason:
    `hasattr(socket, "AF_UNIX")` is True on Windows builds of CPython and
    whether a bind then succeeds depends on the OS build. A platform branch
    here would make these tests pass vacuously on the leg least like the
    author's machine, which is worse than skipping them.
    """
    if not hasattr(socket, "AF_UNIX"):
        return False
    path = str(Path(tempfile.gettempdir()) / f"st1593probe-{os.getpid()}.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(path)
        return True
    except OSError:
        return False
    finally:
        s.close()
        try:
            os.unlink(path)
        except OSError:
            pass


CAN_BIND = _can_bind_af_unix()

needs_socket = pytest.mark.skipif(
    not CAN_BIND, reason="this platform cannot bind an AF_UNIX socket")


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sock_path() -> str:
    # macOS caps AF_UNIX paths near 104 bytes and pytest's `tmp_path` is long,
    # so the socket goes in the system temp dir. `gettempdir()` rather than a
    # "/tmp" literal: this module runs on every leg of the matrix, and "/tmp"
    # on Windows resolves to a drive-root path that need not exist.
    return str(Path(tempfile.gettempdir()) / f"st1593-{os.getpid()}-{time.time_ns()}.sock")


class FakeConsumer:
    """A stand-in for `claude-channel`, with its counter discipline copied.

    The real server increments `linesRead` when a line comes off the wire, then
    either `forwarded` (handing it to `mcp.notification()`) or `dropped`, and
    calls `publishHealth()` immediately after. This reproduces exactly that
    order, because the probe's whole subject is which of those three numbers
    moved.

    `forwards=False` is the wedged forwarder: it still reads and still
    re-stamps `updated` on every line, so the health file stays *fresh* while
    `forwarded` stays put. A frozen counter under a stale stamp is already
    `CANNOT DETERMINE` and says nothing; a frozen counter under a moving stamp
    is the finding.
    """

    def __init__(self, path: str, *, forwards: bool = True, drops: bool = False,
                 reads: bool = True, stale_baseline: float = 0.0) -> None:
        #: Seconds to back-date the *first* published `updated` stamp by. Every
        #: publish after it is stamped now, so this constructs the one state
        #: `channel:health` cannot resolve and a probe can: counters that went
        #: cold, over a read loop that is still working.
        self.stale_baseline = stale_baseline
        self.path = path
        self.health_path = path + channel.HEALTH_SUFFIX
        self.forwards = forwards
        self.drops = drops
        self.reads = reads
        self.lines_read = 0
        self.forwarded = 0
        self.dropped = 0
        self.last_forwarded = None
        #: The raw bytes of the last line taken off the wire, so a test can
        #: compare what was *sent* against what the report told the caller to
        #: look for. A report naming an id that never reached the socket is the
        #: same defect one field over.
        self.last_line = None
        #: Connections accepted and deliberately never drained, kept open for
        #: the lifetime of the fixture. See `_serve`.
        self.held = []
        #: The fixture's own third state (#1758). `lines_read == 0` has two
        #: readings — nothing was sent, or the read raised and nobody heard —
        #: and a test whose premise is the first passes under the second unless
        #: the harness can say which it hit. Every test below that depends on
        #: the read path asserts this is empty, so a broken harness fails
        #: loudly instead of satisfying a negative assertion for free.
        self.errors = []
        self.started = _stamp()
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        self.srv.settimeout(0.1)
        self._stop = threading.Event()
        # Published from the moment the socket is bound and before a single
        # event, exactly as the real consumer does (`writeHealthNow()` at the
        # bottom of channel.ts), so that "no health file" keeps meaning
        # "whatever is bound here is not claude-channel".
        self.publish()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def publish(self) -> None:
        stamped = time.gmtime(time.time() - self.stale_baseline)
        # Once. The point of the option is a stamp that goes cold and then
        # comes back, so only the baseline is back-dated.
        self.stale_baseline = 0.0
        record = {
            "pid": os.getpid(),
            "started": self.started,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", stamped),
            "sock_path": self.path,
            "lines_read": self.lines_read,
            "forwarded": self.forwarded,
            "dropped": self.dropped,
            "last_forwarded": self.last_forwarded,
        }
        tmp = f"{self.health_path}.{os.getpid()}.tmp"
        Path(tmp).write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, self.health_path)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            if not self.reads:
                # Bound, accepting, and never taking the line off the wire —
                # and the connection is **held open**, not closed. Closing it
                # here made the producer's `sendall` race a `BrokenPipeError`,
                # which is `EMIT_UNKNOWN` and a completely different arm: the
                # test passed or failed on whether the bytes reached the kernel
                # buffer first. A wedged reader is a live connection nobody is
                # draining, so that is what the fixture has to be.
                self.held.append(conn)
                continue
            with conn:
                conn.settimeout(1.0)
                buf = b""
                try:
                    while NL not in buf:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                except OSError as err:
                    # Recorded, not swallowed (#1758). This is the one `except`
                    # in the fixture that is NOT teardown: it guards the read
                    # path, which is the thing every counter assertion in this
                    # file is about. A bare `pass` here gives the harness two
                    # indistinguishable ways to report `lines_read == 0` — the
                    # consumer did not read, and the consumer could not be
                    # heard — and the second one passes the tests whose premise
                    # is the first. That is this file's own subject turned on
                    # its own scaffolding, and it is a near neighbour of the
                    # BrokenPipeError fixture bug already fixed above.
                    self.errors.append(f"{type(err).__name__}: {err}")
                for line in buf.split(NL):
                    if not line.strip():
                        continue
                    self.lines_read += 1
                    self.last_line = line
                    if self.drops:
                        self.dropped += 1
                    elif self.forwards:
                        self.forwarded += 1
                        self.last_forwarded = _stamp()
                    self.publish()

    def close(self) -> None:
        """Teardown, and every `except OSError` below is teardown-only (#1758).

        The distinction from the one in `_serve` is the whole answer to that
        review: nothing here can make an assertion pass. This runs after the
        serve thread has been stopped and joined, no test reads any of these
        results, and each descriptor is already known-dead to the only peer
        that ever held it — the probe, which is a finished subprocess or a
        returned function call by the time anything gets here.

        What a failure would cost is a leaked file in the system temp
        directory, and not a wrong verdict: `_sock_path()` keys on pid *and*
        `time.time_ns()`, so a socket or health file that outlives its fixture
        cannot be picked up by another test. Raising instead would be actively
        worse — it would replace a real assertion failure with a teardown
        error raised from the `finally` that was trying to report it.
        """
        self._stop.set()
        self.thread.join(timeout=5)
        for conn in self.held:
            try:
                conn.close()
            except OSError:
                pass  # teardown: the peer is gone, and nothing reads this
        try:
            self.srv.close()
        except OSError:
            pass  # teardown: an unclosable listener outlives the process anyway
        for path in (self.path, self.health_path):
            try:
                os.unlink(path)
            except OSError:
                pass  # teardown: a leaked temp file, and the names are unique


def _harness_was_heard(consumer: FakeConsumer) -> None:
    """The fixture's read path raised nothing, so its counters mean what they say.

    Called in every test that builds a `FakeConsumer`, and it is the reason
    `_serve` records an `OSError` instead of swallowing one (#1758). Two tests
    here have `lines_read == 0` as their *premise* — the wedged reader, and the
    cold baseline that stays cold — and that number has two causes: nothing
    arrived, or the read raised and nobody heard. Without this, a harness that
    broke would satisfy those assertions for free, which is precisely the
    failure the op under test exists to refuse, wearing the test suite as a
    costume.

    Deliberately not folded into `close()`: that runs in a `finally`, so an
    assertion there would fire *instead of* whatever real failure the block was
    trying to report.
    """
    assert not consumer.errors, (
        "the fixture's own read path raised, so its counters are not evidence "
        f"either way: {consumer.errors}")


def _run_probe(sock: str, extra_env=None, sub: str = "probe"):
    # `PYTHONIOENCODING` because that is what supertool exports before
    # dispatching a preset op (`_supertool.py`, #415): the report carries em
    # dashes, and on a cp1252 Windows runner the child would otherwise die with
    # UnicodeEncodeError while printing its own verdict. Running the op under a
    # different environment from the one it ships in tests something the
    # operator never runs.
    env = {**os.environ, "SUPERTOOL_WATCH_SOCK": sock, "PYTHONIOENCODING": "utf-8"}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(CHANNEL_OP), sub],
        capture_output=True, text=True, env=env, timeout=60,
        # Explicit, not the locale codec: the Windows runners decode by cp1252,
        # where the failure surfaces as a TypeError naming nothing (#856).
        encoding="utf-8", errors="replace",
    )


# --- the must-not-fire half, and the must-fire half beside it ---------------

def test_probe_reports_not_delivering_when_nothing_is_listening(tmp_path):
    """A definite negative: nothing took the bytes, so nothing can arrive.

    On its own this passes for a probe that reports failure for an unrelated
    reason. The control is the next test, which uses the same fixture and the
    same code path and requires the counter to *move*.
    """
    result = _run_probe(str(tmp_path / "absent.sock"))
    assert result.returncode == channel.RC_NOT_DELIVERING, result.stdout + result.stderr
    assert result.stdout.startswith("channel: NOT DELIVERING"), result.stdout


@needs_socket
def test_probe_reports_forwarded_when_the_consumer_advances_its_counter(tmp_path):
    """The positive control. A live consumer reads the synthetic line and
    increments `forwarded`; the probe must observe the advance and say so."""
    path = _sock_path()
    consumer = FakeConsumer(path)
    try:
        result = _run_probe(path, {"SUPERTOOL_WATCH_STATE_DIR": str(tmp_path)})
    finally:
        consumer.close()
    _harness_was_heard(consumer)
    assert result.returncode == channel.RC_FORWARDING, result.stdout + result.stderr
    assert result.stdout.startswith("channel: FORWARDED"), result.stdout
    assert consumer.forwarded == 1, "the fixture never saw the probe's event"


@needs_socket
def test_probe_never_renders_forwarded_as_delivered(tmp_path):
    """The refusal the op exists for. `forwarded` means handed to the MCP
    transport; arrival in a session is observable only from inside it. A report
    that says "delivered" — under any verdict — is this issue rebuilt inside
    its own fix."""
    path = _sock_path()
    consumer = FakeConsumer(path)
    try:
        result = _run_probe(path, {"SUPERTOOL_WATCH_STATE_DIR": str(tmp_path)})
    finally:
        consumer.close()
    _harness_was_heard(consumer)
    body = result.stdout.lower()
    assert "delivered" not in body, result.stdout
    assert "delivery" not in body, result.stdout
    assert "forwarded" in body


@needs_socket
def test_probe_names_the_tag_the_caller_should_now_look_for(tmp_path):
    """The other half of the answer is the caller's to observe, so the report
    has to hand them something exact to look for — the reserved source and the
    per-run id, not "check your session"."""
    path = _sock_path()
    consumer = FakeConsumer(path)
    try:
        result = _run_probe(path, {"SUPERTOOL_WATCH_STATE_DIR": str(tmp_path)})
    finally:
        consumer.close()
    _harness_was_heard(consumer)
    assert transport.PROBE_SOURCE in result.stdout
    assert "watcher_source" in result.stdout, result.stdout
    assert consumer.lines_read == 1
    # The id is generated per run and must reach both the wire and the report,
    # or the caller is told to look for something that was never sent.
    emitted = json.loads(consumer.last_line.decode("utf-8"))
    assert emitted["source"] == transport.PROBE_SOURCE
    assert emitted["id"] in result.stdout, result.stdout


# --- the reserved source, and the side effect it must not have --------------

def test_the_probe_source_is_reserved_and_impersonates_no_watcher():
    """No bundled source may answer to the probe's `source`, or the tag it
    produces would be indistinguishable from a real watcher's."""
    bundled = {p.name for p in (REPO / "presets" / "watch" / "sources").iterdir()
               if p.is_dir()}
    assert bundled, "the source directory scan found nothing — the premise is unchecked"
    assert transport.PROBE_SOURCE not in bundled


@needs_socket
def test_probe_writes_no_watcher_state_file(tmp_path):
    """`emit_event` refreshes a state file; the probe must not, because a
    reserved source with a state file of its own would show up on `watches` and
    on `radar`'s delivery survey as a watcher that does not exist."""
    path = _sock_path()
    consumer = FakeConsumer(path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    try:
        result = _run_probe(path, {"SUPERTOOL_WATCH_STATE_DIR": str(state_dir)})
    finally:
        consumer.close()
    _harness_was_heard(consumer)
    assert result.returncode == channel.RC_FORWARDING, result.stdout + result.stderr
    left = sorted(p.name for p in state_dir.iterdir())
    assert left == [], f"the probe left {left} in the poller state directory"


# --- everything else it cannot claim ----------------------------------------

@needs_socket
def test_probe_declines_when_the_consumer_publishes_no_counters(tmp_path):
    """Bound, took the bytes, and there is no baseline to compare against. That
    is not a negative and it is certainly not a positive."""
    path = _sock_path()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    try:
        result = _run_probe(path, {"SUPERTOOL_WATCH_STATE_DIR": str(tmp_path)})
    finally:
        srv.close()
        os.unlink(path)
    assert result.returncode == channel.RC_UNKNOWN, result.stdout + result.stderr
    assert result.stdout.startswith("channel: CANNOT DETERMINE"), result.stdout


@needs_socket
def test_probe_reports_a_finding_when_the_line_is_read_and_not_forwarded():
    """`lines_read` moved and `forwarded` did not: the consumer took the event
    off the wire and handed it nowhere. A finding, and not the same answer as
    "it has not read it yet"."""
    path = _sock_path()
    consumer = FakeConsumer(path, forwards=False)
    try:
        code, report = channel.probe(path, wait=1.5)
    finally:
        consumer.close()
    _harness_was_heard(consumer)
    assert consumer.lines_read == 1, "the fixture never read the probe's event"
    assert code == channel.RC_PROBE_NOT_FORWARDED, report
    assert report.startswith("channel: ACCEPTED, NOT FORWARDED"), report


@needs_socket
def test_probe_names_the_discard_when_dropped_is_what_moved():
    """A consumer that read the line and refused it is a different finding from
    one that read it and did nothing, and it sends an operator somewhere else —
    the burst budget, not the read loop."""
    path = _sock_path()
    consumer = FakeConsumer(path, drops=True)
    try:
        code, report = channel.probe(path, wait=1.5)
    finally:
        consumer.close()
    _harness_was_heard(consumer)
    assert consumer.dropped == 1
    assert code == channel.RC_PROBE_DISCARDED, report
    assert report.startswith("channel: ACCEPTED, DISCARDED"), report


@needs_socket
def test_probe_declines_when_the_consumer_never_reads_the_line():
    """Bound, accepting, heartbeating, and not taking the line off the wire
    inside the probe's budget. Slow and wedged are indistinguishable in a
    bounded wait, so this is `CANNOT DETERMINE` with the reason — reporting it
    as a finding would claim more than was measured."""
    path = _sock_path()
    consumer = FakeConsumer(path, reads=False)
    try:
        code, report = channel.probe(path, wait=0.8)
    finally:
        consumer.close()
    _harness_was_heard(consumer)
    assert consumer.lines_read == 0
    assert code == channel.RC_UNKNOWN, report
    assert report.startswith("channel: CANNOT DETERMINE"), report
    assert "has not read" in report, report


def test_probe_declines_rather_than_guessing_when_the_after_read_fails(monkeypatch, tmp_path):
    """The third state on the read itself. A health file that becomes
    unreadable *during* the wait must not render as "the counter did not move":
    one is a finding about the consumer, the other is a finding about this
    process's own eyesight.

    Driven through `read_health` rather than through a socket, so it runs on
    every leg of the matrix including the ones that cannot bind AF_UNIX.
    """
    path = str(tmp_path / "probe.sock")
    before = {
        "pid": os.getpid(), "started": _stamp(), "updated": _stamp(),
        "sock_path": path, "lines_read": 0, "forwarded": 7, "dropped": 0,
        "last_forwarded": None,
    }
    reads = {"n": 0}

    def fake_read_health(_path):
        reads["n"] += 1
        if reads["n"] == 1:
            return dict(before), ""
        return None, "the health file went away mid-probe"

    monkeypatch.setattr(channel, "read_health", fake_read_health)
    monkeypatch.setattr(channel.transport, "emit_socket",
                        lambda record, path=None: transport.Emit(
                            transport.EMIT_ACCEPTED, "accepted the bytes"))
    monkeypatch.setattr(channel, "peer_pid", lambda _p: (os.getpid(), ""))
    code, report = channel.probe(path, wait=0.2)
    assert code == channel.RC_UNKNOWN, report
    assert report.startswith("channel: CANNOT DETERMINE"), report
    assert "went away mid-probe" in report, report


# --- a cold baseline is what this op is most for -----------------------------
#
# Observed on 2026-08-15 and it is why these two exist. The consumer over this
# clone was holding the socket with its counters 607s cold, `channel:health`
# said CANNOT DETERMINE, and the first cut of `probe` copied that verdict
# straight out — it treated a stale baseline as a reason to abort *before*
# emitting anything worth waiting on. Then a maintainer's session reported that
# the probe's event had arrived in it as a rendered `<channel>` tag at
# 19:16:32Z. The path was working the whole time.
#
# So the short-circuit was throwing away the strongest evidence this op can
# produce, in exactly the case that produces it. A stale stamp on the baseline
# is not a reason to decline: it is the question. What decides the verdict is
# whether the file comes *back* — fresh, and advanced — after the emit.

@needs_socket
def test_a_cold_baseline_that_warms_and_advances_is_a_definite_positive():
    """`channel:health` cannot get past a stale stamp; this must.

    The counters are 300s cold at the baseline — well past
    `STALE_AFTER_SECS` — and the consumer then reads the probe's line,
    increments and republishes with a current stamp. Declining here would
    reproduce, inside the probe, the exact limit the probe was written to get
    past.
    """
    path = _sock_path()
    consumer = FakeConsumer(path, stale_baseline=300.0)
    try:
        code, report = channel.probe(path, wait=2.0)
    finally:
        consumer.close()
    _harness_was_heard(consumer)
    assert consumer.forwarded == 1, "the fixture never forwarded the probe's event"
    assert code == channel.RC_FORWARDING, report
    assert report.startswith("channel: FORWARDED"), report
    # The report must not hide where it started, or a reader cannot tell this
    # apart from a channel that was healthy all along.
    assert "cold" in report or "stale" in report, report


@needs_socket
def test_a_cold_baseline_that_stays_cold_is_still_not_a_verdict():
    """The control for the test above, and the reason it is not just a
    loosened guard. A consumer whose stamp never comes back has published
    nothing this op can compare, so tolerating a cold baseline must not turn
    into believing one."""
    path = _sock_path()
    consumer = FakeConsumer(path, stale_baseline=300.0, reads=False)
    try:
        code, report = channel.probe(path, wait=0.8)
    finally:
        consumer.close()
    _harness_was_heard(consumer)
    assert code == channel.RC_UNKNOWN, report
    assert report.startswith("channel: CANNOT DETERMINE"), report
    assert "refreshed" in report or "stale" in report, report


# --- the op's declared budget, and its documentation ------------------------

def test_the_probe_fits_inside_the_declared_op_timeout():
    """The arithmetic, not the number — #1558's lesson, applied to the second
    subcommand. A probe that cannot answer before its own op times out can
    never reach the honest verdict it was written to produce: the reader gets
    supertool's bare `TIMEOUT` with an empty body instead."""
    declared = json.loads(WATCH_PRESET.read_text(encoding="utf-8"))
    timeout = declared["ops"]["channel"]["timeout"]
    worst = max(channel.SUBSCRIPTION_WORST_CASE, channel.PROBE_WORST_CASE)
    assert timeout > worst, (
        f"channel is declared at {timeout}s and its worst sub-op costs {worst}s")


def test_the_preset_declares_the_new_sub_op():
    """An op nobody can discover is not shipped, and `syntax` is where a caller
    looks first."""
    declared = json.loads(WATCH_PRESET.read_text(encoding="utf-8"))
    op = declared["ops"]["channel"]
    assert "probe" in op["syntax"], op["syntax"]
    assert "probe" in op["description"]


def test_an_unknown_sub_op_is_still_refused_by_name():
    """Two sub-ops now, and the refusal has to name both — a message that still
    said "the only one is channel:health" would send a caller away from the op
    that answers their question."""
    result = _run_probe(str(Path(tempfile.gettempdir()) / "st1593-nope.sock"), sub="wat")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "probe" in result.stderr, result.stderr
    assert "health" in result.stderr, result.stderr


def test_change_is_findable():
    assert_change_is_findable(1593)
