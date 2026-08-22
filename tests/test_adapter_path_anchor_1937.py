"""A real diagnostic silently loses its location when a tool's own path
echo does not byte-match the invoked argv string (#1937) -- and the first
cut of the fix for that reopened #1934's own misattribution one layer over,
found by this branch's own self-review before it shipped (see the second
half of this file).

#1934 anchored xmllint/ruby-check/hadolint/gofmt-check/actionlint's line
parser on `re.escape(file)` -- the literal path each adapter invoked its
tool with -- so a crafted filename could no longer forge its own reported
line/col. On windows-latest CI, real ruby's own diagnostic output did not
echo that literal string back: the anchor then matched nothing, and a real,
located diagnostic degraded to an unlocated one (never a bare `count: 0` --
every adapter's `main()` already has a fallback for "found output, could not
place it in the file" -- but the line and column a real diagnostic carried
were lost all the same, which is the "absence the tool produced, rendered as
an absence in the world" defect class this repository names as its own).

`validators/common/path_anchor.py` widens each adapter's anchor to accept a
small, FIXED set of spellings of the invoked path -- separator direction and
a leading drive letter's case -- rather than requiring an exact byte match,
GATED TO WINDOWS: both transforms are unsafe on POSIX, where the characters
they key off (`\\\\`, a leading `X:`) are ordinary filename content, and the
first version of this module did not gate on that, so this file's own
`test_path_variants_never_introduces_a_different_path` originally asserted a
weakened, self-normalizing version of the property it claimed to check and
would not have caught it. Both are fixed together here.

It is pure string manipulation: no filesystem access. So the Windows-only
widening is driven with Windows-shaped paths DIRECTLY through the parser, on
whatever platform this test happens to run on, via the explicit
`platform="win32"` override every function in `path_anchor` accepts for
exactly this reason -- production code never passes it and falls back to the
real `sys.platform`. The CI failure that found this was a Windows leg; this
control does not need one.

actionlint is the one adapter exercised here with a RELATIVE Windows-shaped
path rather than an absolute one: its own anchor additionally calls
`os.path.relpath` first (#1934's fix for actionlint's CWD-relativisation),
and `relpath` is itself platform-native. On POSIX, `os.path.relpath` is a
no-op for an already-relative input -- verified directly below, not assumed
-- but that is NOT true on every platform: CPython's own `ntpath.relpath`
always rejoins its result with `\\\\`, so on real Windows it is not a no-op
even for a relative input (a claim this file used to make and #1937's own
self-review found wrong). A relative path sidesteps the OS-native
relativisation LOGIC (nothing to walk up or resolve), which is the actual
reason it isolates `path_anchor`'s own behaviour here; it does not sidestep
separator normalisation on Windows, and the assertion below pins the POSIX
half of that claim rather than leaving it merely asserted in prose.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from _symlink import require_symlink

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


_path_anchor_cache = None


def _load_common_module():
    global _path_anchor_cache
    if _path_anchor_cache is None:
        spec = importlib.util.spec_from_file_location(
            "path_anchor_1937", VALIDATORS / "common" / "path_anchor.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _path_anchor_cache = mod
    return _path_anchor_cache


xmllint = _load("xmllint")
ruby_check = _load("ruby-check")
gofmt_check = _load("gofmt-check")
hadolint = _load("hadolint")
actionlint = _load("actionlint")


def test_relpath_is_a_no_op_for_an_already_relative_path_on_posix():
    """Pinned rather than only asserted in prose -- the half of the module
    docstring's claim that IS true on the platform this suite actually runs
    on. Windows' `ntpath.relpath` rejoining with `\\\\` regardless is the
    other half, and is not (and cannot be) pinned here; see the docstring."""
    assert os.path.relpath("workflows\\deploy.yml") == "workflows\\deploy.yml"


# ---------------------------------------------------------------------------
# The shared helper itself -- pure, portable, no filesystem access. Every
# call below is explicit about which platform it means: `platform="win32"`
# where the Windows-only widening is under test, and no override (so the
# real `sys.platform` of whatever machine runs this suite decides) where the
# POSIX-safety of NOT widening is under test.
# ---------------------------------------------------------------------------

def test_path_variants_covers_separator_direction_on_windows():
    pa = _load_common_module()
    variants = pa.path_variants("C:\\Users\\dev\\a.xml", platform="win32")
    assert "C:\\Users\\dev\\a.xml" in variants
    assert "C:/Users/dev/a.xml" in variants


def test_path_variants_covers_drive_letter_case_on_windows():
    pa = _load_common_module()
    variants = pa.path_variants("c:/users/dev/a.rb", platform="win32")
    assert "c:/users/dev/a.rb" in variants
    assert "C:/users/dev/a.rb" in variants


def test_path_variants_does_not_widen_on_a_non_windows_platform():
    """The gate itself, pinned directly -- explicit non-Windows platform
    strings, not relying on whatever this suite happens to run on."""
    pa = _load_common_module()
    assert pa.path_variants("weird\\name.xml", platform="linux") == ["weird\\name.xml"]
    assert pa.path_variants("weird\\name.xml", platform="darwin") == ["weird\\name.xml"]


def test_safe_realpath_does_not_raise_on_an_embedded_nul_byte():
    """A self-review finding, itself corrected by a second one: the first
    cut of `safe_realpath` caught only `OSError`, but `os.path.realpath`
    raises `ValueError` for an embedded NUL byte on Linux -- verified
    directly. The `except (OSError, ValueError)` fix that followed still
    rested on realpath RAISING at all, and CI (macos-latest, 3.9) found a
    platform where it does not: realpath there joins the NUL straight into
    the returned string instead. `safe_realpath` now checks for the NUL
    explicitly, before ever calling `realpath`, so this assertion holds
    independent of what any one platform's C library does with one."""
    pa = _load_common_module()
    assert pa.safe_realpath("a\x00b") is None


