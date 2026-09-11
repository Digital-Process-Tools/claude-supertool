r"""#2493 -- an advisory note when a doubled-backslash escape sequence that
`literal_backslashes` exempted from the payload refusal reaches a `.py` file.

`_payload_double_backslash_refusal` (#1087) already blocks an even backslash
run in a write-bound field of a triple-single-quoted (TOML literal-block)
payload UNLESS the payload's own author exempts it via `literal_backslashes`.
That refusal is unconditional, so the scenario #2493 reported -- a doubled
backslash-n landing on disk where a single-backslash newline escape was
meant, `py-syntax` reporting `ok` because both parse -- can only reach
`_atomic_write` at all when the author already exempted the field.

The residual gap this file pins: the exemption is FIELD-scoped, not
OCCURRENCE-scoped, so one legitimate doubled run (a regex, a Windows path)
in a `content`/`new` field exempts every OTHER doubled run in that same
field too -- including a genuine typo sitting a few lines away. This is
exactly the shape #2493's own report describes happening while writing
`_supertool.py` itself, a file dense with legitimate backslash escapes.

Scope, per the issue's own settled decision:
  - only `.py` targets (docs/validators.md's "Declining instead of
    guessing" is the class this belongs to; not a general escape linter);
  - only occurrences the payload already exempted -- one still refused
    never reaches disk, so there is nothing to advise about;
  - only the TOML literal-block payload route (the one route that
    processes no escapes at all); never the escaped-string route, never
    JSON payloads;
  - advisory only: it is appended to the RECEIPT after the [validators]
    block (so after `py-syntax`'s own `ok` line), never to `py-syntax`'s own
    verdict, and it never blocks or rolls back the write.
"""
from __future__ import annotations

from pathlib import Path

import supertool

BS = chr(92)
NL = chr(10)
Q = chr(34)
Q3 = chr(39) * 3


