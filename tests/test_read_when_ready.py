"""Pins for `conftest.read_when_ready` — the "existence is not writtenness" rule.

The bug these exist for: a writer doing `open(p, "w").write(payload)` creates the
file empty and fills it a moment later, and a reader that polls `p.exists()` and
reads on the first True observes `""`. That is what took
`test_empty_placeholders_preserve_positional_argv` down on the macOS 3.9 CI leg
with `json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`.

Nothing here depends on CI timing. The writer holds the file open, empty, until
the reader has provably started, then writes — so the "half-written file" state
is entered deliberately rather than hoped for.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from conftest import read_when_ready

PAYLOAD = json.dumps(["edit", "x.php", "", "", ""])
FILL_DELAY = 0.3

# `read_when_ready`'s own default (2.0s) is a production tuning, not a budget
# for this test's conditions — borrowing it by omission is what flaked
# (windows-latest/3.11, PR #580): under `-n auto` on a loaded, low-core
# runner, this test's own writer *thread* can go unscheduled well past
# FILL_DELAY, the same class of scheduler starvation `read_when_ready`'s
# docstring already names for the reader's poll loop. Per the hang-guard test
# this repo already applies (#504/#505 — "multiply by ten; if it still
# catches the bug, widen rather than move it"): 20x FILL_DELAY still fails
# instantly if the writer never runs at all, and only buys patience for real
# scheduler jitter.
GENEROUS_TIMEOUT = FILL_DELAY * 20  # 6.0s


def _empty_then_filled(
    path: Path, created: threading.Event, payload: str = PAYLOAD, delay: float = FILL_DELAY
):
    """Create `path` empty, announce it, fill it `delay` later. Returns the thread."""
    def _run() -> None:
        with open(path, "w", encoding="utf-8") as fh:
            created.set()
            time.sleep(delay)
            fh.write(payload)

    t = threading.Thread(target=_run)
    t.start()
    return t


def test_existence_poll_reads_the_empty_file_read_when_ready_waits_out(tmp_path: Path) -> None:
    """The exact old shape blows up on the empty read; the helper returns the content."""
    naive_marker = tmp_path / "naive.json"
    created = threading.Event()
    writer = _empty_then_filled(naive_marker, created)
    assert created.wait(2), "writer never created the marker"

    # The pre-fix reader, verbatim: poll for existence, then read once.
    deadline = time.time() + 2
    while time.time() < deadline and not naive_marker.exists():
        time.sleep(0.05)
    assert naive_marker.exists(), "notifier did not fire"
    with pytest.raises(json.JSONDecodeError) as excinfo:
        json.loads(naive_marker.read_text(encoding="utf-8"))
    # The precise CI traceback line — this is what an empty read looks like.
    assert "Expecting value: line 1 column 1 (char 0)" in str(excinfo.value)
    writer.join(5)

    # Same writer, same window, reader that waits for parseable content.
    ready_marker = tmp_path / "ready.json"
    created = threading.Event()
    writer = _empty_then_filled(ready_marker, created)
    assert created.wait(2), "writer never created the marker"
    assert ready_marker.read_text(encoding="utf-8") == "", "fixture did not reproduce the empty window"

    started = time.monotonic()
    assert read_when_ready(ready_marker, json.loads, timeout=GENEROUS_TIMEOUT) == ["edit", "x.php", "", "", ""]
    # It waited rather than got lucky: it cannot have returned before the fill.
    assert time.monotonic() - started >= FILL_DELAY * 0.8
    writer.join(5)


def test_never_appeared_and_never_filled_are_reported_apart(tmp_path: Path) -> None:
    """Two different bugs, two different messages — `exists()` could tell neither."""
    missing = tmp_path / "never-written.json"
    with pytest.raises(AssertionError) as never_ran:
        read_when_ready(missing, json.loads, timeout=0.05)
    assert "never appeared" in str(never_ran.value)

    stuck = tmp_path / "empty-forever.json"
    stuck.write_text("")
    with pytest.raises(AssertionError) as never_landed:
        read_when_ready(stuck, json.loads, timeout=0.05)
    message = str(never_landed.value)
    assert "never appeared" not in message
    assert "never held parseable content" in message
    assert "last read: ''" in message


def test_truncated_json_is_not_mistaken_for_complete(tmp_path: Path) -> None:
    """A partial write is incomplete content, not just an empty file."""
    marker = tmp_path / "truncated.json"
    marker.write_text('["edit", "x.ph')
    with pytest.raises(AssertionError) as excinfo:
        read_when_ready(marker, json.loads, timeout=0.05)
    assert "never held parseable content" in str(excinfo.value)


def test_default_parser_accepts_any_non_empty_text(tmp_path: Path) -> None:
    """Text markers get the same guarantee without a parser argument."""
    marker = tmp_path / "text.txt"
    created = threading.Event()
    writer = _empty_then_filled(marker, created, payload="edit target.php")
    assert created.wait(2), "writer never created the marker"
    assert read_when_ready(marker, timeout=GENEROUS_TIMEOUT).strip() == "edit target.php"
    writer.join(5)


def test_writer_slower_than_the_librarys_default_budget_still_lands(tmp_path: Path) -> None:
    """RED pin for the reported flake (windows-latest/3.11, PR #580): a writer
    slower than the library's implicit default (timeout=2.0) must still be
    observed, because that default was never sized for this test's own CI
    conditions — it is `read_when_ready`'s production tuning, borrowed by
    omission. `-n auto` under load can starve this test's own writer thread of
    scheduling for longer than 2s (the class the helper's docstring already
    names, one layer up: it is not only the *reader's* poll loop that can be
    descheduled — the test's writer thread can be too).
    """
    marker = tmp_path / "slow.txt"
    created = threading.Event()
    delay = 2.5  # > the library's default timeout (2.0s), < GENEROUS_TIMEOUT (6.0s)
    writer = _empty_then_filled(marker, created, payload="edit target.php", delay=delay)
    assert created.wait(2), "writer never created the marker"
    assert read_when_ready(marker, timeout=GENEROUS_TIMEOUT).strip() == "edit target.php"
    writer.join(5)