def test_safe_realpath_rejects_a_nul_byte_even_if_realpath_itself_would_not(monkeypatch):
    """Proves the guard fires BEFORE `os.path.realpath` is ever called,
    rather than depending on what it does with the input -- monkeypatches
    it to the exact passthrough behaviour macOS 3.9 exhibited (return a
    joined, NUL-bearing string; raise nothing) and confirms `None` still
    comes back. Without the explicit check, this would return the
    NUL-bearing string on any platform whose `realpath` behaves this way."""
    pa = _load_common_module()
    monkeypatch.setattr(pa.os.path, "realpath", lambda p: "/passthrough/" + p)
    assert pa.safe_realpath("a\x00b") is None


def test_path_variants_does_not_collide_two_distinct_posix_paths():
    """The regression this file exists to guard: on a real POSIX machine
    (no `platform=` override -- this is what production code gets), a `\\\\`
    or a leading `X:` in an INVOKED filename is ordinary content, not a
    separator or a drive letter, and must never manufacture a variant that
    is actually a DIFFERENT real path. Both halves of the collision found in
    this branch's own self-review, pinned directly rather than only through
    an adapter."""
    pa = _load_common_module()
    if sys.platform.startswith("win"):
        import pytest
        pytest.skip("this is a POSIX-safety regression test; on real "
                    "Windows both paths below are the same path")
    assert pa.path_variants("weird\\name.xml") == ["weird\\name.xml"]
    assert "weird/name.xml" not in pa.path_variants("weird\\name.xml")
    assert pa.path_variants("X:secret") == ["X:secret"]
    assert "x:secret" not in pa.path_variants("X:secret")


def test_xmllint_does_not_attribute_a_different_posix_files_diagnostic():
    """The adapter-level shape of the same regression: a file genuinely
    named `weird\\name.xml` must not accept a diagnostic that names the
    genuinely different file `weird/name.xml` -- on real POSIX, both are
    real, distinct filenames."""
    if sys.platform.startswith("win"):
        import pytest
        pytest.skip("weird\\\\name.xml and weird/name.xml are the same path on Windows")
    invoked = "weird\\name.xml"
    out = "weird/name.xml:99: parser error : a different file's diagnostic"
    found = xmllint.parse_diagnostics(out, invoked)
    assert found == [], found


# ---------------------------------------------------------------------------
# Windows-shaped absolute paths through xmllint / ruby-check / gofmt-check /
# hadolint, with the widening explicitly turned on via `monkeypatch` on
# `sys.platform` -- the only lever available at the adapter level, since
# `parse_diagnostics(out, file)`'s public signature does not carry a
# `platform=` override of its own. `monkeypatch` reverts automatically, so
# this cannot leak into another test even under a shared worker process.
# ---------------------------------------------------------------------------

