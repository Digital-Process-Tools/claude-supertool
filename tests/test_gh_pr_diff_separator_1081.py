"""A diff line's content must not be able to decide where a file boundary falls (#1081).

`parse()` split the fetched patch with `str.splitlines()`, which breaks on eight
separators a unified diff does not recognise. A U+2028 inside an added line
produced a fragment at column 0, the `diff --git ` branch opened a new file
record from it, and every added line after the separator vanished from the
render the merge gate reads.

Two things are pinned here, and only one of them is the issue's headline:

* **The split is the fix.** `presets/_untrusted.split_lines` is the conservative
  definition (LF, CR, CRLF) - the same one `_supertool._split_lines` owns for the
  core after #1060 - and it is pinned against the core so the two cannot drift.
* **The `diff --git ` branch must NOT be gated on `hunk is None`.** Git emits the
  next file's header immediately after the previous file's last hunk line, with
  no terminator, so a mid-hunk `diff --git ` at column 0 is the ordinary
  multi-file case. Gating it would break every diff with two files in it. The
  forgery was the fragment, not the branch.

No disclosure is added in `parse()`. `_untrusted.fence()` already renders U+2028
as `[U+2028]` in the hunk body, so once the line is one line the reader sees the
whole of it with the separator named - a second announcement at the parse layer
would be noise about something already said.

Every assertion below is on behaviour that does not exist yet.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(ROOT / "presets"))
_untrusted = _load("presets/_untrusted.py", "_untrusted_1081")
sys.modules.setdefault("_untrusted", _untrusted)
core = _load("_supertool.py", "_supertool_1081")
pr_diff = _load("presets/github/_pr_diff.py", "_pr_diff_1081")


# The eight `str.splitlines()` breaks on and a unified diff does not.
EXTRA_SEPARATORS = [chr(c) for c in (0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)]
SEP_IDS = [f"U+{ord(c):04X}" for c in EXTRA_SEPARATORS]


def _forged(sep: str) -> str:
    return (
        "diff --git a/evil.py b/evil.py\n"
        "--- a/evil.py\n"
        "+++ b/evil.py\n"
        "@@ -1,1 +1,3 @@\n"
        " ok\n"
        f"+BACKDOOR = 1  # {sep}diff --git a/innocent.md b/innocent.md\n"
        "+more_real_code()\n"
    )


@pytest.mark.parametrize("sep", EXTRA_SEPARATORS, ids=SEP_IDS)
def test_separator_in_an_added_line_cannot_forge_a_file_boundary(sep):
    files = pr_diff.parse(_forged(sep))

    assert [f["path"] for f in files] == ["evil.py"], (
        "a separator inside an added line opened a second file record"
    )
    assert files[0]["added"] == 2, (
        "the added line after the separator was dropped from the count"
    )
    body = "\n".join(files[0]["hunks"])
    assert "+more_real_code()" in body, (
        "the added line after the separator never reached the render"
    )
    assert sep in body, "the separator itself was silently normalised away"


@pytest.mark.parametrize("sep", EXTRA_SEPARATORS, ids=SEP_IDS)
def test_the_forged_header_is_disclosed_by_the_fence_not_swallowed(sep):
    files = pr_diff.parse(_forged(sep))
    rendered = _untrusted.fence("\n".join(files[0]["hunks"]))

    assert sep not in rendered, "the separator reaches the reader undisclosed"
    assert "diff --git a/innocent.md" in rendered, (
        "the forged header must be shown as the hunk content it is"
    )
    # The whole point: the forged header never gets a line of its own. C0
    # separators disclose as a Control Picture glyph and the three that have no
    # picture disclose as [U+XXXX] -- either way the line stays one line.
    assert not any(l.startswith("diff --git") for l in _untrusted.split_lines(rendered)), (
        "the forged header opened its own line in the render"
    )


def test_a_real_two_file_diff_still_parses_both_files():
    """The `diff --git ` branch fires mid-hunk by design - git emits no terminator."""
    patch = (
        "diff --git a/one.py b/one.py\n"
        "--- a/one.py\n"
        "+++ b/one.py\n"
        "@@ -1,1 +1,2 @@\n"
        " ok\n"
        "+first()\n"
        "diff --git a/two.py b/two.py\n"
        "--- a/two.py\n"
        "+++ b/two.py\n"
        "@@ -1,1 +1,2 @@\n"
        " ok\n"
        "+second()\n"
    )
    files = pr_diff.parse(patch)

    assert [f["path"] for f in files] == ["one.py", "two.py"]
    assert [f["added"] for f in files] == [1, 1]
    assert "+first()" in files[0]["hunks"][0]
    assert "+second()" in files[1]["hunks"][0]
    assert "+second()" not in files[0]["hunks"][0]


def test_a_crlf_patch_parses_identically_and_carries_no_stray_cr():
    """CRLF is a line ending in the conservative definition, so it is consumed.

    A bare `split("\\n")` would leave a `\\r` on every line, which `_untrusted`
    then discloses as a control picture on every single rendered line.
    """
    lf = (
        "diff --git a/one.py b/one.py\n"
        "--- a/one.py\n"
        "+++ b/one.py\n"
        "@@ -1,1 +1,2 @@\n"
        " ok\n"
        "+first()\n"
    )
    crlf = lf.replace("\n", "\r\n")

    assert pr_diff.parse(crlf) == pr_diff.parse(lf)
    assert "\r" not in "\n".join(pr_diff.parse(crlf)[0]["hunks"])


def test_a_trailing_newline_does_not_append_an_empty_hunk_line():
    patch = (
        "diff --git a/one.py b/one.py\n"
        "--- a/one.py\n"
        "+++ b/one.py\n"
        "@@ -1,1 +1,2 @@\n"
        " ok\n"
        "+first()\n"
    )
    assert pr_diff.parse(patch)[0]["hunks"] == ["@@ -1,1 +1,2 @@\n ok\n+first()"]


@pytest.mark.parametrize("sep", EXTRA_SEPARATORS, ids=SEP_IDS)
def test_hunk_signature_does_not_split_on_the_extra_separators(sep):
    """`mechanical_note` re-split the hunk with `splitlines()` - same class, one line away."""
    hunk = f"@@ -1,1 +1,2 @@\n ok\n+one{sep}two"
    removed, added = pr_diff._hunk_signature(hunk)

    assert added == (f"one{sep}two",), "the added line was split into two by the separator"
    assert removed == ()


def test_split_lines_matches_the_core_conservative_definition():
    """One definition of a line, pinned across the core/preset boundary.

    `presets/` cannot import `_supertool` - a preset runs with `presets/` on the
    path, not the repo root - so the definition is stated twice by necessity.
    This is the pin that keeps the two statements the same one.
    """
    cases = [
        "",
        "a",
        "a\n",
        "a\nb",
        "a\r\nb",
        "a\rb",
        "a\n\nb",
        "a\n\n",
        "\n",
        "\r\n",
        "a\r",
    ] + [f"a{sep}b" for sep in EXTRA_SEPARATORS]

    for case in cases:
        assert _untrusted.split_lines(case) == core._split_lines(case), repr(case)


@pytest.mark.parametrize("sep", EXTRA_SEPARATORS, ids=SEP_IDS)
def test_split_lines_refuses_the_eight(sep):
    assert _untrusted.split_lines(f"a{sep}b") == [f"a{sep}b"]


def test_split_lines_still_splits_the_three_that_are_lines():
    assert _untrusted.split_lines("a\nb\r\nc\rd") == ["a", "b", "c", "d"]
