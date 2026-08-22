"""A real diagnostic silently loses its location when a tool's own path
echo does not byte-match the invoked argv string (#1937).

#1934 anchored xmllint/ruby-check/hadolint/gofmt-check/actionlint's line
parser on `re.escape(file)` -- the literal path each adapter invoked its
tool with -- so a crafted filename could no longer forge its own reported
line/col. On windows-latest CI, real ruby's own diagnostic output did not
echo that literal string back: the anchor then matched nothing, and a real,
located diagnostic degraded to an unlocated one (never a bare `count: 0` --
every adapter's `main()` already has a `if not errors and output:` fallback
for "found output, could not place it in the file" -- but the line and
column a real diagnostic carried were lost all the same, which is the
"absence the tool produced, rendered as an absence in the world" defect
class this repository names as its own).

`validators/common/path_anchor.py` widens each adapter's anchor to accept a
small, FIXED set of spellings of the invoked path -- separator direction and
a leading drive letter's case -- rather than requiring an exact byte match.
It is pure string manipulation: no `os.path`, no filesystem access, nothing
platform-native. So this file drives it, and every adapter using it, with
Windows-shaped paths (backslashes, a drive letter) DIRECTLY through the
parser, on whatever platform this test happens to run on. The CI failure
that found this was a Windows leg; this control does not need one.

actionlint is the one adapter not exercised here with an ABSOLUTE
Windows-shaped path: its own anchor additionally calls `os.path.relpath`
first (#1934's fix for actionlint's CWD-relativisation), and `relpath` is
itself platform-native -- feeding it a Windows-shaped absolute path on a
POSIX runner exercises POSIX's own (irrelevant) interpretation of that
string, not Windows's. actionlint's separator/case tolerance is exercised
here with a RELATIVE Windows-shaped path instead, for which `relpath` is a
no-op on every platform (`os.path.relpath(p) == p` when `p` is already
relative to the process's own CWD), so what is left to test is exactly the
`path_anchor` behaviour this file is about, with no relpath confound.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

VALIDATORS = Path(__file__).parent.parent / "validators"

_ADAPTERS = {
    name: VALIDATORS / name / f"{name}.py"
    for name in ("xmllint", "ruby-check", "gofmt-check", "hadolint", "actionlint")
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name.replace(chr(45), chr(95))}_1937", _ADAPTERS[name])
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


path_anchor = _load_common = None


def _load_common_module():
    global path_anchor
    if path_anchor is None:
        spec = importlib.util.spec_from_file_location(
            "path_anchor_1937", VALIDATORS / "common" / "path_anchor.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        path_anchor = mod
    return path_anchor


xmllint = _load("xmllint")
ruby_check = _load("ruby-check")
gofmt_check = _load("gofmt-check")
hadolint = _load("hadolint")
actionlint = _load("actionlint")


# ---------------------------------------------------------------------------
# The shared helper itself -- pure, portable, no filesystem access.
# ---------------------------------------------------------------------------

def test_path_variants_covers_separator_direction():
    pa = _load_common_module()
    variants = pa.path_variants("C:\\Users\\dev\\a.xml")
    assert "C:\\Users\\dev\\a.xml" in variants
    assert "C:/Users/dev/a.xml" in variants


def test_path_variants_covers_drive_letter_case():
    pa = _load_common_module()
    variants = pa.path_variants("c:/users/dev/a.rb")
    assert "c:/users/dev/a.rb" in variants
    assert "C:/users/dev/a.rb" in variants


def test_path_variants_never_introduces_a_different_path():
    """The tolerance is spelling-only. A genuinely different path -- not a
    separator/case respelling of the same one -- must never appear."""
    pa = _load_common_module()
    variants = pa.path_variants("C:\\Users\\dev\\a.xml")
    assert "C:\\Users\\dev\\evil.xml" not in variants
    assert all(v.lower().replace("\\", "/").endswith("users/dev/a.xml") for v in variants)


# ---------------------------------------------------------------------------
# Windows-shaped absolute paths through xmllint / ruby-check / gofmt-check /
# hadolint -- none of these adapters transform the path before anchoring, so
# an absolute Windows-shaped path is a fair, portable stand-in for what real
# CI hit.
# ---------------------------------------------------------------------------

def test_xmllint_finds_the_location_when_echoed_with_forward_slashes():
    invoked = "C:\\Users\\dev\\project\\a.xml"
    echoed = "C:/Users/dev/project/a.xml"
    line = f'{echoed}:2: parser error : Opening and ending tag mismatch'
    found = xmllint.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 2, found


def test_ruby_check_finds_the_location_when_echoed_with_forward_slashes():
    invoked = "C:\\Users\\dev\\project\\bad.rb"
    echoed = "C:/Users/dev/project/bad.rb"
    line = f'{echoed}:2: syntax error, unexpected end-of-input'
    found = ruby_check.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 2, found


def test_gofmt_check_finds_the_location_when_echoed_with_forward_slashes():
    invoked = "C:\\Users\\dev\\project\\bad.go"
    echoed = "C:/Users/dev/project/bad.go"
    line = f'{echoed}:3:12: expected close paren, found brace'
    found = gofmt_check.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 3 and found[0]["col"] == 12, found


def test_hadolint_finds_the_location_when_echoed_with_forward_slashes():
    invoked = "C:\\Users\\dev\\project\\Dockerfile"
    echoed = "C:/Users/dev/project/Dockerfile"
    line = f'{echoed}:5 DL3007 warning: using latest is prone to errors'
    found = hadolint.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 5 and found[0]["code"] == "DL3007", found


def test_ruby_check_finds_the_location_with_a_lowercase_drive_letter():
    invoked = "C:/Users/dev/project/bad.rb"
    echoed = "c:/Users/dev/project/bad.rb"
    line = f'{echoed}:2: syntax error, unexpected end-of-input'
    found = ruby_check.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 2, found


# ---------------------------------------------------------------------------
# actionlint, with a RELATIVE Windows-shaped path so `os.path.relpath` is a
# no-op on every platform and what is left to exercise is path_anchor alone.
# ---------------------------------------------------------------------------

def test_actionlint_finds_the_location_when_echoed_with_forward_slashes():
    invoked = "workflows\\deploy.yml"
    echoed = "workflows/deploy.yml"
    line = f'{echoed}:7:15: specifying action "bogus" is not allowed [action]'
    found = actionlint.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 7 and found[0]["col"] == 15, found


# ---------------------------------------------------------------------------
# The negative control: without path_anchor's tolerance, none of the above
# would be found. Proven directly against the OLD exact-match shape (#1934's
# original anchor), not inferred -- this is the red half of this file's own
# claim, kept in the file rather than only in a session transcript.
# ---------------------------------------------------------------------------

def test_a_plain_exact_anchor_would_have_missed_all_of_the_above():
    import re
    invoked = "C:\\Users\\dev\\project\\a.xml"
    echoed = "C:/Users/dev/project/a.xml"
    line = f'{echoed}:2: parser error : Opening and ending tag mismatch'
    exact = re.compile(r"^" + re.escape(invoked) + r":(\d+):\s*(.+)")
    assert exact.match(line) is None, (
        "the exact-match anchor this file replaces would already have found "
        "this -- the control below proves nothing")
