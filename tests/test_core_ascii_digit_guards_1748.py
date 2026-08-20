"""#1748 — the core's own ASCII-digit test, on every path a caller can reach.

#1727 replaced `str.isdigit()` with an anchored ASCII test at 11 sites under
`presets/`. `_supertool.py` was outside that sweep and held 36 more, so the two
classes #1727 names were still live in the core:

* **Unicode decimals** — U+0662 ARABIC-INDIC DIGIT TWO and its family.
  `str.isdecimal()` is True too, so `int()` converts them and the op proceeds
  against a number the caller never typed. In the core this is the expensive
  half: `around:def:F:<U+0662>` rendered a 2-line window, `grep:PAT:F:<U+0662>`
  a 2-result limit, and `vim:F:<U+0662>jx` **wrote to the file** and printed
  `1. 2j` back — a receipt the caller cannot match to what they typed.
* **Superscripts** — U+00B2 SUPERSCRIPT TWO and its family. `str.isdigit()` is
  True and `str.isdecimal()` is False, so `int()` raises. The core's dispatch
  catch-all turns that into `ERROR: argument parsing: invalid literal for int()
  with base 10`, which is a refusal — but one that names an interpreter builtin
  rather than the argument slot, and only by accident of a catch-all two
  thousand lines away.

Every case below is a triple, and the triple is the point:

* the U+00B2 call must not report `invalid literal for int()` — proves the raise
  is gone;
* the U+0662 call must not produce the same output as the `2` call — proves the
  value stopped being silently read as a number, which no crash test can see;
* the `2` call must succeed — the positive control, without which both halves
  above pass against a broken harness, an unresolvable path or a process that
  died before it spoke.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent
ENTRY = ROOT / "supertool.py"
CORE = ROOT / "_supertool.py"
PRESETS = ROOT / "presets"


def _load(rel: str, name: str) -> Any:
    """Load a preset module by path. The presets are not a package and run as
    standalone subprocesses, so this is the only way to reach one from here."""
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None, rel
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

#: U+00B2. `str.isdigit()` True, `str.isdecimal()` False -> `int()` raises.
SUP = "²"
#: U+0662 ARABIC-INDIC DIGIT TWO. Both True -> `int()` returns 2 in silence.
DEC = "٢"

PROBE = "alpha\nbravo\ncharlie\ndef target\ndelta\necho\nfoxtrot\ngolf\n"

#: `{n}` is the slot a caller types a count into. Each of these reaches a
#: separate guard in `_supertool.py`; the mapping is in the fix's changelog
#: entry, not repeated here where it would go stale.
OP_TEMPLATES = [
    "around:target:probe.txt:{n}",
    "around:target:{n}",
    "grep:target:probe.txt:{n}",
    "between:probe.txt:{n}:3",
    "vim:probe.txt:{n}j",
    "vim:probe.txt:>{n}j",
]


@pytest.fixture()
def probe(tmp_path: Path) -> Path:
    (tmp_path / "probe.txt").write_text(PROBE, encoding="utf-8")
    return tmp_path


def _run(op: str, cwd: Path) -> tuple[int, str]:
    """Spawn the real entry point. In-process would skip the dispatch
    catch-all that turns the `ValueError` into the message under test.

    The leading `--- OP ---` header is dropped, and this is load-bearing rather
    than tidiness: it echoes the op string, so the U+0662 run and the `2` run
    differ on that line **whatever the code does**. Left in, the
    silently-read-as-two assertion below passed on unfixed code — the exact
    "would this still pass if the code did nothing" trap, caught by running the
    suite red before writing the fix.

    Both encoding pins are load-bearing on the Windows legs and neither is
    observable here. The child prints the refused argument back, so a U+00B2 in
    an error message meets the console codepage: cp1252 has no mapping for it
    and the `print` raises, after the refusal it was reporting already happened
    (#1388). And on the parent side a locale decode raises inside subprocess's
    reader thread, `communicate()` hands back None, and `proc.stdout` fails with
    a TypeError that names nothing (#856) -- which is what
    `tests/test_encoding_seam.py` caught in the first draft of this file."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(ENTRY), op], env=env,
        cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = proc.stdout
    if out.startswith("--- "):
        out = out.split("\n", 1)[1] if "\n" in out else ""
    return proc.returncode, out + proc.stderr


@pytest.mark.parametrize("template", OP_TEMPLATES)
def test_ascii_count_still_works(template: str, probe: Path) -> None:
    """The positive control. Asserted first and separately: if this fails, the
    two assertions below are measuring a harness, not a guard."""
    rc, out = _run(template.format(n="2"), probe)
    assert "invalid literal for int()" not in out, out
    assert "Traceback" not in out, out


@pytest.mark.parametrize("template", OP_TEMPLATES)
def test_superscript_does_not_reach_int(template: str, probe: Path) -> None:
    rc, out = _run(template.format(n=SUP), probe)
    assert "invalid literal for int()" not in out, (
        "%r still reaches int() with a superscript:\n%s"
        % (template.format(n=SUP), out))
    assert "Traceback" not in out, out


@pytest.mark.parametrize("template", OP_TEMPLATES)
def test_unicode_decimal_is_not_silently_read_as_two(
        template: str, probe: Path) -> None:
    """The class no crash test can see. U+0662 converts to 2, so before the fix
    these two runs were byte-identical and the caller was never told."""
    _, ascii_out = _run(template.format(n="2"), probe)
    _, unicode_out = _run(template.format(n=DEC), probe)
    assert unicode_out != ascii_out, (
        "%r was honoured as if the caller had typed 2:\n%s"
        % (template.format(n=DEC), unicode_out))


def test_vim_does_not_edit_the_file_on_a_unicode_count(probe: Path) -> None:
    """The worst instance, kept as its own case because it is a write. A
    U+0662 count deleted a character three lines down and reported `2j`."""
    before = (probe / "probe.txt").read_text(encoding="utf-8")
    rc, out = _run("vim:probe.txt:" + DEC + "jx", probe)
    after = (probe / "probe.txt").read_text(encoding="utf-8")
    assert after == before, out
    assert rc != 0, out


# --- the core's claim about the presets ------------------------------------

def test_the_core_and_the_presets_hold_the_identical_digit_predicate() -> None:
    """`_supertool.py`'s comment states that `presets/_digits.py` "holds the
    identical pair" and is deliberately not imported, because presets run as
    standalone subprocesses with only `presets/` on their path. Nothing
    compared them (#1765).

    The duplication is deliberate and stays; what was missing is the pin. If
    the two drift, the core and a preset disagree about what a number is at
    two layers of one call, and no leg goes red. Mirrors
    `tests/test_mcp_autospawn_honoured_1743.py`, which pins the one other
    core/preset pair the same comment style claims.

    Compared as live objects rather than against a literal written twice:
    a third copy of the pattern here would be one more thing to keep in step,
    which is the defect and not the test for it.
    """
    import supertool  # noqa: PLC0415

    digits = _load("_digits.py", "st_digits_1765")
    assert supertool._ASCII_DIGITS.pattern == digits.DIGITS.pattern
    assert supertool._ASCII_DIGITS.flags == digits.DIGITS.flags

    # Both must actually be the ASCII test, not merely equal to each other:
    # two identically-wrong regexes would satisfy the assertions above.
    for probe in ("2", "1764"):
        assert supertool._ASCII_DIGITS.match(probe), probe
        assert digits.DIGITS.match(probe), probe
    # `ascii()` rather than `repr()`: these probes are U+0662 and U+00B2, and a
    # failure message is written with the console's codepage, not the source
    # file's. On a cp1252 console `repr()` would raise UnicodeEncodeError while
    # reporting the failure, replacing the assertion nobody can now read.
    for probe in (DEC, SUP, "2 ", "2" + chr(10), "", "1.0"):
        assert not supertool._ASCII_DIGITS.match(probe), ascii(probe)
        assert not digits.DIGITS.match(probe), ascii(probe)


# --- recurrence guard ------------------------------------------------------

def _isdigit_code_lines(text: str) -> list[str]:
    """Code lines only. Prose in this repo quotes `str.isdigit()` in backticks
    to say why it is wrong, and a scan that forbade the word would forbid the
    explanation. Same reader as `tests/test_ascii_digit_guards_1727.py`, on
    purpose: two spellings of one scan is what #1727 was filed about.

    `.isdecimal(` is in the list because it is the spelling that produces the
    defect, not merely a related one (#1764): it is True for U+0662 and
    `int()` converts it, so it is the expensive half of this module's own
    docstring. It was absent from both readers until #1764, which left the
    silent-conversion class the only one that could recur behind a green leg.
    """
    offenders: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "`" in line:
            continue
        if (".isdigit(" in line or ".isnumeric(" in line
                or ".isdecimal(" in line):
            offenders.append("%d: %s" % (n, stripped))
    return offenders


def test_the_recurrence_scan_can_actually_see_one() -> None:
    """The positive control for the negative assertion below. Synthetic rather
    than a second real file, because this proves the reader this module ships
    rather than a fact about somebody else's tree."""
    seen = _isdigit_code_lines(
        "if x.isdigit():\n# a `x.isdigit()` mention\nno digits here\n")
    assert seen == ["1: if x.isdigit():"], seen


def test_the_scan_sees_every_spelling_that_lets_a_non_ascii_digit_reach_int() -> None:
    """One case per predicate the scan must catch, plus the lines it must not.

    `str.isdecimal()` is the spelling that produces the expensive half this
    module's docstring describes, and it was the one the scan could not see
    (#1764): U+0662 is `isdecimal()` and `int()` converts it, so
    `if x.isdecimal(): int(x)` anywhere in the core is the silent-conversion
    defect again behind a green leg.

    The must-not-match half is the pair for the empty-list assertion below,
    which a reader that returned nothing would also satisfy.
    """
    for spelling in (".isdigit(", ".isnumeric(", ".isdecimal("):
        line = "if x%s):" % spelling
        assert _isdigit_code_lines(line) == ["1: " + line], spelling

    assert _isdigit_code_lines(
        "no digits here\n"
        "# a commented x.isdecimal() mention\n"
        "prose quoting `x.isdecimal()` in backticks\n") == []


def test_the_core_does_not_reach_for_isdigit_again() -> None:
    text = CORE.read_text(encoding="utf-8")
    assert len(text.splitlines()) > 20000, (
        "read %d lines of the core — the scan below proves nothing if the "
        "file did not arrive" % len(text.splitlines()))
    offenders = _isdigit_code_lines(text)
    assert offenders == [], "\n".join(offenders)
