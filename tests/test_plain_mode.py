"""Tests for plain/ASCII output mode — issue #308.

Hooks, ``grep`` and CI parse op output with no UTF-8 / locale guarantee. Plain
mode (``--plain`` flag or ``SUPERTOOL_PLAIN=1``) swaps the status glyphs for
stable ASCII markers (``[WARN]``/``[OK]``/``[FAIL]``/``[INFO]``) so machine
consumers never depend on a multibyte glyph. Default (rich) output is unchanged.
Covers: the plain_mode()/mark() helpers, the --plain → env propagation in
main(), and the defensive stdout UTF-8 reconfigure (must not crash).
"""
from __future__ import annotations

import io
import os

import pytest

import supertool


@pytest.fixture
def restore_plain_env():
    """main() mutates os.environ['SUPERTOOL_PLAIN'] directly (to reach preset
    subprocesses), which monkeypatch can't auto-revert. Snapshot + restore with
    raw os.environ so the leak doesn't bleed into later tests in the process."""
    saved = os.environ.get("SUPERTOOL_PLAIN")
    os.environ.pop("SUPERTOOL_PLAIN", None)
    yield
    if saved is None:
        os.environ.pop("SUPERTOOL_PLAIN", None)
    else:
        os.environ["SUPERTOOL_PLAIN"] = saved


# ---------------------------------------------------------------------------
# plain_mode() — env detection
# ---------------------------------------------------------------------------

def test_plain_mode_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_PLAIN", raising=False)
    assert supertool.plain_mode() is False


def test_plain_mode_truthy_values(monkeypatch) -> None:
    for val in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("SUPERTOOL_PLAIN", val)
        assert supertool.plain_mode() is True, val


def test_plain_mode_falsy_values(monkeypatch) -> None:
    for val in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("SUPERTOOL_PLAIN", val)
        assert supertool.plain_mode() is False, val


# ---------------------------------------------------------------------------
# mark() — glyph → ASCII marker mapping
# ---------------------------------------------------------------------------

def test_mark_rich_mode_returns_glyph(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_PLAIN", raising=False)
    assert supertool.mark("⚠") == "⚠"
    assert supertool.mark("✓") == "✓"
    assert supertool.mark("✗") == "✗"
    assert supertool.mark("ℹ") == "ℹ"


def test_mark_plain_mode_returns_ascii(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_PLAIN", "1")
    assert supertool.mark("⚠") == "[WARN]"
    assert supertool.mark("✓") == "[OK]"
    assert supertool.mark("✗") == "[FAIL]"
    assert supertool.mark("ℹ") == "[INFO]"
    # Every ASCII marker is, well, ASCII.
    for glyph in ("⚠", "✓", "✗", "ℹ"):
        supertool.mark(glyph).encode("ascii")  # raises if non-ASCII


def test_mark_unknown_glyph_passes_through(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_PLAIN", "1")
    assert supertool.mark("→") == "→"


# ---------------------------------------------------------------------------
# main() — --plain flag is consumed and exported for preset subprocesses
# ---------------------------------------------------------------------------

def test_plain_flag_sets_env_and_is_consumed(monkeypatch, restore_plain_env) -> None:
    seen: list[str] = []
    monkeypatch.setattr(supertool, "dispatch", lambda a: seen.append(a) or "")
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    rc = supertool.main(["--plain", "read:foo"])

    assert rc == 0
    assert supertool.plain_mode() is True          # exported to env
    assert seen == ["read:foo"]                     # flag stripped from ops


def test_no_plain_flag_leaves_env_untouched(monkeypatch, restore_plain_env) -> None:
    monkeypatch.setattr(supertool, "dispatch", lambda a: "")
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    supertool.main(["read:foo"])

    assert supertool.plain_mode() is False


# ---------------------------------------------------------------------------
# _reconfigure_stdout_utf8() — defensive, must never crash
# ---------------------------------------------------------------------------

def test_reconfigure_stdout_utf8_does_not_crash() -> None:
    # Plain call against the real streams — must be a no-op-safe operation.
    supertool._reconfigure_stdout_utf8()


def test_reconfigure_stdout_utf8_tolerates_streams_without_reconfigure(monkeypatch) -> None:
    # A StringIO has no .reconfigure — the helper must skip it silently.
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    supertool._reconfigure_stdout_utf8()  # no AttributeError


def test_reconfigure_stdout_utf8_swallows_reconfigure_errors(monkeypatch) -> None:
    class Boom:
        def reconfigure(self, **kwargs):
            raise ValueError("cannot reconfigure")

    monkeypatch.setattr("sys.stdout", Boom())
    monkeypatch.setattr("sys.stderr", Boom())
    supertool._reconfigure_stdout_utf8()  # ValueError swallowed
