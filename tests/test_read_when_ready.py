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


def _empty_then_filled(path: Path, created: threading.Event, payload: str = PAYLOAD):
    """Create `path` empty, announce it, fill it `FILL_DELAY` later. Returns the thread."""
    def _run() -> None:
        with open(path, "w", encoding="utf-8") as fh:
            created.set()
            time.sleep(FILL_DELAY)
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
    assert read_when_ready(ready_marker, json.loads) == ["edit", "x.php", "", "", ""]
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
    assert read_when_ready(marker).strip() == "edit target.php"
    writer.join(5)
