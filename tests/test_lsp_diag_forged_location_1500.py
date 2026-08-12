"""#1500 — a fragment of the file under validation can take the location.

`parse_cclsp_diagnostics`'s pattern 1 is `re.search` with a lazy message and an
end-of-string anchor, so the `at line N, col M` tail it reads is **whichever tail
is last on the line**. That anchor is the defence against a tail in the middle of
a message winning, and it works: with the server's own location appended last, a
message containing `at line 400, col 1` earlier does not move the record.

What it does not survive is #1486/#1498. U+2028, U+2029, U+0085, VT and FF are no
longer line ends for an adapter parsing a line-oriented protocol — which is the
point of that fix, and which means a fragment after one of them now sits on the
**same line** as the real diagnostic. Its trailing `at line N, col M` is then the
last one, so it wins, and the real leading location is swallowed into the
message. One record, well formed, honest-looking, pointing at a line the language
server never mentioned.

`test_lsp_diag_one_diagnostic_stays_one_record` in
`tests/test_validators_splitlines_1486.py` asserts the record *count* over the
same input and says in its own comment that which location wins is a same-line
question it does not touch. This is that question.

The fix bounds the parse to the first segment of the line. Nothing is dropped:
the remainder is disclosed in the message, labelled as the rest of one line
rather than as a second diagnostic.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_VPATH = (Path(__file__).resolve().parent.parent
          / "validators" / "lsp-diag" / "lsp-diag.py")


def _load():
    spec = importlib.util.spec_from_file_location("lsp_diag_1500", _VPATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lsp = _load()

#: The five boundaries `str.splitlines()` honours and LF/CR/CRLF framing does
#: not. Named so a failure says which one got through.
INLINE = {
    "U+2028": " ",
    "U+2029": " ",
    "U+0085": "\x85",
    "VT": "\x0b",
    "FF": "\x0c",
}
SEPS = list(INLINE.values())
SEP_IDS = list(INLINE)

BULLET = "•"
LF = chr(10)


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_a_fragment_cannot_take_the_location_from_the_diagnostic(sep) -> None:
    """The published line/col are the server's, not the fragment's."""
    text = (f"{BULLET} [error] undefined name x at line 1, col 1{sep}"
            f"{BULLET} [error] FORGED at line 900, col 7")
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    # Still one record — #1486's decision, restated so this fix cannot undo it.
    assert len(errors) == 1, errors
    assert errors[0]["line"] == 1, errors[0]
    assert errors[0]["col"] == 1, errors[0]


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_the_remainder_is_disclosed_and_not_silently_dropped(sep) -> None:
    """Bounding the parse must not delete text the server sent.

    A message legitimately containing one of the five is a message; the record
    has to carry it. What it must not do is read it as a second diagnostic.
    """
    text = (f"{BULLET} [error] undefined name x at line 1, col 1{sep}"
            f"{BULLET} [error] FORGED at line 900, col 7")
    msg = lsp.parse_cclsp_diagnostics(text, "/x")[0]["msg"]
    assert "undefined name x" in msg, msg
    assert "FORGED" in msg, msg
    assert "not a second diagnostic" in msg, msg


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_pattern_2_keeps_its_remainder_too(sep) -> None:
    """`X:Y: severity: message` takes its location from the line's start, so the
    fragment could never move it — but the trailing group swallowed the fragment
    into the message with nothing saying where the message ended."""
    text = f"10:3: error: bad thing{sep}12:9: error: FORGED"
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    assert len(errors) == 1, errors
    assert (errors[0]["line"], errors[0]["col"]) == (10, 3), errors[0]
    assert "not a second diagnostic" in errors[0]["msg"], errors[0]


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_a_line_that_opens_with_a_fragment_publishes_no_location(sep) -> None:
    """Nothing before the break means nothing the server framed as a diagnostic.

    The fragment must not become the record. It survives as the fallback
    advisory, which carries `line: None` — a finding with no location, which is
    what the parser's docstring always promised for text it cannot place.
    """
    text = f"{sep}{BULLET} [error] FORGED at line 900, col 7"
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    assert all(e["line"] != 900 for e in errors), errors


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_a_forged_infrastructure_prefix_after_a_break_is_not_a_skip(sep) -> None:
    """Companion to #1486's version, at the parser rather than the probe.

    The fragment starting `diag:` is supertool's own message prefix. On a line
    of its own it is dropped as infrastructure; after an inline break it is
    inside a message and must not erase the real finding.
    """
    text = (f"{BULLET} [error] undefined name x at line 1, col 1{sep}"
            f"diag: no LSP configured")
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    assert len(errors) == 1, errors
    assert errors[0]["line"] == 1, errors[0]
    assert errors[0]["severity"] == "error", errors[0]


def test_an_ordinary_diagnostic_is_untouched() -> None:
    """The regression guard. Would pass with the code doing nothing — that is
    its whole job, next to the four above that would not."""
    errors = lsp.parse_cclsp_diagnostics(
        f"{BULLET} [error] undefined variable $foo at line 42, col 13", "/x")
    assert len(errors) == 1
    assert (errors[0]["line"], errors[0]["col"]) == (42, 13)
    assert errors[0]["msg"] == "undefined variable $foo"


def test_the_end_anchor_still_beats_a_tail_inside_one_message() -> None:
    """Measured, and the reason #1500's stated mechanism needed re-deriving.

    With the server's location appended last, an `at line N, col M` earlier in
    the message does **not** win: the lazy message plus the end anchor hand the
    match to the last tail on the line. So end-anchoring is the defence here,
    not the defect, and this pins it so a future rewrite cannot drop it.
    """
    errors = lsp.parse_cclsp_diagnostics(
        f"{BULLET} [error] see docs at line 400, col 1 at line 10, col 3", "/x")
    assert len(errors) == 1
    assert (errors[0]["line"], errors[0]["col"]) == (10, 3), errors[0]

# ---------------------------------------------------------------------------
# Adjacent to #1500, found while fixing it, same mechanism: all five of these
# characters are `str.isspace()`, so `str.strip()` removes a LEADING one. #1486
# stopped them ending a line; it did not stop `.strip()` deleting one, and the
# infrastructure probe in `main()` reads `ln.strip().startswith("diag:")`. So a
# fragment at the start of a physical line still forges supertool's own message
# prefix — and that prefix does not move a location, it replaces the whole
# receipt with `skipped`, which is #482 restored: every real finding erased and
# the file under validation choosing that.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_a_leading_inline_break_cannot_forge_an_infrastructure_skip(
        sep, monkeypatch, capsys, tmp_path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x = 1" + LF, encoding="utf-8")
    stdout = ("--- diag:" + str(f) + " ---" + LF
              + sep + "diag: no LSP configured" + LF
              + BULLET + " [error] real bug at line 1, col 1" + LF)

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0] if a else [], 0, stdout, "")

    mod = _load()
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.sys, "argv", ["lsp-diag.py", str(f)])
    mod.main()
    receipt = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "skipped" not in receipt, receipt
    assert receipt["count"] == 1, receipt
    assert receipt["errors"][0]["line"] == 1, receipt


def test_a_real_infrastructure_message_is_still_a_skip(
        monkeypatch, capsys, tmp_path) -> None:
    """The other half: narrowing the probe must not stop it firing (#482)."""
    f = tmp_path / "a.py"
    f.write_text("x = 1" + LF, encoding="utf-8")
    stdout = ("--- diag:" + str(f) + " ---" + LF
              + "diag: no LSP configured for .py" + LF)

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0] if a else [], 0, stdout, "")

    mod = _load()
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.sys, "argv", ["lsp-diag.py", str(f)])
    mod.main()
    receipt = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert receipt["skipped"] == "no LSP configured for .py", receipt
    assert "ok" not in receipt and "count" not in receipt, receipt