def _payload(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def _toml_path(target: Path) -> str:
    r"""A payload `path =` as a basic string, with separators escaped.

    On Windows `tmp_path` is `C:\Users\...`, and an unescaped backslash in a
    basic TOML string starts an escape sequence -- the payload never parses.
    Same fixture trap and same fix as `test_payload_shell_quote_escape_
    refusal_2243.py`'s own `_toml_path`; not reused directly because that
    file's helper is private to it, but the shape is identical on purpose."""
    return Q + str(target).replace(BS, BS * 2) + Q


# ---------------------------------------------------------------------------
# Unit level -- the two new helpers directly
# ---------------------------------------------------------------------------

def test_target_path_for_top_level_label() -> None:
    parsed = {"path": "x.py", "content": "..."}
    assert supertool._payload_target_path_for_label(parsed, "content") == "x.py"


def test_target_path_for_batch_label() -> None:
    parsed = {"ops": [{"path": "a.txt"}, {"path": "b.py"}]}
    assert supertool._payload_target_path_for_label(parsed, "ops[1].content") == "b.py"


def test_target_path_for_out_of_range_batch_label() -> None:
    parsed = {"ops": [{"path": "a.py"}]}
    assert supertool._payload_target_path_for_label(parsed, "ops[5].content") == ""


def test_advisory_empty_when_nothing_exempted() -> None:
    """No `literal_backslashes` at all: every write-bound doubled run is
    still refused by `_payload_double_backslash_refusal` before this ever
    runs, so there is nothing exempted to advise about."""
    raw = (
        "path = " + Q + "x.py" + Q + NL
        + "content = " + Q3 + "a" + BS * 2 + "nb" + Q3 + NL
    )
    parsed = {"path": "x.py", "content": "a" + BS * 2 + "nb"}
    assert supertool._payload_py_doubled_backslash_advisory(parsed, raw) == {}


def test_advisory_fires_for_exempted_py_doubled_run(tmp_path: Path) -> None:
    """The bug shape itself: a doubled backslash-n where a real newline
    escape was meant, in a `content` field exempted via
    `literal_backslashes = true`, writing a `.py` file. The advisory names
    the field and the byte position."""
    raw = (
        "literal_backslashes = true" + NL
        + "path = " + Q + "x.py" + Q + NL
        + "content = " + Q3 + "a" + BS * 2 + "nb" + Q3 + NL
    )
    parsed = {
        "literal_backslashes": True,
        "path": "x.py",
        "content": "a" + BS * 2 + "nb",
    }
    import os
    advisory = supertool._payload_py_doubled_backslash_advisory(parsed, raw)
    assert list(advisory) == [os.path.abspath("x.py")]
    text = advisory[os.path.abspath("x.py")]
    assert "literal_backslashes" in text
    assert "`content`" in text
    assert "payload line 3" in text


def test_advisory_silent_for_non_py_target() -> None:
    """The same exempted doubled run, targeting `.txt` instead of `.py`:
    a Python string-literal escape is not the risk there, and the issue's
    own scope decision is `.py` only."""
    raw = (
        "literal_backslashes = true" + NL
        + "path = " + Q + "x.txt" + Q + NL
        + "content = " + Q3 + "a" + BS * 2 + "nb" + Q3 + NL
    )
    parsed = {
        "literal_backslashes": True,
        "path": "x.txt",
        "content": "a" + BS * 2 + "nb",
    }
    assert supertool._payload_py_doubled_backslash_advisory(parsed, raw) == {}


# ---------------------------------------------------------------------------
# Positive control: a genuine, plausibly-intentional doubled backslash (a
# Windows path) exempted the SAME way must not be treated differently by
# the mechanism -- the note is a decline-to-guess, not an accusation, and
# there is no way to distinguish this from a typo at this layer, which is
# why the note declines to guess rather than staying quiet. This pins that
# the mechanism does not special-case "looks intentional": it is scoped by
# exemption + `.py` target alone, exactly as narrowly as the issue asked.
# ---------------------------------------------------------------------------

def test_advisory_also_names_a_plausibly_intentional_windows_path() -> None:
    raw = (
        "literal_backslashes = true" + NL
        + "path = " + Q + "x.py" + Q + NL
        + "content = " + Q3 + "p = " + BS * 2 + "server" + BS * 2 + "share" + Q3 + NL
    )
    parsed = {
        "literal_backslashes": True,
        "path": "x.py",
        "content": "p = " + BS * 2 + "server" + BS * 2 + "share",
    }
    advisory = supertool._payload_py_doubled_backslash_advisory(parsed, raw)
    assert advisory  # still surfaced -- the tool cannot tell intent from bytes alone


# ---------------------------------------------------------------------------
# End to end, through the real payload route -- the receipt itself
# ---------------------------------------------------------------------------

def test_end_to_end_paste_carries_the_advisory_after_validators(tmp_path: Path) -> None:
    ref = _payload(
        tmp_path, "p.toml",
        "literal_backslashes = true" + NL
        + "path = " + _toml_path(tmp_path / "out.py") + NL
        + "content = " + Q3 + "x = " + Q + "a" + BS * 2 + "nb" + Q + Q3 + NL,
    )
    out = supertool.dispatch(f"paste:@{ref}")
    assert "ERROR" not in out
    written = (tmp_path / "out.py").read_text(encoding="utf-8")
    # The literal block processed no escapes: the doubled run reached disk
    # at its full length, exactly as #2493 reported.
    assert "a" + BS * 2 + "nb" in written
    assert "[validators]" in out
    assert "py-syntax" in out
    # The advisory is AFTER the validators block, never inside py-syntax's
    # own line, and py-syntax's own verdict is untouched (#2493's own
    # scope decision: "not to py-syntax's verdict, which stays ok").
    validators_idx = out.index("[validators]")
    py_syntax_line = next(l for l in out.splitlines() if "py-syntax" in l)
    py_syntax_idx = out.index(py_syntax_line)
    advisory_idx = out.index("literal_backslashes")
    assert py_syntax_idx > validators_idx
    assert advisory_idx > py_syntax_idx
    assert "\N{CROSS MARK}" not in py_syntax_line


def test_end_to_end_paste_without_exemption_is_refused_not_advised(tmp_path: Path) -> None:
    """Without `literal_backslashes`, the write is refused outright --
    #1087's existing mechanism -- and the advisory never gets a chance to
    fire, because nothing reached disk."""
    ref = _payload(
        tmp_path, "p.toml",
        "path = " + _toml_path(tmp_path / "out.py") + NL
        + "content = " + Q3 + "x = " + Q + "a" + BS * 2 + "nb" + Q + Q3 + NL,
    )
    out = supertool.dispatch(f"paste:@{ref}")
    assert "ERROR" in out
    assert not (tmp_path / "out.py").exists()


def test_end_to_end_escaped_string_route_is_never_advised(tmp_path: Path) -> None:
    """A normal (non-literal-block) TOML basic string, where escapes ARE
    processed, writing the SAME doubled bytes to disk as the literal-block
    bug case via a route the issue explicitly says stays untouched
    (#2493's scope decision: do not touch the normal escaped-string
    payload route)."""
    target = tmp_path / "out.py"
    spec = tmp_path / "e.toml"
    spec.write_text(
        "path = " + _toml_path(target) + NL
        + "content = " + Q + "x = " + BS + Q + "a" + BS * 4 + "nb"
        + BS + Q + Q + NL,
        encoding="utf-8",
    )
    out = supertool.dispatch(f"paste:@{spec}")
    assert "ERROR" not in out
    assert "literal_backslashes" not in out
    assert "\N{INFORMATION SOURCE}" not in out


def test_end_to_end_json_payload_route_is_never_advised(tmp_path: Path) -> None:
    """A JSON payload writing the identical doubled-backslash `.py` bytes:
    the advisory scans the raw TOML source for a triple-single-quoted
    literal block, so a JSON payload -- which never contains one -- can
    never trigger it."""
    import json
    target = tmp_path / "out.py"
    spec = tmp_path / "e.json"
    spec.write_text(json.dumps({
        "path": str(target),
        "content": "x = " + Q + "a" + BS * 2 + "nb" + Q + NL,
    }), encoding="utf-8")
    out = supertool.dispatch(f"paste:@{spec}")
    assert "ERROR" not in out
    assert "literal_backslashes" not in out
