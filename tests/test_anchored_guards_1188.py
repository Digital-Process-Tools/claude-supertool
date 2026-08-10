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

**The limit, stated -- and stated per site, not only here.** This reads the
first argument of a call spelled `re.<something>`: a string literal, or a `+`
splice whose first and last pieces are literals. Head and tail settle *which
anchor* ends the pattern; they do not on their own settle whether the run in
front of it swallows a newline, so a splice whose last literal is only the
anchor is reported as undecided rather than clean. Anything else -- a bare
name, a call result, an f-string -- is *declined by name*, and a pattern whose
head says `^` and whose tail cannot be read is a **failure**, not a skip.

That is the whole point: a scanner that read 80% of the patterns and reported
clean is what let `_PATH_TOK` sit three lines from a flagged twin, spliced
with `+` and invisible (#1241).

Measured on the tree at the time of writing: 111 calls in scope, 1 spliced and
read, 50 declined with a reason, 14 waived, 0 half-read, 0 with an undecidable
trailing run. Still open by construction: a pattern held in a dict and compiled
elsewhere, and one reached through an aliased import (`from re import compile
as _c`). This narrows the class; it does not close it.

`fullmatch` is deliberately **not** in the call list. `re.fullmatch(r"^x$", s)`
requires the whole string, so it has never had this bug, and flagging one would
attach a true-sounding reason to a pattern that does not carry the defect —
which is the shape of mistake this file exists to catch.
"""
from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "presets"))

import _job_argv  # noqa: E402
import _refname  # noqa: E402

#: Files and trees whose regexes decide whether a *value* is acceptable.
SCANNED = ("_supertool.py", "presets", ".github/scripts")

#: The escape hatch, and it has to say why. Written on the `re.*` call's own
#: lines, or on the comment block directly above it, so it is read by whoever
#: next edits that call rather than living in a table nobody opens. Above is
#: allowed because a trailing comment has about thirty columns to work with,
#: and a reason that does not fit is a reason that does not get written.
#:
#: Matched against a **comment token's own text**, and it has to *open* that
#: comment. A bare `"anchored-ok" in line` fails in both directions (#1241):
#: a comment explaining why the waiver was *declined* was read as a silent
#: waiver and demanded a reason for an exit nobody took -- punishing the one
#: thing most worth writing down -- while `# ... anchored-ok: x` buried
#: mid-sentence, or the token inside a string, would have granted one.
WAIVER = re.compile(r"#\s*anchored-ok\s*:\s*(\S.*)")
_WAIVER_OPENS = re.compile(r"#\s*anchored-ok\b")

#: No `fullmatch` -- see the docstring; it requires the whole string and so
#: never accepts a trailing newline the pattern did not ask for.
_RE_CALLS = frozenset((
    "compile", "match", "search", "sub", "subn",
    "split", "findall", "finditer",
))

#: What to say when `\Z` would not actually change anything. `$` is only half
#: the defect: `^...\s*$` and `^...\s*\Z` accept the same values, because the
#: trailing run eats the newline before either anchor is consulted. Measured
#: on `^(#{1,6})\s+(.*?)\s*$` in #1241 -- `$` matches `"## x\n"`, `\Z` matches
#: it too, `[ \t]*\Z` does not.
_NO_OP_ADVICE = (
    r"A `\Z` placed after a run that can itself swallow a newline -- `\s*`, "
    r"`\s+`, `[\s\S]*`, or `.*` under re.DOTALL -- is a no-op: the run eats "
    r"the newline and the anchor never sees it, so the pattern accepts exactly "
    r"what it accepted before and the guard turns green over an unchanged "
    r"defect. A waiver does the same. The fix is to narrow the trailing run to "
    r"the characters actually meant, `[ \t]*` rather than `\s*`."
)

#: An atom at the very end of the pattern body, quantified or not. The
#: quantifier is optional because `\s\Z` eats a trailing newline exactly as
#: `\s*\Z` does -- the `*` was never the defect. The `\]?` is Python's rule
#: that a `]` immediately after `[` or `[^` is a literal member, not the close.
_TRAILING_RUN = re.compile(
    r"(\\[sSwWdD]|\[\^?\]?(?:[^\]\\]|\\.)*\]|\.)"
    r"(?:[*+]|\{\d*,\d*\})?\??\Z")

#: Escapes that match U+000A, and the classes that contain one.
_NEWLINE_ESCAPES = ("\\s", "\\W", "\\D", "\\n", "\\r")


class _Site(NamedTuple):
    """One `re.*` call the guard considers in scope, and what it could read.

    `decline` is set when the guard **cannot answer**, never merely when it
    could not read every byte: a pattern spliced from literals still has a
    readable head and tail, which is all the anchor question needs.
    """

    lineno: int
    text: Optional[str]       # whole pattern, when every piece is a literal
    spliced: bool             # not whole, but head and tail both read
    swallow_known: bool       # `swallows` is a verdict, not an assumption
    head_anchored: bool       # the readable head starts with `^`
    anchor: Optional[str]     # `"$"`, `"\\Z"`, or None when unreadable
    swallows: bool            # the run before the anchor can eat a newline
    waiver: Optional[str]
    decline: Optional[str]


def _python_sources() -> list[Path]:
    out: list[Path] = []
    for entry in SCANNED:
        target = ROOT / entry
        if target.is_file():
            out.append(target)
        elif target.is_dir():
            out.extend(sorted(target.rglob("*.py")))
    return out


def _comments(source: str) -> list[tuple[int, str, bool]]:
    """`(lineno, text, on_its_own_line)` per comment -- tokenized, not grepped.

    Tokenizing is the point. `"anchored-ok" in line` also fires on the token
    inside a string literal, and on prose that merely names it (#1241).
    """
    out: list[tuple[int, str, bool]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                own = tok.line[:tok.start[1]].strip() == ""
                out.append((tok.start[0], tok.string, own))
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        pass
    return out


def _silent_waivers(source: str) -> list[int]:
    """Line numbers of comments that claim a waiver without giving a reason."""
    return [lineno for lineno, text, _ in _comments(source)
            if _WAIVER_OPENS.match(text) and not WAIVER.match(text)]


def _plus_leaves(node: ast.expr) -> list[ast.expr]:
    """Flatten `a + b + c`; anything else is a single leaf."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _plus_leaves(node.left) + _plus_leaves(node.right)
    return [node]


def _literal(node: ast.expr) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _decline_reason(leaves: list[ast.expr]) -> str:
    for leaf in leaves:
        if _literal(leaf) is not None:
            continue
        if isinstance(leaf, ast.Name):
            return f"a name (`{leaf.id}`) this scan does not resolve"
        if isinstance(leaf, ast.Call):
            return f"a call (`{ast.unparse(leaf)[:60]}`) evaluated at runtime"
        if isinstance(leaf, ast.JoinedStr):
            return "an f-string interpolated at runtime"
        return f"an expression this scan does not read (`{type(leaf).__name__}`)"
    raise AssertionError("every leaf was a literal")  # pragma: no cover


def _backslash_run(text: str, end: int) -> int:
    count = 0
    while end - count - 1 >= 0 and text[end - count - 1] == "\\":
        count += 1
    return count


def _trailing_anchor(text: str) -> Optional[str]:
    r"""The anchor that really ends `text` -- `"$"`, `"\\Z"`, or None.

    The even-backslash run tells `\\$` (a literal backslash, then the anchor)
    apart from `\$` (a literal dollar, and no anchor at all).
    """
    if text.endswith("$") and _backslash_run(text, len(text) - 1) % 2 == 0:
        return "$"
    if text.endswith("\\Z") and _backslash_run(text, len(text) - 2) % 2 == 0:
        return "\\Z"
    return None


def _atom_swallows(atom: str, dotall: bool) -> bool:
    if atom == ".":
        return dotall
    if not atom.startswith("["):
        return atom in ("\\s", "\\W", "\\D")
    body = atom[1:-1]
    listed = any(esc in body for esc in _NEWLINE_ESCAPES) or "\n" in body
    if body.startswith("^"):
        return not listed          # negated: swallows unless it names one
    return listed


def _swallows_newline(body: str, dotall: bool) -> tuple[bool, bool]:
    r"""`(swallows, decided)` for the run at the end of `body`.

    Trailing group closers are peeled, because `(.*)\Z` is the same defect as
    `.*\Z` and writing the capture around it must not hide it.

    `decided` exists for a spliced pattern whose last literal is only the
    anchor: the run in front of it lives in a piece this scan cannot read, and
    answering "no" there would be a definitive clean verdict over bytes never
    examined. A "yes" is always sound -- a swallowing run that is visible is
    real whatever precedes it. The residual limit, stated: a splice whose last
    literal is a fragment of a run started in an unreadable piece (`... + r"s*"`
    after a piece ending in a backslash) is read as the fragment.
    """
    while True:
        hit = _TRAILING_RUN.search(body)
        if hit and _atom_swallows(hit.group(1), dotall):
            return True, True
        if body.endswith(")") and _backslash_run(body, len(body) - 1) % 2 == 0:
            body = body[:-1]
            continue
        return False, bool(body)


def _scan_source(source: str) -> list[_Site]:
    r"""Every in-scope `re.*` call in `source`, decided or explicitly declined.

    In scope means: not `re.MULTILINE` (there `$` means end-of-line and `\Z`
    would be wrong), and not a readable head that fails to start with `^` (a
    suffix test such as `\.pem$`, where matching before a trailing newline
    changes nothing a caller can act on).
    """
    comments = _comments(source)
    waivers = {lineno: WAIVER.match(text).group(1).strip()
               for lineno, text, _ in comments if WAIVER.match(text)}
    own_line = {lineno for lineno, _, own in comments if own}
    sites: list[_Site] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in _RE_CALLS
                and getattr(func.value, "id", None) == "re" and node.args):
            continue
        leaves = _plus_leaves(node.args[0])
        head, tail = _literal(leaves[0]), _literal(leaves[-1])
        if head is not None and not head.startswith("^"):
            continue
        flags = " ".join(
            [ast.unparse(a) for a in node.args[1:]]
            + [ast.unparse(k.value) for k in node.keywords]
        )
        if re.search(r"\bre\.(MULTILINE|M)\b", flags):
            continue
        parts = [_literal(leaf) for leaf in leaves]
        text = "".join(parts) if all(p is not None for p in parts) else None
        dotall = (bool(re.search(r"\bre\.(DOTALL|S)\b", flags))
                  or (head or "").startswith("(?s"))
        anchor = _trailing_anchor(tail) if tail is not None else None
        decline = (None if head is not None and tail is not None
                   else _decline_reason(leaves))
        swallows, decided = (
            _swallows_newline(tail[:len(tail) - len(anchor)], dotall)
            if anchor else (False, True))
        above = node.lineno - 1
        while above in own_line:
            above -= 1
        span = range(above + 1, (node.end_lineno or node.lineno) + 1)
        reason = next((waivers[n] for n in span if n in waivers), None)
        spliced = text is None and decline is None
        sites.append(_Site(node.lineno, text, spliced, decided or not spliced,
                           head is not None, anchor, swallows, reason, decline))
    return sites


def _scan_tree() -> list[tuple[str, _Site]]:
    out: list[tuple[str, _Site]] = []
    for path in _python_sources():
        rel = path.relative_to(ROOT).as_posix()
        for site in _scan_source(path.read_text(encoding="utf-8")):
            out.append((rel, site))
    return out


def test_no_fully_anchored_dollar_guard() -> None:
    offenders = [f"{rel}:{site.lineno}: {site.text or '<spliced>'!r}"
                 for rel, site in _scan_tree()
                 if site.anchor == "$" and not site.waiver]
    assert not offenders, (
        "These patterns are anchored ^...$ and used as whole-value tests, so "
        "each one accepts its own value with a trailing newline appended -- "
        "Python's $ matches before a final newline (#1188).\n"
        + "\n".join(offenders)
        + "\n\nEnd the pattern with \\Z instead, or -- if the value really is "
        "one line out of a larger text and the newline is the delimiter rather "
        "than smuggled input -- append `# anchored-ok: <why>` to the line the "
        "re. call starts on.\n\n" + _NO_OP_ADVICE
    )


def test_no_no_op_trailing_anchor() -> None:
    r"""`\Z` that changes nothing, which is worse than the `$` it replaced.

    A guard that accepts its own prescribed fix without checking whether the
    fix does anything converts a real finding into a closed one (#1241).
    """
    offenders = [f"{rel}:{site.lineno}: {site.text or '<spliced>'!r}"
                 for rel, site in _scan_tree()
                 if site.anchor == "\\Z" and site.swallows and not site.waiver]
    assert not offenders, (
        "These patterns end in \\Z, and the run in front of it swallows a "
        "trailing newline anyway, so the anchor is decoration:\n"
        + "\n".join(offenders) + "\n\n" + _NO_OP_ADVICE
    )


def test_no_undecidable_trailing_run() -> None:
    """A splice whose last literal is only the anchor answers nothing.

    `swallows=False` there would be a clean verdict over bytes the scan never
    read -- narrower than the old blindness and worse, because being absent
    from a scan at least never claimed anything (#1241).
    """
    offenders = [f"{rel}:{site.lineno}" for rel, site in _scan_tree()
                 if site.anchor and not site.swallow_known and not site.waiver]
    assert not offenders, (
        "These patterns are spliced and end in a literal that is only the "
        "anchor, so what precedes it was never read:\n" + "\n".join(offenders)
        + "\n\nMove the trailing run into the last literal of the splice, or "
        "append `# anchored-ok: <why>`.\n\n" + _NO_OP_ADVICE
    )


def test_no_half_read_whole_value_pattern() -> None:
    """A pattern the guard can start reading and cannot finish is a finding.

    Silently covering the readable half and reporting clean is the defect this
    repo keeps having; the third state has to be said out loud (#1241).
    """
    offenders = [f"{rel}:{site.lineno}: {site.decline}"
                 for rel, site in _scan_tree()
                 if site.head_anchored and site.decline and not site.waiver]
    assert not offenders, (
        "These patterns start with `^` and this scan cannot read what they "
        "end with, so it cannot say whether they accept a trailing newline:\n"
        + "\n".join(offenders)
        + "\n\nSplice the tail from a literal, or -- if the pattern is not a "
        "whole-value test -- append `# anchored-ok: <why>`."
    )


def test_every_declined_site_names_a_reason() -> None:
    """Out of scope is fine; out of scope without saying so is the defect.

    Three states, never two: read whole, read at both ends and therefore
    decidable, or declined with the reason attached.
    """
    mute = [f"{rel}:{site.lineno}" for rel, site in _scan_tree()
            if site.text is None and not site.spliced and not site.decline]
    assert not mute, "read no pattern text and gave no reason:\n" + "\n".join(mute)


def test_the_splice_reader_is_still_wired() -> None:
    """`_PATH_TOK` was invisible for exactly as long as `+` went unhandled.

    With nothing in this bucket the guard is back to literals only, and every
    tree test above goes quietly green over the half it stopped reading
    (#1241).
    """
    spliced = [f"{rel}:{site.lineno}" for rel, site in _scan_tree()
               if site.spliced]
    assert spliced, "no `+`-spliced pattern read; the splice reader is dead"


def test_every_waiver_says_why() -> None:
    """A waiver with no reason is a suppression, and reads as a decision."""
    silent = [f"{path.relative_to(ROOT).as_posix()}:{lineno}"
              for path in _python_sources()
              for lineno in _silent_waivers(path.read_text(encoding="utf-8"))]
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

# --------------------------------------------------------------------------
# The scanner's own behaviour, pinned on synthetic sources (#1241).
#
# The tree-scan tests above are green whenever the tree happens to be clean,
# so they cannot tell a scanner that reads every pattern from one that reads
# the easy half. These do: each hands `_scan_source` a source string whose
# right answer is known.
# --------------------------------------------------------------------------


def _one(source: str) -> "_Site":
    sites = _scan_source(source)
    assert len(sites) == 1, f"expected one candidate site, got {sites}"
    return sites[0]


def test_prose_that_declines_a_waiver_is_not_a_waiver() -> None:
    """The comment that most deserves to exist must not trip the guard."""
    source = r"""import re
# The anchor stays, and deliberately not an anchored-ok waiver: the input
# is a whole value, not a line sliced out of a larger text.
X = re.compile(r'^[a-z]+\Z')
"""
    assert _silent_waivers(source) == []
    assert _one(source).waiver is None


def test_a_waiver_has_to_open_its_comment() -> None:
    """Mid-comment prose about a waiver must not grant one, or demand one.

    The second assertion is the one #1241 was filed for: the old check said
    `"anchored-ok" in line`, so this line was a waiver with no reason after
    the colon and the guard asked the author to justify an exit not taken.
    """
    source = r"""import re
X = re.compile(r'^[a-z]+$')  # not an anchored-ok: excuse, just prose
"""
    assert _one(source).waiver is None
    assert _silent_waivers(source) == []


def test_anchored_ok_inside_a_string_is_not_a_waiver() -> None:
    """On the call's own line, where a line-based reader cannot tell them apart."""
    source = r"""import re
S = '# anchored-ok: a fake'; X = re.compile(r'^[a-z]+$')
"""
    assert _silent_waivers(source) == []
    assert _one(source).waiver is None


def test_a_real_waiver_still_grants_and_still_needs_a_reason() -> None:
    granted = r"""import re
X = re.compile(r'^[a-z]+$')  # anchored-ok: one line out of a larger text
"""
    assert _one(granted).waiver == "one line out of a larger text"
    silent = r"""import re
X = re.compile(r'^[a-z]+$')  # anchored-ok:
"""
    assert _silent_waivers(silent) == [2]


def test_a_waiver_may_sit_on_the_line_above_the_call() -> None:
    """Thirty trailing columns is not room for a reason worth reading."""
    source = r"""import re
# anchored-ok: DOTALL is the point and the trailing run is meant to swallow
# the newline, which is a sentence that does not fit after the call.
X = re.compile(r'^a(.*)\Z', re.DOTALL)
"""
    assert _one(source).waiver.startswith("DOTALL is the point")


def test_a_pattern_built_by_concatenation_is_seen() -> None:
    """#1241: `_PATH_TOK` sat three lines from a flagged twin, and was invisible."""
    source = r"""import re
E = 'a|b'
X = re.compile(r'^(?:' + E + r')$')
"""
    site = _one(source)
    assert site.anchor == "$"
    assert site.decline is None


def test_concatenation_with_an_unreadable_tail_is_declined_not_passed() -> None:
    """Head says `^`, tail unknown: the guard cannot answer, so it must say so."""
    source = r"""import re
E = 'a|b'
X = re.compile(r'^(?:' + E)
"""
    site = _one(source)
    assert site.decline
    assert site.anchor is None


def test_an_unreadable_pattern_always_names_why() -> None:
    """A runtime pattern is out of scope, but never silently out of scope."""
    for expr, want in (
        ("re.compile(pattern)", "a name"),
        ("re.compile(re.escape(pattern))", "a call"),
        ("re.compile(f'^{x}$')", "an f-string"),
    ):
        site = _one("import re\nX = " + expr + "\n")
        assert site.text is None
        assert want in (site.decline or ""), (expr, site.decline)


def test_a_Z_after_a_whitespace_run_is_refused() -> None:
    """The guard's own prescribed fix, and on this shape it is a no-op."""
    site = _one(r"""import re
X = re.compile(r'^(#{1,6})\s+(.*?)\s*\Z')
""")
    assert site.anchor == "\\Z"
    assert site.swallows is True


def test_a_narrowed_trailing_run_before_Z_is_accepted() -> None:
    site = _one(r"""import re
X = re.compile(r'^(#{1,6})\s+(.*?)[ \t]*\Z')
""")
    assert site.anchor == "\\Z"
    assert site.swallows is False


def test_dot_star_before_Z_swallows_only_under_dotall() -> None:
    dotall = r"""import re
X = re.compile(r'^a(.*)\Z', re.DOTALL)
"""
    plain = r"""import re
X = re.compile(r'^a(.*)\Z')
"""
    assert _one(dotall).swallows is True
    assert _one(plain).swallows is False


def test_an_unquantified_swallowing_atom_is_refused() -> None:
    r"""`\s\Z` eats the newline exactly as `\s*\Z` does; the `*` is not the bug."""
    assert _one(r"""import re
X = re.compile(r'^abc\s\Z')
""").swallows is True


def test_a_class_opening_with_a_bracket_is_read() -> None:
    """`[^]abc]` is a legal class whose first literal member is `]`."""
    assert _one(r"""import re
X = re.compile(r'^abc[^]abc]*\Z')
""").swallows is True


def test_a_splice_whose_tail_is_only_the_anchor_gives_no_verdict() -> None:
    """The run before the anchor is in a piece this scan cannot read.

    Reporting `swallows=False` here would be a definitive clean verdict over
    bytes never examined -- narrower than the old blindness, and worse, because
    absent from the scan at least never claimed anything.
    """
    site = _one(r"""import re
V = 'x'
X = re.compile(r'^abc' + V + r'\Z')
""")
    assert site.spliced is True
    assert site.swallow_known is False


def test_a_splice_whose_tail_carries_the_run_still_decides() -> None:
    """`_PATH_TOK`'s shape: the trailing run is inside the last literal."""
    site = _one(r"""import re
E = 'py|md'
X = re.compile(r'^([a-z]*\.(?:' + E + r'))(?::(\d+))?\Z')
""")
    assert site.spliced is True
    assert site.swallow_known is True
    assert site.swallows is False


def test_the_no_op_anchor_is_measured_not_quoted() -> None:
    """#1241's measurement, run rather than repeated."""
    value = "## Open defects\n"
    assert re.compile(r"^(#{1,6})\s+(.*?)\s*$").match(value)
    assert re.compile(r"^(#{1,6})\s+(.*?)\s*\Z").match(value)
    assert not re.compile(r"^(#{1,6})\s+(.*?)[ \t]*\Z").match(value)


def test_the_advice_says_the_naive_fix_is_a_no_op() -> None:
    assert "no-op" in _NO_OP_ADVICE
    assert "narrow" in _NO_OP_ADVICE.lower()
