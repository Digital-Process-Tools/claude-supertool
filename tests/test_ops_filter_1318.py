"""ops:grep=PATTERN — filtering the roster without piping through grep (#1318).

`ops` prints every op's signature and refuses an unrecognised argument (#1231)
rather than silently discarding it — but that left `help:OP` / `ops:roster` as
the only routes to "which op does X", neither of which searches. Piping `ops`
through `grep` is what the raw-command guard blocks, correctly, so the only
route left was a redirect-to-temp-file workaround: the detour the guard exists
to prevent, reached by obeying it. Three independent agents hit this in one
week (#1318).

`ops:grep=PATTERN` matches op name, syntax and description, and always states
`N of M` — including `0 of M` — so an empty result can never be misread as a
short roster, the exact absence-as-answer defect `_ops_argument_refusal`'s own
docstring names.
"""

from __future__ import annotations

import supertool


def _fake_config():
    return {
        "builtin-ops": {
            "read": {"syntax": "read:PATH", "description": "Read a file"},
            "paste": {"syntax": "paste:PATH:CONTENT",
                      "description": "Creates a new file on disk"},
        },
        "ops": {
            "custom-op": {"syntax": "custom-op:X",
                          "description": "Does something custom"},
        },
    }


def test_ops_grep_matches_on_name(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=paste")
    assert "paste:PATH:CONTENT" in out
    assert "read:PATH" not in out
    assert "custom-op:X" not in out


def test_ops_grep_matches_on_description(monkeypatch) -> None:
    """'paste' never appears in this pattern — only its description does."""
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=Creates a new file")
    assert "paste:PATH:CONTENT" in out
    assert "read:PATH" not in out


def test_ops_grep_states_the_count_when_something_matches(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=paste")
    assert "1 of 3" in out


def test_ops_grep_zero_matches_states_the_count_not_an_empty_listing(monkeypatch) -> None:
    """The whole point (#1318): an empty result must never read like a short roster."""
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=zzzznomatch")
    assert "0 of 3" in out
    assert "paste" not in out


def test_ops_grep_empty_pattern_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=")
    assert "ERROR" in out


def test_ops_unknown_argument_refusal_names_the_filter(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:zzzz")
    assert "ops:grep=PATTERN" in out


def test_ops_compact_does_not_gain_the_filter(monkeypatch) -> None:
    """Scoped to bare `ops`, matching roster/session/full's own scope (#1231)."""
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops-compact:grep=paste")
    assert "ERROR" in out
