r"""A whole-value guard anchored with `$` accepts a trailing newline (#1188).

Python's `$` matches at the end of the string **and** immediately before a
final newline, so `re.compile(r"^[0-9]+$").match("5\n")` is a match. A pattern
written `^...$` and asked "is this whole value acceptable?" therefore answers
yes to a value nobody meant to allow, and it answers yes silently — the
newline is invisible in every render of the value that follows.

The audit named four sites. Four tests each pinning one site is how the fifth
ships, so the pin here is the *class*: every fully anchored regex literal in
the trees that decide whether a caller-supplied or forge-supplied value is
acceptable has to end with `\Z`, or carry an `# anchored-ok: <why>` waiver on
its own call.

**What is scanned, and what is not.** `_supertool.py`, `presets/` and
`.github/scripts/` — the code that reads values from a caller, a forge or a
filename. `validators/` is out: those modules parse the stdout of an external
tool line by line, where `$` meaning "end of this line" is the intent rather
than the bug, and a scan over them would be an allowlist of nothing but that
one sentence. `tests/` is out for the same reason.

**Two things skipped by construction.** A pattern compiled with `re.MULTILINE`
is a line scanner and `\Z` would be wrong in it. A pattern that ends with `$`
but does not start with `^` is a suffix test (`\.pem$`), where matching before
a trailing newline changes nothing a caller can act on.

**The limit, stated.** This reads the literal first argument of a call spelled
`re.<something>`. Four shapes are therefore invisible to it: a pattern built by
an f-string or `+`, one assembled from a variable, one held in a dict and
compiled elsewhere, and one reached through an aliased import
(`from re import compile as _c`). None exists in the tree today. This narrows
the class; it does not close it.

`fullmatch` is deliberately **not** in the call list. `re.fullmatch(r"^x$", s)`
requires the whole string, so it has never had this bug, and flagging one would
attach a true-sounding reason to a pattern that does not carry the defect —
which is the shape of mistake this file exists to catch.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "presets"))

import _job_argv  # noqa: E402
import _refname  # noqa: E402

#: Files and trees whose regexes decide whether a *value* is acceptable.
SCANNED = ("_supertool.py", "presets", ".github/scripts")

#: The escape hatch, and it has to say why. Written on the line the `re.*`
#: call starts on, so it is read by whoever next edits that call rather than
#: living in a table in this file that nobody opens.
WAIVER = re.compile(r"#\s*anchored-ok:\s*(\S.*)")

#: No `fullmatch` — see the docstring; it requires the whole string and so
#: never accepts a trailing newline the pattern did not ask for.
_RE_CALLS = frozenset((
    "compile", "match", "search", "sub", "subn",
    "split", "findall", "finditer",
))

#: A `$` at the very end of the pattern that is not itself escaped. The
#: even-backslash run is what tells `\\$` (a literal backslash, then the
#: anchor) apart from `\$` (a literal dollar, and no anchor at all).
_TRAILING_ANCHOR = re.compile(r"(?<!\\)(?:\\\\)*\$\Z")


def _python_sources() -> list[Path]:
    out: list[Path] = []
    for entry in SCANNED:
        target = ROOT / entry
        if target.is_file():
            out.append(target)
        elif target.is_dir():
            out.extend(sorted(target.rglob("*.py")))
    return out


def _anchored_sites(path: Path) -> list[tuple[int, str, str | None]]:
    """`(lineno, pattern, waiver_reason)` for each fully anchored literal."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    found: list[tuple[int, str, str | None]] = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in _RE_CALLS
                and getattr(func.value, "id", None) == "re" and node.args):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        pattern = first.value
        if not pattern.startswith("^"):
            continue
        if not _TRAILING_ANCHOR.search(pattern):
            continue
        flags = " ".join(
            [ast.unparse(a) for a in node.args[1:]]
            + [ast.unparse(k.value) for k in node.keywords]
        )
        if re.search(r"\bre\.(MULTILINE|M)\b", flags):
            continue
        span = lines[node.lineno - 1:(node.end_lineno or node.lineno)]
        reason = None
        for line in span:
            hit = WAIVER.search(line)
            if hit:
                reason = hit.group(1).strip()
                break
        found.append((node.lineno, pattern, reason))
    return found


def test_no_fully_anchored_dollar_guard() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        for lineno, pattern, reason in _anchored_sites(path):
            if reason:
                continue
            rel = path.relative_to(ROOT).as_posix()
            offenders.append(f"{rel}:{lineno}: {pattern!r}")
    assert not offenders, (
        "These patterns are anchored ^...$ and used as whole-value tests, so "
        "each one accepts its own value with a trailing newline appended — "
        "Python's $ matches before a final newline (#1188).\n"
        + "\n".join(offenders)
        + "\n\nEnd the pattern with \\Z instead, or — if the value really is "
        "one line out of a larger text and the newline is the delimiter rather "
        "than smuggled input — append `# anchored-ok: <why>` to the line the "
        "re. call starts on."
    )


def test_every_waiver_says_why() -> None:
    """A waiver with no reason is a suppression, and reads as a decision."""
    silent: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "anchored-ok" in line and not WAIVER.search(line):
                silent.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}")
    assert not silent, (
        "`# anchored-ok` with no reason after the colon:\n" + "\n".join(silent)
    )


@pytest.mark.parametrize("job_id", ["5\n", "5\r\n", "12\n"])
def test_job_id_guard_refuses_a_trailing_newline(job_id: str) -> None:
    """#1145's guard runs before anything is fetched; that is its whole job."""
    assert _job_argv.refuse_job_id("gh-job", "GitHub", job_id) != ""


def test_job_id_guard_still_accepts_a_plain_id() -> None:
    assert _job_argv.refuse_job_id("gh-job", "GitHub", "5") == ""


def test_ordinary_refuses_a_ref_ending_in_a_newline() -> None:
    assert _refname.ordinary("main\n") is False


def test_shell_ref_never_prints_a_live_line_break() -> None:
    """The printed command is run by the reader, so the newline lands in a shell."""
    quoted = _refname.shell_ref("main\n")
    assert quoted != "main\n"
    assert quoted.startswith("'") and quoted.endswith("'")


def test_shell_ref_still_prints_an_ordinary_name_bare() -> None:
    assert _refname.shell_ref("feature/fix-1188") == "feature/fix-1188"


def test_warning_names_a_newline_bearing_ref() -> None:
    assert _refname.warning(["main\n"]) is not None
