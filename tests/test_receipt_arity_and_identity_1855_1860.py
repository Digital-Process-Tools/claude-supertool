r"""Two receipts that read as the opposite of what they mean (#1855, #1860).

**#1855 -- `nearest match at lines 114-125 (100%)` printed under `old string
not found`.** Two sentences a caller cannot both believe. Measured mechanism,
which the issue reasoned but did not observe: `difflib.SequenceMatcher.ratio()`
returns 0.998 for a twelve-line block differing by three trailing spaces, and
`f"{0.998:.0%}"` is `100%`. Nothing normalises anything -- the render rounds a
near-miss up into a claim of identity. So it is a RECEIPT defect and not a
matching defect, and the fix belongs in one message rather than in the matcher.
Asserted here in both directions: the percentage may never read as `100%` while
the strings differ, and the receipt must name WHICH KIND of difference, because
the reported cost was a caller who could not tell "you invented a line" from
"your indentation drifted".

**#1860 -- the doubled-backslash refusal fired on two and not on four.** The
guard's own stated premise ("each pair would reach disk as TWO backslashes,
pass every validator, and be wrong only in string contents") is true of four
exactly as it is of two. Two tests asserted the exemption on the ground that
"four was counted, not produced by escape reflex"; #1860 observed the opposite
twice, and named the mechanism the original rationale could not have had --
four is what a caller writes immediately AFTER reading the two-backslash
refusal and doubling again to escape the escape. The refusal manufactures its
own blind spot. Those two tests are rewritten in place rather than deleted.

**Every "must refuse" case here is paired with a "must write" case in the same
fixture.** A silence assertion passes when the harness is broken, so the odd
arities are not merely controls for the rule -- they are the proof that this
file can write a file at all.
"""
from pathlib import Path

import supertool

BS = chr(92)
NL = chr(10)
Q3 = chr(39) * 3


def _payload(tmp_path: Path, body: str) -> str:
    p = tmp_path / "p.toml"
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(BS, BS * 2) + chr(34)


def _paste(tmp_path: Path, run: int) -> tuple:
    """Write `x <run backslashes> y` through `paste`, return (output, on-disk)."""
    target = tmp_path / ("t%d.txt" % run)
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + Q3 + "x " + BS * run + " y" + Q3 + NL
    )
    out = supertool.dispatch("paste:" + _payload(tmp_path, body))
    disk = target.read_text(encoding="utf-8") if target.exists() else None
    return out, disk


# --------------------------------------------------------------------------
# #1860: the guard reaches every EVEN run, and no odd one.
# --------------------------------------------------------------------------

def test_an_even_run_of_four_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    """The issue, in one call. Before this change the file was created and the
    receipt said `created` -- a payload the guard does not reach and a payload
    it examined and passed were indistinguishable from outside."""
    out, disk = _paste(tmp_path, 4)
    assert "ERROR" in out, out
    assert "refused" in out.lower(), out
    assert disk is None, "a refused paste created the file, holding " + repr(disk)


def test_an_even_run_of_six_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    """Not special-cased at four. The premise is about doubling, and six is a
    doubled three exactly as four is a doubled two."""
    out, disk = _paste(tmp_path, 6)
    assert "ERROR" in out, out
    assert disk is None, "a refused paste created the file, holding " + repr(disk)


def test_a_run_of_two_is_still_refused(tmp_path: Path) -> None:
    """The case that already worked. Widening a rule is the move that quietly
    drops the case it was written for."""
    out, disk = _paste(tmp_path, 2)
    assert "ERROR" in out, out
    assert disk is None, out


def test_a_single_backslash_still_writes_unrefused(tmp_path: Path) -> None:
    """The control that stops the fix being "refuse every backslash", which
    would make the payload route unusable for the regex and Windows-path cases
    the remedy explicitly sanctions -- AND the positive half of every silence
    assertion above: if the harness could not write at all, each `disk is None`
    would pass for the wrong reason."""
    out, disk = _paste(tmp_path, 1)
    assert "ERROR" not in out, out
    assert disk == "x " + BS + " y" + NL, repr(disk)


