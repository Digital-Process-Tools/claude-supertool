r"""#834 — a payload that meant to end a literal block with a quote.

The issue was filed as "a payload string ending in `'` writes broken code",
with the proposed fix "refuse content ending in `'`". The premise is wrong and
the tests below are what say so:

* A TOML multi-line literal CAN end with an apostrophe. `'''a''''` is legal and
  parses to `a'` — a closing run may be 4 or 5 quotes, and the surplus is
  content. So `'''    kind = 'mr''''` is not merely allowed, it is *the correct
  spelling* of the payload in the issue. A guard that refused a value ending in
  `'` would refuse the fix.
* What actually broke the reported write is the backslash. The caller typed
  `'mr\'` out of escape reflex; inside `'''...'''` a backslash is content and
  never an escape, so the value ended `\'` and the Python written from it
  parsed as something else entirely.

So the guard fires on `\` immediately before a closing `'''` run, not on a
trailing quote. It refuses rather than warns for one reason: *both* readings of
that backslash have another spelling (drop it, or use a basic block and double
it), so nothing becomes unwritable. Where a refusal would leave an intent with
no way to be expressed, a warning is the honest severity instead.

And the escape hatch has to exist on every interpreter. `_mini_toml_loads`
(Python <3.11) closed a literal block at the first `'''` and choked on the
surplus quote, so the spelling the refusal message recommends parsed on 3.11+
and failed below it — the #684 rule, one delimiter over.
"""
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

import supertool


# (name, TOML source, expected) — expected None means "must raise".
# Read off tomllib; the 3.11+ leg below is what keeps that claim honest.
SURPLUS_QUOTE_CASES: Tuple[Tuple[str, str, Optional[Dict[str, Any]]], ...] = (
    ("literal_one_surplus", "a = '''x''''", {"a": "x'"}),
    ("literal_two_surplus", "a = '''x'''''", {"a": "x''"}),
    ("literal_none", "a = '''x'''", {"a": "x"}),
    ("literal_three_surplus_is_an_error", "a = '''x''''''", None),
    ("basic_one_surplus", 'a = """x""""', {"a": 'x"'}),
    ("basic_two_surplus", 'a = """x"""""', {"a": 'x""'}),
    ("basic_none", 'a = """x"""', {"a": "x"}),
    # The issue's payload, spelled correctly: the closer carries the quote.
    ("the_issue_spelled_right", "a = '''    kind = 'mr''''", {"a": "    kind = 'mr'"}),
)

SURPLUS_IDS = tuple(name for name, _, _ in SURPLUS_QUOTE_CASES)


def _outcome(parser: Any, source: str) -> Tuple[str, Any]:
    try:
        return "ok", parser(source)
    except Exception:
        return "error", None


@pytest.mark.parametrize("name,source,expected", SURPLUS_QUOTE_CASES, ids=SURPLUS_IDS)
def test_fallback_matches_the_reference_table(
    name: str, source: str, expected: Optional[Dict[str, Any]]
) -> None:
    """The <3.11 parser, on every interpreter in the matrix."""
    status, value = _outcome(supertool._mini_toml_loads, source)
    if expected is None:
        assert status == "error", (
            f"{name}: fallback accepted {source!r} -> {value!r}; tomllib rejects it"
        )
    else:
        assert status == "ok", f"{name}: fallback rejected {source!r}"
        assert value == expected, f"{name}: fallback produced {value!r}"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is 3.11+")
@pytest.mark.parametrize("name,source,expected", SURPLUS_QUOTE_CASES, ids=SURPLUS_IDS)
def test_reference_table_is_a_truthful_transcript_of_tomllib(
    name: str, source: str, expected: Optional[Dict[str, Any]]
) -> None:
    """The table is only worth anything if tomllib really says this."""
    import tomllib

    status, value = _outcome(tomllib.loads, source)
    if expected is None:
        assert status == "error", f"{name}: tomllib accepted {source!r} -> {value!r}"
    else:
        assert status == "ok", f"{name}: tomllib rejected {source!r}"
        assert value == expected, f"{name}: tomllib produced {value!r}"


