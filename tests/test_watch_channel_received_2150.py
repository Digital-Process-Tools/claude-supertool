"""`channel:received:N` -- a session's own count of channel events it has
actually received, against the consumer's `forwarded` count (issue #2150).

#2051 named two remedies for "events forward on both sockets and none
arrive". PR #2147 closed the second, the static collision check. This is
the first, unbuilt when #2051 closed: every counter this subsystem
publishes -- `forwarded`, `dropped`, `lines_read` -- is the forwarder
describing its own outbox, and none of them can see the inbox. A session
with a broken last hop and a quiet session read identically.

Three states, and `UNSETTLED` is the one that keeps the other two honest: a
session that never calls this at all and one whose counts genuinely agree
must not render alike.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WATCH_DIR = str(REPO / "presets" / "watch")
PRESETS_DIR = str(REPO / "presets")

for _dir in (WATCH_DIR, PRESETS_DIR):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402


def _write_health(sock: str, **fields) -> None:
    record = {
        "pid": 1,
        "started": "2026-08-09T09:00:00Z",
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sock_path": sock,
        "lines_read": 0,
        "forwarded": 0,
        "dropped": 0,
        "last_forwarded": None,
    }
    record.update(fields)
    Path(sock + channel.HEALTH_SUFFIX).write_text(json.dumps(record), encoding="utf-8")


def _write_received(sock: str, **fields) -> None:
    record = {"received": 0, "forwarded_at_report": 0, "ts": "2026-08-09T09:00:00Z"}
    record.update(fields)
    Path(sock + channel.RECEIVED_SUFFIX).write_text(json.dumps(record), encoding="utf-8")


def test_a_first_report_is_unsettled_never_agree(tmp_path) -> None:
    """No prior receipt: there is nothing to diff against yet. This must not
    read as a match -- the whole point of the third state."""
    sock = str(tmp_path / "test.sock")
    _write_health(sock, forwarded=5)
    code, report = channel.record_received(sock, 5)
    assert code == 3
    assert "UNSETTLED" in report
    assert "AGREE" not in report


def test_counts_that_agree_over_a_window_report_agree(tmp_path) -> None:
    sock = str(tmp_path / "test.sock")
    _write_received(sock, received=10, forwarded_at_report=100)
    _write_health(sock, forwarded=107)
    code, report = channel.record_received(sock, 17)
    assert code == 0
    assert "AGREE" in report
    assert "7 received" in report
    assert "7 forwarded" in report


def test_counts_that_disagree_over_a_window_report_disagree_with_the_size(tmp_path) -> None:
    sock = str(tmp_path / "test.sock")
    _write_received(sock, received=10, forwarded_at_report=100)
    _write_health(sock, forwarded=110)
    # session reports +7 received, forwarded advanced +10 -- 3 missing.
    code, report = channel.record_received(sock, 17)
    assert code == 1
    assert "DISAGREE by 3" in report


def test_forwarded_going_backwards_is_unsettled_never_agree(tmp_path) -> None:
    """A restarted consumer's `forwarded` starts back at (near) zero. The
    window this call would diff spans the restart, and nothing forwarded on
    the old process's watch survives to compare -- must decline, not guess."""
    sock = str(tmp_path / "test.sock")
    _write_received(sock, received=50, forwarded_at_report=500)
    _write_health(sock, forwarded=3)  # the consumer restarted
    code, report = channel.record_received(sock, 53)
    assert code == 3
    assert "UNSETTLED" in report
    assert "AGREE" not in report
    assert "restarted" in report


def test_a_corrupt_prior_received_count_is_unsettled_never_agree(tmp_path) -> None:
    sock = str(tmp_path / "test.sock")
    _write_received(sock, received="not-a-number", forwarded_at_report=100)
    _write_health(sock, forwarded=110)
    code, report = channel.record_received(sock, 10)
    assert code == 3
    assert "UNSETTLED" in report
    assert "AGREE" not in report


def test_a_corrupt_prior_forwarded_baseline_is_unsettled_never_agree(tmp_path) -> None:
    sock = str(tmp_path / "test.sock")
    _write_received(sock, received=10, forwarded_at_report=None)
    _write_health(sock, forwarded=110)
    code, report = channel.record_received(sock, 20)
    assert code == 3
    assert "UNSETTLED" in report
    assert "AGREE" not in report


def test_unreadable_forwarded_is_unsettled_never_agree(tmp_path) -> None:
    """No health file at all: `forwarded` cannot be read, so nothing can be
    compared -- must not read as a match with a self-report of 0."""
    sock = str(tmp_path / "test.sock")
    code, report = channel.record_received(sock, 0)
    assert code == 3
    assert "UNSETTLED" in report
    assert "AGREE" not in report


def test_every_call_persists_a_receipt_for_the_next_one(tmp_path) -> None:
    sock = str(tmp_path / "test.sock")
    _write_health(sock, forwarded=50)
    channel.record_received(sock, 3)
    receipt, refusal = channel.read_received_receipt(sock)
    assert refusal == ""
    assert receipt["received"] == 3
    assert receipt["forwarded_at_report"] == 50


def test_no_receipt_ever_recorded_is_its_own_answer(tmp_path) -> None:
    sock = str(tmp_path / "test.sock")
    receipt, why = channel.read_received_receipt(sock)
    assert receipt is None
    assert "no receipt" in why


# --- main() argv wiring ------------------------------------------------------

def test_main_rejects_a_missing_count(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(channel, "SOCK_PATH", str(tmp_path / "x.sock"))
    code = channel.main(["channel.py", "received"])
    assert code == 2


def test_main_rejects_a_non_integer_count(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(channel, "SOCK_PATH", str(tmp_path / "x.sock"))
    code = channel.main(["channel.py", "received", "not-a-number"])
    assert code == 2


def test_main_rejects_a_negative_count(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(channel, "SOCK_PATH", str(tmp_path / "x.sock"))
    code = channel.main(["channel.py", "received", "-1"])
    assert code == 2


def test_main_routes_received_through_to_the_report(monkeypatch, tmp_path, capsys) -> None:
    sock = str(tmp_path / "x.sock")
    monkeypatch.setattr(channel, "SOCK_PATH", sock)
    _write_health(sock, forwarded=9)
    code = channel.main(["channel.py", "received", "9"])
    out = capsys.readouterr().out
    assert code == 3  # first call, no baseline yet
    assert "channel: received report" in out


def test_unknown_sub_op_now_names_all_three(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(channel, "SOCK_PATH", str(tmp_path / "x.sock"))
    code = channel.main(["channel.py", "bogus"])
    err = capsys.readouterr().err
    assert code == 2
    assert "channel:health" in err
    assert "channel:probe" in err
    assert "channel:received" in err
