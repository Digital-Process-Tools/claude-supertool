"""#394 — content containing ''' has an escape hatch, and the error names it.

The failure surfaces as a column in the payload, which is where the block
closed, not where the caller's content closed it. Without the hint the reader
hunts a syntax error that is not there.
"""
import json
from pathlib import Path

import pytest

import supertool


# Python source that inspects Python source — the case the issue was filed on.
PY_CONTENT = 'if s.startswith(("#", "\'\'\'", "*")):'


def test_hint_fires_on_odd_delimiter_count() -> None:
    raw = "path = 'x'\nnew = '''a''' b'''\n"
    hint = supertool._toml_delimiter_hint(raw)
    assert "basic" in hint and "JSON" in hint


def test_hint_is_silent_on_balanced_payloads() -> None:
    assert supertool._toml_delimiter_hint("new = '''a'''\n") == ""


def test_hint_is_silent_when_there_are_no_literal_blocks() -> None:
    assert supertool._toml_delimiter_hint("path = 1 = 2\n") == ""


def test_parse_error_carries_the_hint(tmp_path: Path) -> None:
    payload = tmp_path / "p.toml"
    payload.write_text("path = 'x.py'\nnew = '''" + PY_CONTENT + "'''\n")
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file("@" + str(payload))
    message = str(excinfo.value)
    assert "TOML parse error" in message
    # The wording moved in #1830 and the diagnosis got narrower, not looser.
    # This payload's content carries a ''' run that closes the block early;
    # the count is odd as well, but that was never the mechanism, and saying
    # "odd number of runs" about a payload whose real problem is a run inside a
    # value sent the reader after a count. The message now names where the run
    # is. `test_an_odd_delimiter_run_is_already_explained` still pins the
    # odd-count wording, on a payload that has no early close to find.
    assert "closed the block early" in message
    assert "payload line 2" in message


def test_basic_block_carries_content_with_triple_quotes(tmp_path: Path) -> None:
    """The documented way out — a basic block, escapes and all."""
    payload = tmp_path / "p.toml"
    payload.write_text('path = "x.py"\nnew = """' + PY_CONTENT + '"""\n')
    loaded = supertool._load_at_file("@" + str(payload))
    assert loaded["new"] == PY_CONTENT


def test_json_payload_carries_it_too(tmp_path: Path) -> None:
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"path": "x.py", "new": PY_CONTENT}))
    assert supertool._load_at_file("@" + str(payload))["new"] == PY_CONTENT


def test_mini_parser_agrees_with_tomllib_on_the_escape_hatch() -> None:
    """The <3.11 fallback must accept the same way out, or the advice is wrong
    on exactly the platforms that most need it."""
    raw = 'path = "x.py"\nnew = """' + PY_CONTENT + '"""\n'
    assert supertool._mini_toml_loads(raw)["new"] == PY_CONTENT