def test_an_odd_run_of_three_still_writes_unrefused(tmp_path: Path) -> None:
    """Three is never what a doubling reflex produces -- doubling one gives two
    and doubling two gives four. The rule is evenness, not length, and this is
    the case that tells those two rules apart."""
    out, disk = _paste(tmp_path, 3)
    assert "ERROR" not in out, out
    assert disk == "x " + BS * 3 + " y" + NL, repr(disk)


def test_the_refusal_names_the_run_length_it_found(tmp_path: Path) -> None:
    """A receipt that says `\\\\` while refusing a run of four sends the reader
    to look for a pair that is not there. The count of characters is the fact
    the caller acts on."""
    out, _disk = _paste(tmp_path, 4)
    assert "4" in out, "the run length is not in the refusal: " + out
    assert BS * 4 in out, "the offending run is not quoted back: " + out


def test_the_optin_still_suppresses_a_widened_refusal(tmp_path: Path) -> None:
    """Widening an unsuppressible refusal would strand every correct caller.
    The whole reason the refusal may widen is that `literal_backslashes` is the
    recorded decision, so it must reach the arities that are new here."""
    target = tmp_path / "q.txt"
    body = (
        "literal_backslashes = true" + NL
        + "path = " + _toml_path(target) + NL
        + "content = " + Q3 + "x " + BS * 4 + " y" + Q3 + NL
    )
    out = supertool.dispatch("paste:" + _payload(tmp_path, body))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == "x " + BS * 4 + " y" + NL


def test_old_gets_the_note_at_four_as_it_does_at_two(tmp_path: Path) -> None:
    """The recorded decision on #1860's adjacent gap. `old` stays a NOTE rather
    than a refusal -- it is an anchor, a doubled one cannot match, and nothing
    reaches disk -- but the note's reach is the scanner's, so widening the
    scanner must carry `old` with it. Silently leaving `old` at two would put
    the same blind spot one field over."""
    target = tmp_path / "t.py"
    target.write_text("PAT = 1" + NL, encoding="utf-8")
    body = (
        "path = " + _toml_path(target) + NL
        + "old = " + Q3 + "PAT = " + BS * 4 + "d" + Q3 + NL
        + "new = " + Q3 + "PAT = 2" + Q3 + NL
    )
    out = supertool.dispatch("edit:" + _payload(tmp_path, body))
    assert "literal block" in out.lower(), (
        "no note for a doubled run in `old`: " + out)
    assert "payload refused" not in out.lower(), (
        "`old` was refused rather than noted: " + out)


# --------------------------------------------------------------------------
# #1855: a percentage may not read as identity while the strings differ.
# --------------------------------------------------------------------------

def _block(n: int = 12) -> list:
    return ["    value_%d = compute_with_a_fairly_long_name(%d, %d)" % (i, i, i)
            for i in range(n)]


def _file(body: list) -> list:
    return (["header %d" % i for i in range(113)] + body
            + ["tail %d" % i for i in range(20)])


def test_a_near_miss_never_renders_as_one_hundred_percent() -> None:
    """The issue. `ratio()` is 0.998 for this pair and `.0%` rounded it to
    `100%`, printed directly under `old string not found`."""
    body = _block()
    anchor = list(body)
    anchor[3] = anchor[3] + "   "
    hint = supertool._edit_nearest_hint(NL.join(anchor), _file(body), "f.py")
    assert hint, "no hint at all"
    assert "100%" not in hint, (
        "a differing anchor is reported as a 100% match: " + hint)


def test_a_near_miss_names_what_kind_of_difference_it_is() -> None:
    """The reported cost was one extra read call spent finding a one-line
    difference the receipt had already measured and thrown away. Naming the
    class is what turns the hint from a location into an answer."""
    body = _block()
    anchor = list(body)
    anchor[3] = anchor[3] + "   "
    hint = supertool._edit_nearest_hint(NL.join(anchor), _file(body), "f.py")
    assert "whitespace" in hint.lower(), (
        "a whitespace-only near miss is not named as one: " + hint)


