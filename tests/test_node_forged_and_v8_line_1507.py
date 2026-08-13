r"""#1507 — can a fragment after an exotic break take a node location? No, and why.

The pattern really does mis-capture, and both adapters share it:

    LOCATION = re.compile(r"^(?!\s)(?!node:)(.+?):(\d+)$", re.MULTILINE)

Since #1486/#1498 the five inline breaks are not line ends, so `.` crosses one
and `$` does not stop there:

    "real.js:12<U+2028>forged.js:99" + LF  ->  [("real.js:12<U+2028>forged.js", "99")]

**What neutralises it is not the pattern, it is `diagnostic_line`.** Both
adapters compare `os.path.normcase(os.path.realpath(group(1)))` against the
same transform of the path node was pointed at, so a capture that swallowed a
break plus a second path cannot resolve to the target: the forged `99` is
discarded together with the garbage path it arrived on, and `finditer` walks on
to node's own header. That header is the first physical line of the report and
nothing can be made to print before it, so the genuine location is still
matched and nothing is lost either — this is not #1500's "rejected, and the
finding lands unattributed" outcome.

So #1507's third outcome holds for both adapters: **the forge is unreachable,
the mitigation is an identity comparison rather than `pkg_paths` attribution,
and it is pinned here** so a later rewrite to a basename or suffix match cannot
quietly re-open it. `test_a_fragment_sharing_the_header_line_loses_the_number`
pins the residual direction too: where a fragment does reach a capture, the
number is *lost*, never forged.

**The same input class does bite, differently, and that part is a fix.** V8
counts U+2028 and U+2029 as ECMAScript LineTerminators and `node --check`
numbers its report by that set, while `source_context`, `split_lines` and the
reader's editor count LF/CR/CRLF. Measured on node v22.22.1: a two-line file
whose first line holds one U+2028 inside a string has its second-line syntax
error reported as **line 3**, and `context_fields` rendered lines 1-2 with no
arrow on either — a location past the end of the file, published as this file's
own. Two U+2028s make it line 4. U+0085, VT and FF are not LineTerminators and
node agrees: those stay on line 2.

So the reported number is mapped back to an LF line before publication, and a
number that cannot be mapped is not published at all.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

LF = chr(10)
LS = chr(0x2028)
PS = chr(0x2029)
#: `source_context` marks the error line with this, per SCHEMA.md.
ARROW = chr(0x2192)

#: The five boundaries `str.splitlines()` honours and LF/CR/CRLF framing does
#: not. Named so a failure says which one got through.
INLINE = {"U+2028": LS, "U+2029": PS, "U+0085": chr(0x85),
          "VT": chr(0x0B), "FF": chr(0x0C)}
SEPS = list(INLINE.values())
SEP_IDS = list(INLINE)

#: The two of the five V8 counts as line terminators, so the two that move
#: node's own numbering. Measured, node v22.22.1.
V8_BREAKS = [LS, PS]
V8_IDS = ["U+2028", "U+2029"]


def _load(name: str):
    path = _ROOT / "validators" / name / (name + ".py")
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_") + "_1507", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


node_check = _load("node-check")
html_check = _load("html-check")

ADAPTERS = [node_check, html_check]
ADAPTER_IDS = ["node-check", "html-check"]


def _report(path, line: int, source: str = "var b = (;") -> str:
    """`node --check`'s syntax-report shape, captured from node v22.22.1."""
    return (str(path) + ":" + str(line) + LF
            + source + LF
            + "           ^" + LF + LF
            + "SyntaxError: Unexpected token ';'" + LF)