@pytest.fixture
def no_tomllib(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `import tomllib` fail, so `_load_at_file` takes the <3.11 route."""
    monkeypatch.setitem(sys.modules, "tomllib", None)


# The payload from the issue, and the payload it should have been.
BROKEN = "path = {p}\nold = \"OLD\"\nnew = '''    kind = 'mr\\''''\n"
CORRECT = "path = {p}\nold = \"OLD\"\nnew = '''    kind = 'mr''''\n"


def _write(tmp_path: Path, body: str) -> str:
    payload = tmp_path / "p.toml"
    payload.write_text(body.format(p='"x.py"'), encoding="utf-8")
    return "@" + str(payload)


def test_the_correct_spelling_loads(tmp_path: Path) -> None:
    """The value ends with `'` — and that is legal, so it must not be refused.

    This is the regression guard against the fix the issue proposed.
    """
    loaded = supertool._load_at_file(_write(tmp_path, CORRECT))
    assert loaded["new"] == "    kind = 'mr'"


@pytest.mark.usefixtures("no_tomllib")
def test_the_correct_spelling_loads_without_tomllib(tmp_path: Path) -> None:
    """The recommended spelling has to work on the parser below 3.11 too, or
    the refusal message is advice that fails on a third of the matrix."""
    loaded = supertool._load_at_file(_write(tmp_path, CORRECT))
    assert loaded["new"] == "    kind = 'mr'"


def test_backslash_before_the_closing_run_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(_write(tmp_path, BROKEN))
    message = str(excinfo.value)
    assert "backslash" in message.lower()
    assert "escape" in message.lower()


def test_the_refusal_names_both_alternative_spellings(tmp_path: Path) -> None:
    """A refusal is only legitimate here because every intent behind the
    backslash has another spelling. The message has to carry both, or the
    reader is left holding a rejection and no way forward."""
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(_write(tmp_path, BROKEN))
    message = str(excinfo.value)
    assert "'''    kind = 'mr''''" in message, "the trailing-quote spelling"
    assert '"""' in message and "\\\\" in message, "the doubled-backslash route"


@pytest.mark.usefixtures("no_tomllib")
def test_backslash_before_the_closing_run_is_refused_without_tomllib(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(_write(tmp_path, BROKEN))
    assert "backslash" in str(excinfo.value).lower()


def test_a_backslash_elsewhere_in_the_block_is_untouched(tmp_path: Path) -> None:
    """The guard is about the closing run. A literal block is the documented
    home for backslashes and must stay that way."""
    payload = tmp_path / "p.toml"
    payload.write_text(
        "path = 'x.py'\nnew = '''C:\\Users\\dev" + chr(10) + "re.sub(r'\\d+', '')'''\n",
        encoding="utf-8",
    )
    loaded = supertool._load_at_file("@" + str(payload))
    assert loaded["new"].startswith("C:\\Users\\dev")
    assert loaded["new"].endswith("re.sub(r'\\d+', '')")


def test_the_op_refuses_instead_of_writing(tmp_path: Path) -> None:
    """End to end: the reported payload must not reach the file.

    The issue's whole cost is that the write went through and the breakage
    surfaced two layers away, in another language's parser.

    The target is a `.txt` deliberately, in both end-to-end tests. A `.py`
    target would let the syntax validator roll the write back and the test
    would pass on a green board with the guard deleted — it has to be the
    guard that stops this, on a file no validator has an opinion about.
    """
    target = tmp_path / "x.txt"
    target.write_text("OLD\n", encoding="utf-8")
    payload = tmp_path / "p.toml"
    payload.write_text(
        BROKEN.format(p='"' + str(target).replace("\\", "\\\\") + '"'),
        encoding="utf-8",
    )
    out = supertool.dispatch("edit:@" + str(payload))
    assert "ERROR" in out
    assert "backslash" in out.lower()
    assert target.read_text(encoding="utf-8") == "OLD\n", "the file was written"


def test_the_correct_spelling_still_writes(tmp_path: Path) -> None:
    """The other half of the trade: the guard must not cost the legal payload."""
    target = tmp_path / "x.txt"
    target.write_text("OLD\n", encoding="utf-8")
    payload = tmp_path / "p.toml"
    payload.write_text(
        CORRECT.format(p='"' + str(target).replace("\\", "\\\\") + '"'),
        encoding="utf-8",
    )
    out = supertool.dispatch("edit:@" + str(payload))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == "    kind = 'mr'\n"
