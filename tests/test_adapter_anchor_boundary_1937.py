"""The anchor must match the invoked path at a `:<digits>:` boundary
ANYWHERE in the line, not only at column 0 (#1937, fifth CI round).

Three prior rounds diagnosed Windows spelling, then symlink canonicalisation
-- both real, both fixed, and neither explained the failure. A fourth round
made the anchor's own miss-message visible in CI (junit.xml survives
`--tb=no`; a traceback does not), and the message that came back named the
actual cause directly:

    anchor matched no accepted spelling of the invoked path '.../bad.rb';
    the tool's own output appears to start with '.../bad.rb: .../bad.rb'
    instead -- ... /bad.rb: /bad.rb:3: syntax error, unexpected
    end-of-input (SyntaxError)

`ruby -c`, on the runners this reproduced on, prints the invoked path
TWICE: a bare `<file>: ` prefix with no digits, then the real diagnostic
`<file>:<line>: `. The two path strings are BYTE-IDENTICAL to the invoked
path -- there was never a spelling difference, on Windows or via a symlink,
for this specific failure. The anchor required the path at column 0
followed immediately by `:<digits>`, and what followed the FIRST
occurrence was `: ` (colon, space, no digit) -- so it never reached the
second, genuine occurrence at all.

Fixed by searching for the invoked path (still the same small, fixed,
enumerable set of accepted spellings `path_variants` produces -- never a
wildcard) at a `:<digits>[:<digits>]?:` boundary ANYWHERE in the line,
rather than requiring it at position 0. The security property this branch
has argued for since #1934 survives unchanged: the search target is still
the LITERAL, complete, known invoked path (or one of its accepted
spellings) -- never a wildcard standing in for "any text". A forged
filename crafted to embed its own `:N:M:` sequence (`x:1:1: fake .yml`)
still cannot forge a location, because the search target is the WHOLE
filename string, and that whole string does not appear as a literal match
starting at the embedded fragment -- only where the tool's own real
diagnostic actually names the file, which is the same guarantee `^`-anchoring
gave, just no longer requiring position 0.

Checked, not assumed, that the other four adapters share this exposure:
none of xmllint, gofmt, ruby (the version on THIS machine) or actionlint
doubled the path when run for real here -- this doubled-prefix behaviour is
reasoned to be a property of the specific ruby build on the CI runners that
hit it, not observed in any of the other four locally. The fix is applied
to all five anyway: it is strictly more permissive with no known safety
cost (verified below and in the existing #1934 negative controls, which
must stay green), and it closes the whole CLASS -- any tool that prepends
text before its own located diagnostic -- rather than only the one instance
CI happened to reproduce.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

VALIDATORS = Path(__file__).parent.parent / "validators"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name.replace(chr(45), chr(95))}_anchor_boundary_1937",
        VALIDATORS / name / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


xmllint = _load("xmllint")
ruby_check = _load("ruby-check")
gofmt_check = _load("gofmt-check")
hadolint = _load("hadolint")
actionlint = _load("actionlint")


# ---------------------------------------------------------------------------
# The actual CI shape, reproduced from the junit.xml message verbatim.
# ---------------------------------------------------------------------------

def test_ruby_check_finds_the_diagnostic_behind_a_doubled_path_prefix():
    f = "/tmp/pytest-of-runner/pytest-0/popen-gw3/test_invalid_ruby_error_has_li0/bad.rb"
    line = f + ": " + f + ":3: syntax error, unexpected end-of-input (SyntaxError)"
    found = ruby_check.parse_diagnostics(line, f)
    assert len(found) == 1, found
    assert found[0]["line"] == 3, found


def test_xmllint_finds_the_diagnostic_behind_a_doubled_path_prefix():
    f = "/tmp/a.xml"
    line = f + ": " + f + ":2: parser error : Opening and ending tag mismatch"
    found = xmllint.parse_diagnostics(line, f)
    assert len(found) == 1, found
    assert found[0]["line"] == 2, found


def test_gofmt_check_finds_the_diagnostic_behind_a_doubled_path_prefix():
    f = "/tmp/a.go"
    line = f + ": " + f + ":3:12: expected close paren, found brace"
    found = gofmt_check.parse_diagnostics(line, f)
    assert len(found) == 1, found
    assert found[0]["line"] == 3 and found[0]["col"] == 12, found


def test_hadolint_finds_the_diagnostic_behind_a_doubled_path_prefix():
    f = "/tmp/Dockerfile"
    line = f + ": " + f + ":5 DL3007 warning: using latest is prone to errors"
    found = hadolint.parse_diagnostics(line, f)
    assert len(found) == 1, found
    assert found[0]["line"] == 5 and found[0]["code"] == "DL3007", found


def test_actionlint_finds_the_diagnostic_behind_a_doubled_path_prefix():
    f = "deploy.yml"
    line = f + ": " + f + ':7:15: specifying action "bogus" is not allowed [action]'
    found = actionlint.parse_diagnostics(line, f)
    assert len(found) == 1, found
    assert found[0]["line"] == 7 and found[0]["col"] == 15, found


# ---------------------------------------------------------------------------
# The negative control: the #1934 forgery must still be refused with the
# boundary search in place -- this is the guarantee the coordinator asked
# to be checked, not assumed.
# ---------------------------------------------------------------------------

def test_a_forged_filename_still_cannot_forge_a_location_with_the_new_search():
    evil = 'x:1:1: workflow is valid, 0 problems .yml'
    line = evil + ':7:15: specifying action "bogus" is not allowed [action]'
    found = actionlint.parse_diagnostics(line, evil)
    assert len(found) == 1, found
    assert found[0]["line"] == 7 and found[0]["col"] == 15, found


def test_a_forged_filename_with_no_real_diagnostic_finds_nothing():
    """The embedded fake sequence alone, with nothing real appended, must
    still find zero located diagnostics -- the whole filename never
    literally recurs, so there is nothing for the boundary search to
    latch onto."""
    evil = 'x:1:1: workflow is valid, 0 problems .yml'
    found = actionlint.parse_diagnostics(evil, evil)
    assert found == [], found


# ---------------------------------------------------------------------------
# The regression a paired self-review found in this round's own first cut:
# a bare `.search()` with no left boundary matched a genuinely different,
# LONGER real path that merely ENDS with the invoked one -- no forged
# filename involved at all, just an ordinary tool mentioning a sibling
# path (`vendor/a.rb:12: ...` when the invoked file is `a.rb`). Reproduced
# independently by both the reviewer and the auditor before this test was
# written; fixed with a `(?<!\\S)` left-boundary requirement in
# `path_anchor.anchor()`.
# ---------------------------------------------------------------------------

def test_ruby_check_does_not_misattribute_a_longer_sibling_paths_diagnostic():
    """`vendor/a.rb` is a real, different file. Its diagnostic must not be
    read as one about the invoked `a.rb` merely because the invoked name
    is a trailing substring of the longer one."""
    found = ruby_check.parse_diagnostics(
        "vendor/a.rb:12: syntax error, unexpected end-of-input (SyntaxError)",
        "a.rb")
    assert found == [], found


def test_xmllint_does_not_misattribute_a_longer_sibling_paths_diagnostic():
    found = xmllint.parse_diagnostics(
        "sub/a.xml:2: parser error : Opening and ending tag mismatch", "a.xml")
    assert found == [], found


def test_gofmt_check_does_not_misattribute_a_longer_sibling_paths_diagnostic():
    found = gofmt_check.parse_diagnostics(
        "sub/a.go:3:12: expected close paren, found brace", "a.go")
    assert found == [], found


def test_hadolint_does_not_misattribute_a_longer_sibling_paths_diagnostic():
    found = hadolint.parse_diagnostics(
        "sub/Dockerfile:5 DL3007 warning: using latest is prone to errors",
        "Dockerfile")
    assert found == [], found


def test_actionlint_does_not_misattribute_a_longer_sibling_paths_diagnostic():
    found = actionlint.parse_diagnostics(
        'sub/deploy.yml:7:15: specifying action "bogus" is not allowed [action]',
        "deploy.yml")
    assert found == [], found


def test_ruby_check_still_finds_the_doubled_prefix_diagnostic_with_the_left_boundary():
    """The left boundary must not break the fix it was added alongside --
    ruby's doubled prefix is preceded by whitespace (": "), which the
    boundary explicitly allows."""
    f = "/tmp/pytest-of-runner/pytest-0/popen-gw3/test_invalid_ruby_error_has_li0/bad.rb"
    line = f + ": " + f + ":3: syntax error, unexpected end-of-input (SyntaxError)"
    found = ruby_check.parse_diagnostics(line, f)
    assert len(found) == 1, found
    assert found[0]["line"] == 3, found