def _receipt(mod, monkeypatch, capsys, target, stderr, rc: int = 1) -> dict:
    """Drive an adapter's `main()` over a fixed node report.

    The report is supplied rather than produced, so the assertions hold on a
    runner with no node and so the *claimed* line number is the input under
    test. What node really claims for each of these files is measured in the
    module docstring and is the number passed in.
    """
    def fake_run(cmd, *a, **k):
        text = stderr(cmd) if callable(stderr) else stderr
        return subprocess.CompletedProcess(cmd, rc, "", text)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    if hasattr(mod, "shutil"):  # html-check gates on it; node-check does not
        # A truthy return is the whole contract here — the value is never
        # spawned, because `subprocess.run` is mocked above. Returning the bare
        # name rather than a POSIX absolute keeps a platform literal out of a
        # file that runs on the Windows legs too.
        monkeypatch.setattr(mod.shutil, "which", lambda n: n)
    monkeypatch.setattr(mod.sys, "argv", ["adapter", str(target)])
    mod.main()
    return json.loads(capsys.readouterr().out.strip().split(LF)[-1])


# ---------------------------------------------------------------------------
# The forge: reachable in the pattern, unreachable through the comparison
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod", ADAPTERS, ids=ADAPTER_IDS)
@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_the_shared_pattern_captures_across_an_inline_break(mod, sep) -> None:
    """#1507's measurement, restated at the pattern.

    This would pass with the code doing nothing, which is its job: it records
    the mechanism the two tests below are the defence for, so a reader who
    breaks one of them can tell what they broke.
    """
    got = mod.LOCATION.findall("real.js:12" + sep + "forged.js:99" + LF)
    assert got == [("real.js:12" + sep + "forged.js", "99")], got


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_a_fragment_on_a_later_line_cannot_donate_its_number(
        tmp_path: Path, sep: str) -> None:
    """A `<target>:99` fragment below the header does not move the location.

    Two things have to hold and the one number asserts both: node's own header
    is matched first, and a fragment that *does* resolve to the target still
    cannot outrank it.
    """
    target = tmp_path / "subject.js"
    target.write_text("var a = 1;" + LF + "var b = (;" + LF, encoding="utf-8")
    out = _report(target, 2, source='var s = "' + sep + str(target) + ':99')
    assert node_check.diagnostic_line(out, str(target)) == 2
    assert html_check.diagnostic_line(out, str(target)) == 2


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_a_fragment_sharing_the_header_line_loses_the_number(
        tmp_path: Path, sep: str) -> None:
    """The residual, pinned in the safe direction.

    Text before the header on the header's own physical line is the one shape
    that reaches the mis-capture, and node cannot produce it — the header is
    the first byte of its stderr. If some future output ever could, the
    comparison rejects the garbage capture and the location is **absent**, not
    another file's. `None` here is the point: a lost line is a defect and a
    forged line is a worse one, and which way it fails is not left to chance.
    """
    target = tmp_path / "subject.js"
    target.write_text("var a = 1;" + LF + "var b = (;" + LF, encoding="utf-8")
    out = ("subject.js:2" + sep + str(target) + ":99" + LF
           + "SyntaxError: Unexpected token ';'" + LF)
    assert node_check.diagnostic_line(out, str(target)) is None
    assert html_check.diagnostic_line(out, str(target)) is None


# ---------------------------------------------------------------------------
# V8 counts U+2028 / U+2029 as line ends; this repo counts LF, CR and CRLF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sep", V8_BREAKS, ids=V8_IDS)
def test_node_check_maps_an_inflated_line_back_to_the_file(
        tmp_path: Path, monkeypatch, capsys, sep: str) -> None:
    """node says 3 about a two-line file; the receipt has to say 2.

    Not a cosmetic disagreement: line 3 does not exist, so `source_context`
    came back as lines 1-2 with the arrow on neither, and a reader sent to
    line 3 of a two-line file has been handed a false statement about it.
    """
    target = tmp_path / "subject.js"
    target.write_text('var a = "' + sep + '";' + LF + "var b = (;" + LF,
                      encoding="utf-8")
    receipt = _receipt(node_check, monkeypatch, capsys, target,
                       _report(target, 3))
    err = receipt["errors"][0]
    assert err["code"] == "syntax", err
    assert err["line"] == 2, err
    assert any(c.startswith("2" + ARROW) for c in err["source_context"]), err