def test_xmllint_finds_the_location_when_echoed_with_forward_slashes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    invoked = "C:\\Users\\dev\\project\\a.xml"
    echoed = "C:/Users/dev/project/a.xml"
    line = f'{echoed}:2: parser error : Opening and ending tag mismatch'
    found = xmllint.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 2, found


def test_ruby_check_finds_the_location_when_echoed_with_forward_slashes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    invoked = "C:\\Users\\dev\\project\\bad.rb"
    echoed = "C:/Users/dev/project/bad.rb"
    line = f'{echoed}:2: syntax error, unexpected end-of-input'
    found = ruby_check.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 2, found


def test_gofmt_check_finds_the_location_when_echoed_with_forward_slashes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    invoked = "C:\\Users\\dev\\project\\bad.go"
    echoed = "C:/Users/dev/project/bad.go"
    line = f'{echoed}:3:12: expected close paren, found brace'
    found = gofmt_check.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 3 and found[0]["col"] == 12, found


def test_hadolint_finds_the_location_when_echoed_with_forward_slashes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    invoked = "C:\\Users\\dev\\project\\Dockerfile"
    echoed = "C:/Users/dev/project/Dockerfile"
    line = f'{echoed}:5 DL3007 warning: using latest is prone to errors'
    found = hadolint.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 5 and found[0]["code"] == "DL3007", found


def test_ruby_check_finds_the_location_with_a_lowercase_drive_letter(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    invoked = "C:/Users/dev/project/bad.rb"
    echoed = "c:/Users/dev/project/bad.rb"
    line = f'{echoed}:2: syntax error, unexpected end-of-input'
    found = ruby_check.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 2, found


def test_xmllint_does_not_widen_when_the_real_platform_is_not_windows(monkeypatch):
    """The other half of the gate, at the adapter level: with `sys.platform`
    left alone (or explicitly pinned to a POSIX value), the forward-slash
    echo above must NOT be found -- proving the widening in the tests above
    is doing the work, not something else in the adapter."""
    monkeypatch.setattr(sys, "platform", "linux")
    invoked = "C:\\Users\\dev\\project\\a.xml"
    echoed = "C:/Users/dev/project/a.xml"
    line = f'{echoed}:2: parser error : Opening and ending tag mismatch'
    found = xmllint.parse_diagnostics(line, invoked)
    assert found == [], found


# ---------------------------------------------------------------------------
# actionlint, with a RELATIVE Windows-shaped path -- see the module
# docstring for why relative sidesteps relpath's own relativisation LOGIC
# without claiming it sidesteps separator normalisation too.
# ---------------------------------------------------------------------------

def test_actionlint_finds_the_location_when_echoed_with_forward_slashes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    invoked = "workflows\\deploy.yml"
    echoed = "workflows/deploy.yml"
    line = f'{echoed}:7:15: specifying action "bogus" is not allowed [action]'
    found = actionlint.parse_diagnostics(line, invoked)
    assert len(found) == 1, found
    assert found[0]["line"] == 7 and found[0]["col"] == 15, found


# ---------------------------------------------------------------------------
# Canonicalisation tolerance (a symlinked invoked path, the tool's own
# diagnostic naming the resolved form instead) -- ungated, every platform,
# via a REAL local symlink. This is the class ubuntu-latest CI hit twice
# with the identical `assert None is not None` shape #1937's Windows-only
# fix did not cover: the same two tests, on Linux, with real ruby, ruling
# out "Windows-specific" as the whole story. Needs no particular CI
# platform -- a symlink is a symlink on any OS this suite runs on.
# ---------------------------------------------------------------------------

def _make_symlinked_file(tmp_path: Path, name: str, body: str) -> tuple[Path, Path]:
    """A `link/name` path whose target is `real/name` -- returns (linked,
    real), both existing files with the same content.

    `require_symlink()` (#1143) skips rather than raising `OSError` on a
    runner without the create-symlink privilege -- a contributor's Windows
    without Developer Mode, most notably (see
    tests/test_symlink_gating_register_1232.py, which is the build gate that
    would otherwise flag this call site as ungated)."""
    require_symlink()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)
    real_file = real_dir / name
    real_file.write_text(body, encoding="utf-8")
    return link_dir / name, real_file.resolve()


