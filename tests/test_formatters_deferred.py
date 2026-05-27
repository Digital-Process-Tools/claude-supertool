"""Tests for deferred-formatter behavior in multi-op invocations.

Issue #164: when several ops in one ./supertool call mutate the same file,
formatters running between ops (e.g. php-cs-fixer's no_unused_imports) can
strip symbols that a later op was about to consume. Defer formatters to
end-of-batch to keep "1 invocation = 1 round-trip" honest.
"""
from __future__ import annotations

import sys
from pathlib import Path

import supertool


def _set_formatters(fmt: dict) -> None:
    supertool._CONFIG = {"formatters": fmt}
    supertool._CONFIG_CHECKED = True


def _make_counting_cmd(tmp_path: Path) -> tuple[str, Path]:
    """Build a formatter cmd that appends a line to a counter file each call.

    Uses ``sys.executable -c`` so the command runs cross-platform (no POSIX shell needed).
    The counter path is written to a helper .py file to avoid quoting/escaping issues
    with spaces or backslashes in tmp_path on Windows.
    """
    counter = tmp_path / "fmt_runs.log"
    counter.write_text("")
    helper = tmp_path / "_fmt_helper.py"
    helper.write_text(
        "import sys\n"
        f"open({str(counter)!r}, 'a').write('run\\n')\n"
        'print(\'{"ok":true,"changes":{"lines_added":0,"lines_removed":0,"bytes_delta":0}}\')\n'
    )
    # Use forward slashes so shlex.split (POSIX mode) doesn't eat backslashes on Windows.
    exe = sys.executable.replace("\\", "/")
    helper_fwd = str(helper).replace("\\", "/")
    return f"{exe} {helper_fwd} {{file}}", counter


def test_defer_runs_formatter_once_per_file_across_multi_op(tmp_path, monkeypatch, capsys) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    cmd, counter = _make_counting_cmd(tmp_path)
    _set_formatters({"fakefmt": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.py"}})

    # Two edit ops on the same file — without deferral, formatter runs twice.
    monkeypatch.chdir(tmp_path)
    argv = [
        f"edit:::x = 1:::x = 2:::{target}",
        f"edit:::x = 2:::x = 3:::{target}",
    ]
    rc = supertool.main(argv)
    assert rc == 0
    assert target.read_text() == "x = 3\n"
    # Formatter ran exactly once for the file (deferred to end of batch).
    runs = counter.read_text().count("run\n")
    assert runs == 1, f"expected 1 deferred run, got {runs}"


def test_single_op_still_runs_formatter_inline(tmp_path, monkeypatch) -> None:
    target = tmp_path / "b.py"
    target.write_text("x = 1\n")
    cmd, counter = _make_counting_cmd(tmp_path)
    _set_formatters({"fakefmt": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.py"}})

    monkeypatch.chdir(tmp_path)
    rc = supertool.main([f"edit:::x = 1:::x = 2:::{target}"])
    assert rc == 0
    assert counter.read_text().count("run\n") == 1


def test_defer_handles_different_files_independently(tmp_path, monkeypatch) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n")
    b.write_text("y = 1\n")
    cmd, counter = _make_counting_cmd(tmp_path)
    _set_formatters({"fakefmt": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.py"}})

    monkeypatch.chdir(tmp_path)
    rc = supertool.main([
        f"edit:::x = 1:::x = 2:::{a}",
        f"edit:::y = 1:::y = 2:::{b}",
    ])
    assert rc == 0
    # Two distinct files → formatter runs once per file.
    assert counter.read_text().count("run\n") == 2


def test_defer_runs_formatter_on_survivor_when_later_op_rolls_back(tmp_path, monkeypatch) -> None:
    """Op 1 succeeds, op 2's validator rolls back — formatter still runs on op 1's file."""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n")
    b.write_text("y = 1\n")

    cmd, counter = _make_counting_cmd(tmp_path)
    # Validator: fails when file contains "BAD". rollback_on_fail restores pre-content.
    # Written to a helper .py file so there are no quoting/escaping issues on Windows.
    val_helper = tmp_path / "_val_helper.py"
    val_helper.write_text(
        "import sys, json\n"
        "content = open(sys.argv[1]).read()\n"
        "bad = 'BAD' in content\n"
        "print(json.dumps({'ok': False, 'tool': 'fakeval', 'errors': [{'msg': 'bad token'}]}"
        " if bad else {'ok': True, 'tool': 'fakeval'}))\n"
        "raise SystemExit(1 if bad else 0)\n"
    )
    # Use forward slashes so shlex.split (POSIX mode) doesn't eat backslashes on Windows.
    exe = sys.executable.replace("\\", "/")
    val_helper_fwd = str(val_helper).replace("\\", "/")

    supertool._CONFIG = {
        "formatters": {
            "fakefmt": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.py"},
        },
        "validators": {
            "fakeval": {
                "cmd": f"{exe} {val_helper_fwd} {{file}}",
                "hooks_into": ["edit"],
                "match": "*.py",
                "rollback_on_fail": True,
            },
        },
    }
    supertool._CONFIG_CHECKED = True

    monkeypatch.chdir(tmp_path)
    supertool.main([
        f"edit:::x = 1:::x = 2:::{a}",        # clean — survives
        f"edit:::y = 1:::y = BAD:::{b}",      # validator fails → rollback
    ])
    # Survivor edited; rolled-back file restored.
    assert a.read_text() == "x = 2\n"
    assert b.read_text() == "y = 1\n"
    # Formatter ran (deferred) at least once — survivor a.py must be in the queue.
    assert counter.read_text().count("run\n") >= 1


def test_defer_state_reset_between_invocations(tmp_path, monkeypatch) -> None:
    """Module-level queue must not leak across main() calls."""
    target = tmp_path / "c.py"
    target.write_text("x = 1\n")
    cmd, counter = _make_counting_cmd(tmp_path)
    _set_formatters({"fakefmt": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.py"}})

    monkeypatch.chdir(tmp_path)
    supertool.main([f"edit:::x = 1:::x = 2:::{target}", f"edit:::x = 2:::x = 3:::{target}"])
    assert supertool._DEFER_FORMATTERS is False
    assert supertool._FORMAT_QUEUE == {}
