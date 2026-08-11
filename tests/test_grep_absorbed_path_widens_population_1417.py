"""grep refuses when an empty PATH slot widens the scan to the whole tree (#1417).

    grep:_FLAGS|def main:presets/github/issues.py:::40

was read as pattern `'_FLAGS|def main:presets/github/issues.py:'` with path
`'.'` and scanned 934 files. Nothing failed: the caller named one file, got a
repo-wide sweep for a pattern nobody typed, and the results were well-formed.

The `|` is incidental — `grep:PAT:PATH:` does it with no alternation at all.
The trigger is the empty PATH token: `_parse_grep_args` defaults an empty path
to `.` while the real path stays inside the rejoined pattern.

`_colon_split_hint` already produced exactly the right refusal for this family,
carrying the `grep:@-` escape — and bailed out on `path == "."` (`if not path or
path == "." or os.path.exists(path)`). The one reading that does not fail loudly
was the one reading it declined to diagnose.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import supertool
from _changelog_findable import assert_change_is_findable


def test_a_changelog_fragment_exists() -> None:
    assert_change_is_findable(1417)


def _tree(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("alpha elsewhere\n", encoding="utf-8")


def test_absorbed_path_does_not_widen_the_population(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The population, not the message: `other.py` was never asked about."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch("grep:alpha|beta:code.py:")
    assert "other.py" not in out, (
        "the caller named code.py; a hit in a sibling file means the scan "
        "silently became tree-wide: " + repr(out))
    assert "ERROR" in out, repr(out)


def test_the_filed_spelling_is_declined(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact shape from the report, trailing LIMIT and all."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch("grep:alpha|beta:code.py:::40")
    assert "other.py" not in out, repr(out)
    assert "ERROR" in out, repr(out)


def test_refusal_carries_the_corrected_call_and_the_payload_route(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`around_line`'s standard: name the call that was meant, in full."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch("grep:alpha|beta:code.py:")
    assert "ERROR" in out, repr(out)
    assert "grep:alpha|beta:code.py" in out, (
        "the corrected spelling has to be typeable, not described: "
        + repr(out))
    assert "grep:@-" in out, (
        "grep:@- is the documented route for a pattern with a ':' in it and "
        "nothing pointed at it from the failure: " + repr(out))


def test_a_colon_pattern_with_no_absorbed_path_still_scans_the_tree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-read is usually right. Measured over 155 grep spellings in this
    repo, 33 fire the ':' disclosure and 0 absorb a real path — so the refusal
    must not fire on any of them."""
    _tree(tmp_path)
    (tmp_path / "cpp.py").write_text("Class::CONST\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch("grep:Class::CONST:.")
    assert "ERROR" not in out, repr(out)
    assert "cpp.py" in out, repr(out)


def test_an_explicit_path_is_untouched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch("grep:alpha:code.py")
    assert "ERROR" not in out, repr(out)
    assert "other.py" not in out, repr(out)


def test_an_uncontained_segment_is_no_existence_oracle(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal stats pattern segments, which dispatch has NOT gated.

    `_gate_paths` runs on the PATH slot before the hint (#1166), precisely so
    the hint's stat is safe. A segment of the PATTERN arrives unchecked, so an
    outside path must never reach the stat — otherwise the refusal reports
    whether a file outside the boundary exists, which is the oracle that gate
    closes. The outside file is a real one created next to the cwd rather than
    a system path: `/etc/hosts` is absent on Windows, so the assertion would
    hold there for a reason that has nothing to do with the gate.
    """
    work = tmp_path / "work"
    work.mkdir()
    _tree(work)
    (tmp_path / "outside.txt").write_text("alpha\n", encoding="utf-8")
    monkeypatch.chdir(work)
    # The suite turns containment off wholesale (tests/conftest.py); a test
    # about containment has to turn it back on or it asserts nothing.
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    out = supertool.dispatch("grep:alpha|beta:../outside.txt:")
    assert "would have scanned the whole tree" not in out, (
        "an outside path must not be confirmed back to the caller: "
        + repr(out))
    assert "ERROR" not in out, repr(out)