def test_ruby_check_finds_the_location_when_the_tool_reports_the_realpath(tmp_path: Path):
    """The invoked path traverses a symlink; the tool's own diagnostic names
    the RESOLVED path instead -- the exact shape a symlinked temp directory
    produces if the tool (or something upstream of it) canonicalises."""
    invoked, real = _make_symlinked_file(tmp_path, "bad.rb", "x = 1\n")
    line = f'{real}:1: syntax error, unexpected end near "x"'
    found = ruby_check.parse_diagnostics(line, str(invoked))
    assert len(found) == 1, found
    assert found[0]["line"] == 1, found


def test_xmllint_finds_the_location_when_the_tool_reports_the_realpath(tmp_path: Path):
    invoked, real = _make_symlinked_file(tmp_path, "a.xml", "<a>\n")
    line = f'{real}:2: parser error : Opening and ending tag mismatch'
    found = xmllint.parse_diagnostics(line, str(invoked))
    assert len(found) == 1, found
    assert found[0]["line"] == 2, found


def test_gofmt_check_finds_the_location_when_the_tool_reports_the_realpath(tmp_path: Path):
    invoked, real = _make_symlinked_file(tmp_path, "a.go", "package p\n")
    line = f'{real}:3:12: expected close paren, found brace'
    found = gofmt_check.parse_diagnostics(line, str(invoked))
    assert len(found) == 1, found
    assert found[0]["line"] == 3 and found[0]["col"] == 12, found


def test_hadolint_finds_the_location_when_the_tool_reports_the_realpath(tmp_path: Path):
    invoked, real = _make_symlinked_file(tmp_path, "Dockerfile", "FROM x\n")
    line = f'{real}:5 DL3007 warning: using latest is prone to errors'
    found = hadolint.parse_diagnostics(line, str(invoked))
    assert len(found) == 1, found
    assert found[0]["line"] == 5 and found[0]["code"] == "DL3007", found


def test_actionlint_finds_the_location_when_the_tool_reports_the_realpath(tmp_path: Path):
    """actionlint's own relpath-relativisation happens first; the second
    candidate this test needs is the REALPATH's own relative form (never an
    absolute one -- actionlint always relativises against CWD, established
    by #1934, so a resolved-and-relativised path is what real actionlint
    would print if it also resolves the symlink), which is what
    `extra_paths` in actionlint.py's `_line_re` supplies."""
    import os
    invoked, real = _make_symlinked_file(tmp_path, "deploy.yml", "on: push\n")
    reported_real = os.path.relpath(real)
    line = f'{reported_real}:7:15: specifying action "bogus" is not allowed [action]'
    found = actionlint.parse_diagnostics(line, str(invoked))
    assert len(found) == 1, found
    assert found[0]["line"] == 7 and found[0]["col"] == 15, found


def test_xmllint_does_not_locate_an_unrelated_files_diagnostic_via_realpath(tmp_path: Path):
    """The realpath widening must not become a second collision channel:
    a diagnostic naming some OTHER real file's canonical path -- not the
    invoked file's own -- must still be rejected."""
    invoked, _real = _make_symlinked_file(tmp_path, "a.xml", "<a>\n")
    other = tmp_path / "real" / "unrelated.xml"
    other.write_text("<b>\n", encoding="utf-8")
    line = f'{other.resolve()}:2: parser error : a different files diagnostic'
    found = xmllint.parse_diagnostics(line, str(invoked))
    assert found == [], found


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


def test_a_plain_exact_anchor_would_have_missed_the_realpath_cases_too(tmp_path: Path):
    import re
    invoked, real = _make_symlinked_file(tmp_path, "bad.rb", "x = 1\n")
    line = f'{real}:1: syntax error, unexpected end near "x"'
    exact = re.compile(r"^" + re.escape(str(invoked)) + r":(\d+):\s+(.+)$")
    assert exact.match(line) is None, (
        "the exact-match anchor this file replaces would already have found "
        "this -- the control above proves nothing")