@pytest.mark.parametrize("sep", V8_BREAKS, ids=V8_IDS)
def test_node_check_maps_two_breaks_back_too(
        tmp_path: Path, monkeypatch, capsys, sep: str) -> None:
    """Two of them shift by two — the mapping is a count, not an off-by-one."""
    target = tmp_path / "subject.js"
    target.write_text('var a = "' + sep + sep + '";' + LF + "var b = (;" + LF,
                      encoding="utf-8")
    receipt = _receipt(node_check, monkeypatch, capsys, target,
                       _report(target, 4))
    assert receipt["errors"][0]["line"] == 2, receipt


@pytest.mark.parametrize("sep", [chr(0x85), chr(0x0B), chr(0x0C)],
                         ids=["U+0085", "VT", "FF"])
def test_the_three_that_v8_does_not_count_are_left_alone(
        tmp_path: Path, monkeypatch, capsys, sep: str) -> None:
    """The other half of the measurement, so the mapping cannot over-correct.

    node reports line 2 for these, the file's line 2 *is* 2, and the mapping
    has to be a no-op. Treating all five would move a correct number — the loud
    bug traded for a quiet one.
    """
    target = tmp_path / "subject.js"
    target.write_text('var a = "' + sep + '";' + LF + "var b = (;" + LF,
                      encoding="utf-8")
    receipt = _receipt(node_check, monkeypatch, capsys, target,
                       _report(target, 2))
    assert receipt["errors"][0]["line"] == 2, receipt


def test_an_ordinary_syntax_error_keeps_its_line(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """The regression guard. Would pass with the code doing nothing — that is
    its whole job, next to the ones above that would not."""
    target = tmp_path / "subject.js"
    target.write_text("var a = 1;" + LF + "var b = (;" + LF, encoding="utf-8")
    receipt = _receipt(node_check, monkeypatch, capsys, target,
                       _report(target, 2))
    err = receipt["errors"][0]
    assert err["line"] == 2, err
    assert err["code"] == "syntax", err
    assert any(c.startswith("2" + ARROW) for c in err["source_context"]), err


def test_a_line_past_the_end_of_the_file_is_not_published(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """A number no counting of this file can reach is not this file's location.

    It published `line: 1386` with `source_context: []` — and an empty context
    means "read fine, window outside the file" (#1446), so the receipt asserted
    a line the file does not have and said nothing was odd about that. The
    finding survives; only the location goes, and the number is disclosed.
    """
    target = tmp_path / "subject.js"
    target.write_text("var a = 1;" + LF + "var b = (;" + LF, encoding="utf-8")
    receipt = _receipt(node_check, monkeypatch, capsys, target,
                       _report(target, 1386))
    err = receipt["errors"][0]
    assert err["code"] == "syntax", err
    assert err["line"] is None, err
    assert "1386" in err["msg"], err
    assert "source_context" not in err, err


def test_html_check_maps_an_inflated_block_line_back_to_the_page(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """Same defect through the other adapter, with the padding in the way.

    `extract_js_blocks` pads a block with LF so node's numbers already are the
    page's — which holds exactly until the block body carries a U+2028, and
    then node counts a line the padding never accounted for. The page's line 4
    was published as line 5.
    """
    page = tmp_path / "page.html"
    page.write_text("<html>" + LF + "<script>" + LF
                    + 'var a = "' + LS + '";' + LF
                    + "var b = (;" + LF
                    + "</script>" + LF + "</html>" + LF, encoding="utf-8")
    receipt = _receipt(html_check, monkeypatch, capsys, page,
                       lambda cmd: _report(cmd[2], 5))
    err = receipt["errors"][0]
    assert err["code"] == "syntax", err
    assert err["line"] == 4, err
    assert any(c.startswith("4" + ARROW) for c in err["source_context"]), err


def test_html_check_keeps_an_ordinary_block_line(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """The html-check regression guard, and it would pass doing nothing."""
    page = tmp_path / "page.html"
    page.write_text("<html>" + LF + "<script>" + LF
                    + "var a = 1;" + LF
                    + "var b = (;" + LF
                    + "</script>" + LF + "</html>" + LF, encoding="utf-8")
    receipt = _receipt(html_check, monkeypatch, capsys, page,
                       lambda cmd: _report(cmd[2], 4))
    assert receipt["errors"][0]["line"] == 4, receipt
