"""Direct unit coverage for the bash quote-balance tracker (#1810).

`unbalanced_quote_open` is a small, deliberately non-authoritative
state machine -- validators/common/quote_balance.py's own docstring lists
what it does not model (here-docs, command substitution, ANSI-C `$'...'`
quoting, nested constructs). These tests exercise the part it DOES claim:
single- and double-quote balance, `#` comments, and backslash escapes,
plus the boundary conditions a hand-rolled scanner like this one is most
likely to get wrong (empty input, a request past the end of the file, a
quote that opens on the very last scanned line).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMON = REPO / "validators" / "common"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def qb():
    if str(COMMON) not in sys.path:
        sys.path.insert(0, str(COMMON))
    return _load(COMMON / "quote_balance.py", "_v1810_quote_balance")


def test_empty_text_has_nothing_open():
    assert qb().unbalanced_quote_open("", 1) is None


def test_no_quotes_at_all():
    assert qb().unbalanced_quote_open("echo hi\necho bye\n", 2) is None


def test_single_quote_open_and_closed_same_line():
    assert qb().unbalanced_quote_open("FOO='bar'\necho done\n", 2) is None


def test_single_quote_left_open():
    text = "FOO='bar\necho done\n"
    assert qb().unbalanced_quote_open(text, 2) == 1


def test_double_quote_left_open():
    text = 'FOO="bar\necho done\n'
    assert qb().unbalanced_quote_open(text, 2) == 1


def test_backslash_escapes_a_double_quote_so_it_does_not_close():
    # \" inside a double-quoted string does not close it -- still open.
    text = 'FOO="a\\"b\necho done\n'
    assert qb().unbalanced_quote_open(text, 2) == 1


def test_backslash_has_no_special_meaning_inside_single_quotes():
    # A single-quoted string cannot contain a single quote at all, escaped
    # or not -- bash's own rule, and this scanner's `state == "'"` branch
    # never consumes two characters for a backslash. The quote right after
    # the backslash still closes the string.
    text = "FOO='a\\'\necho done\n"
    assert qb().unbalanced_quote_open(text, 2) is None


def test_comment_hides_a_quote_after_the_hash():
    text = "echo hi # a dangling ' quote in a comment\necho done\n"
    assert qb().unbalanced_quote_open(text, 2) is None


def test_quote_reopened_after_being_closed_is_the_new_open_line():
    text = "FOO='closed'\nBAR='open\necho x\n"
    assert qb().unbalanced_quote_open(text, 3) == 2


def test_upto_line_stops_the_scan_there():
    # The quote on line 2 is still open at line 2 itself...
    text = "echo hi\nFOO='open\necho x\n"
    assert qb().unbalanced_quote_open(text, 2) == 2
    # ...and reported the same way three lines further into the same
    # unterminated string, since nothing closes it.
    assert qb().unbalanced_quote_open(text, 3) == 2


def test_upto_line_past_the_end_of_a_short_file_does_not_crash():
    text = "FOO='open\n"
    assert qb().unbalanced_quote_open(text, 500) == 1


def test_documented_false_negative_hash_not_at_a_word_boundary():
    """A `#` immediately after non-whitespace is NOT a comment in real bash,
    but this tracker treats every unquoted `#` as one -- a documented gap
    (validators/common/quote_balance.py's own docstring). Pinned here so a
    change to that behavior is a decision, not an accident: real bash reads
    the apostrophe in `foo#bar'baz` as unterminated; this tracker, having
    treated `#bar'baz` as a comment, does not."""
    assert qb().unbalanced_quote_open("a=foo#bar'baz\n", 1) is None