def test_a_line_difference_is_named_differently_from_a_whitespace_one() -> None:
    """The pair the issue asked for. Either message alone is satisfiable by
    printing one constant string, so the two are asserted to DIFFER -- that is
    the assertion a do-nothing implementation cannot pass."""
    body = _block()
    ws = list(body)
    ws[3] = ws[3] + "   "
    # SUBSTITUTED, not inserted, so both anchors are the same height and land
    # on the same line range. Otherwise the two receipts differ in their line
    # numbers alone and the assertion passes without either naming anything --
    # which is what an earlier draft of this test did.
    swapped = list(body)
    swapped[6] = "    # a line that is not in the file"
    hint_ws = supertool._edit_nearest_hint(NL.join(ws), _file(body), "f.py")
    hint_line = supertool._edit_nearest_hint(NL.join(swapped), _file(body), "f.py")
    assert "114-125" in hint_ws and "114-125" in hint_line, (
        "the two anchors did not land on the same range, so any difference "
        "below could be the line numbers: " + hint_ws + " / " + hint_line)
    assert hint_ws != hint_line, (
        "whitespace drift and a substituted line produce the same receipt, "
        "character for character: " + hint_ws)
    assert "whitespace" not in hint_line.lower(), (
        "a substituted line is reported as whitespace: " + hint_line)
    # The count, not merely the word "line" -- the hint already says "at lines
    # 114-125", so asserting on the word alone passes on today's code and
    # measures nothing. One line of twelve is the fact the caller acts on.
    assert "1 of 12" in hint_line, (
        "the receipt does not say how many lines differ: " + hint_line)


def test_a_distant_match_still_reports_its_real_percentage() -> None:
    """The control against fixing this by deleting the number. A genuine 60-90%
    near miss must still print its own figure, not a capped one."""
    body = _block()
    anchor = [ln.replace("value", "other").replace("compute", "derive")
              for ln in body[:6]] + body[6:]
    hint = supertool._edit_nearest_hint(NL.join(anchor), _file(body), "f.py")
    assert hint, "no hint for a genuine near miss"
    assert "%" in hint, "no percentage in the hint at all: " + hint
    figure = hint.split("(", 1)[1].split("%", 1)[0]
    assert figure.isdigit() and 60 <= int(figure) < 100, (
        "the real figure was replaced by a capped or missing one: " + hint)
    assert "100%" not in hint, hint


def test_a_true_hundred_percent_says_why_it_is_still_not_a_match() -> None:
    """The third state, and the one the issue is really about.

    `splitlines()` drops line endings and the hint strips blank lines off both
    ends of the anchor, so the compared LINES can be equal while the byte
    comparison that refused the edit was not. The percentage is then a true
    100 -- and a bare `100%` printed under `old string not found` is precisely
    the self-contradiction #1855 filed. Capping the number here would be a lie
    in the other direction, so the receipt keeps the 100 and says what it is a
    100 OF."""
    body = _block()
    # Blank lines the file does not have: stripped from the anchor, so the
    # compared windows are equal line-for-line.
    anchor = [""] + list(body) + [""]
    hint = supertool._edit_nearest_hint(NL.join(anchor), _file(body), "f.py")
    assert hint, "no hint at all"
    assert "identical line-for-line" in hint, (
        "a genuine 100% is printed bare under `old string not found`: " + hint)
    assert "line endings" in hint, hint


def test_an_exact_anchor_still_edits(tmp_path: Path) -> None:
    """The control the issue named: the receipt cannot be made consistent by
    weakening the comparison. An anchor that IS in the file must still match,
    write, and produce no nearest-match hint at all."""
    target = tmp_path / "t.py"
    body = _block()
    target.write_text(NL.join(_file(body)) + NL, encoding="utf-8")
    payload = (
        "path = " + _toml_path(target) + NL
        + "old = " + Q3 + NL.join(body) + Q3 + NL
        + "new = " + Q3 + "REPLACED" + Q3 + NL
    )
    out = supertool.dispatch("edit:" + _payload(tmp_path, payload))
    assert "ERROR" not in out, out
    assert "nearest match" not in out, out
    assert "REPLACED" in target.read_text(encoding="utf-8")
