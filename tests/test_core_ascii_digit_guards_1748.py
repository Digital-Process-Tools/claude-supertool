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

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
ENTRY = ROOT / "supertool.py"
CORE = ROOT / "_supertool.py"

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


# --- recurrence guard ------------------------------------------------------

def _isdigit_code_lines(text: str) -> list[str]:
    """Code lines only. Prose in this repo quotes `str.isdigit()` in backticks
    to say why it is wrong, and a scan that forbade the word would forbid the
    explanation. Same reader as `tests/test_ascii_digit_guards_1727.py`, on
    purpose: two spellings of one scan is what #1727 was filed about."""
    offenders: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "`" in line:
            continue
        if ".isdigit(" in line or ".isnumeric(" in line:
            offenders.append("%d: %s" % (n, stripped))
    return offenders


def test_the_recurrence_scan_can_actually_see_one() -> None:
    """The positive control for the negative assertion below. Synthetic rather
    than a second real file, because this proves the reader this module ships
    rather than a fact about somebody else's tree."""
    seen = _isdigit_code_lines(
        "if x.isdigit():\n# a `x.isdigit()` mention\nno digits here\n")
    assert seen == ["1: if x.isdigit():"], seen


def test_the_core_does_not_reach_for_isdigit_again() -> None:
    text = CORE.read_text(encoding="utf-8")
    assert len(text.splitlines()) > 20000, (
        "read %d lines of the core — the scan below proves nothing if the "
        "file did not arrive" % len(text.splitlines()))
    offenders = _isdigit_code_lines(text)
    assert offenders == [], "\n".join(offenders)
