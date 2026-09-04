"""The @-payload route must not print the colon-split disclosure (#1825).

`grep:@-` and `around:@-` take `pattern` verbatim from the payload -- nothing
was split, so there is nothing to disclose. `_pattern_read_as_note` fires on
any colon in the pattern with no knowledge of which route it arrived by, so
a payload caller sees "Use grep:@- with a `pattern` key if the split was
meant to fall elsewhere" while already using that key on that route.

The positional-CLI route keeps the note (#1065, #1821) -- that half is
already pinned in test_grep_pattern_fidelity_1065_987_988.py and
test_around_colon_split_disclosure_1821.py and is not repeated here beyond
the one must-fire control needed to keep the must-not-fire assertion honest.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool


def _tree(tmp_path: Path) -> Path:
    d = tmp_path / "notes"
    d.mkdir()
    (d / "a.md").write_text("mode: remind\ntitle: alpha\n")
    (d / "b.md").write_text("mode: block\ntitle: beta\n")
    return d


def _payload(tmp_path: Path, name: str, obj: dict) -> Path:
    f = tmp_path / name
    f.write_text(json.dumps(obj), encoding="utf-8")
    return f


class TestGrepPayloadRouteSuppressesNote:
    def test_payload_route_prints_no_colon_split_note(self, tmp_path: Path) -> None:
        d = _tree(tmp_path)
        spec = _payload(tmp_path, "g.json",
                         {"pattern": "mode:.*remind", "path": str(d), "limit": 5})
        out = supertool.dispatch(f"grep:@{spec}")
        assert "results in" in out, (
            "positive control: the fixture must actually match, or the "
            "silence assertion below is about a dead harness: " + repr(out))
        assert "pattern read as" not in out, (
            "the payload route took `pattern` verbatim under a `pattern` "
            "key -- nothing was split, so the disclosure sends the caller "
            "back to the route already in use: " + repr(out))

    def test_positional_route_still_prints_the_note(self, tmp_path: Path) -> None:
        """Must-fire control: the CLI route still needs the disclosure."""
        d = _tree(tmp_path)
        out = supertool.dispatch(f"grep:mode:.*remind:{d}:5")
        assert "pattern read as" in out, (
            "the positional route DOES split on ':', so the disclosure "
            "must still fire there: " + repr(out))


class TestAroundPayloadRouteSuppressesNote:
    def test_payload_route_prints_no_colon_split_note(self, tmp_path: Path) -> None:
        d = _tree(tmp_path)
        spec = _payload(tmp_path, "a.json",
                         {"pattern": "mode:.*remind|title", "path": str(d), "n": 1})
        out = supertool.dispatch(f"around:@{spec}")
        assert "match at line" in out, (
            "positive control: the fixture must actually match: " + repr(out))
        assert "pattern read as" not in out, (
            "the payload route took `pattern` verbatim -- nothing was "
            "split, so around must not send the caller back to the route "
            "it is already on: " + repr(out))


class TestAroundPositionalRouteStillPrintsNote:
    def test_positional_route_still_prints_the_note(self, tmp_path: Path) -> None:
        """Must-fire control, same fixture as #1821's own test."""
        d = _tree(tmp_path)
        out = supertool.dispatch(f"around:mode:.*remind|title:{d}:1")
        assert "pattern read as" in out, (
            "the positional route DOES split on ':', so the disclosure "
            "must still fire there: " + repr(out))
