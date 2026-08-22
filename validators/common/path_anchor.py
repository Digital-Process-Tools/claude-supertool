"""Anchoring a diagnostic parser on the path it was actually invoked with,
tolerant of the spellings a real tool's own platform can introduce (#1934,
#1937).

`re.escape(file)` alone assumes the tool echoes that exact string back.
actionlint does not (#1934 -- it relativises against its own CWD; see
actionlint.py's own `_line_re`), and even a tool that DOES try to echo the
same path can still differ in two independent ways that carry no security
meaning: a Windows-only SPELLING difference (`\\` vs `/`, a drive letter's
case), and a canonicalisation difference on ANY platform with symlinks --
the invoked path traverses a symlink (a temp directory is the common case)
and the tool's own diagnostic names the RESOLVED path instead of the one it
was actually given. Real ruby went red on windows-latest CI first (#1937,
separator/case) and then on ubuntu-latest (twice, real ruby, ubuntu ~
3.10/3.11) with the identical `assert None is not None` shape -- which
falsified "Windows-specific" as the cause for that second failure before a
Windows-only fix could be trusted to cover it. Both are the same underlying
defect at the design level: a real, located diagnostic silently losing its
line and column because the anchor assumed exact-echo. That is strictly
worse than the #1934 misreport it replaced: a wrong line is still a line,
and this was none.

Two widenings, each gated to where it is actually safe:

- `path_variants` -- spelling only (separator, drive-letter case), GATED TO
  WINDOWS. `\\` and a leading `X:single-letter` are ordinary, unremarkable
  filename characters on POSIX: a real file can be named `weird\\name.xml`
  or `X:secret` on Linux/macOS, and the first cut of this module treated
  either as a separator/drive-letter unconditionally, which let the anchor
  accept a genuinely DIFFERENT file's diagnostic as this one's -- the exact
  misattribution #1934 was filed to prevent, reopened one layer over, and
  found by this branch's own self-review before it shipped. On Windows a
  colon cannot appear in a filename except as the drive separator, so the
  same check is unambiguous there.

- `anchor`'s `extra_paths` -- canonicalisation, UNGATED (every platform).
  `os.path.realpath(file)`, computed by the CALLER (this module touches no
  filesystem itself, to stay testable without one) and passed in, is a
  deterministic function of `file` and the real filesystem alone -- never
  of the tool's output, which is where a forgery would have to live. Two
  files that are not symlink-equivalent always have different realpaths, so
  this cannot manufacture a collision the way an ungated spelling transform
  did: there is no POSIX path for which `os.path.realpath` returns a
  DIFFERENT real file's canonical path.

A forged filename cannot use either widening to impersonate a different
path: every accepted string is still a transform of `file` itself (directly,
or via the filesystem's own symlink structure), gated to where that
transform is meaningful, so what changes is which SPELLINGS of the real
path are accepted, never which paths are. `path_variants` is pure string
manipulation with no filesystem access, so its Windows-only half can be (and
is, in `tests/test_adapter_path_anchor_1937.py`) exercised against
Windows-shaped paths on any platform via the explicit `platform=` override
every function here accepts; the realpath half is exercised there too, with
a real local symlink, which needs no CI machine of any particular OS to
create.

What neither widening attempts: an 8.3 short filename (`RUNNER~1`) has no
general, filesystem-free way to expand back to its long form, so a tool
that prints one is not covered here. That residual gap is a documented,
narrower one than what has actually been observed in CI, and every adapter
using this module already has a designed fallback for "found output, could
not anchor a location in it" -- an unlocated finding naming the raw output,
never a silent `count: 0` (see the `if not errors and output:` /
`if not errors:` branch in each adapter's `main()`). Refusing with a stated
reason, not guessing, is the chosen answer for whatever these two widenings
still do not cover.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

# This module lives in validators/common/ alongside linebreaks.py, but
# unlike the five adapters that import it, nothing guarantees a caller
# loading path_anchor.py directly (as this module's own test files do) has
# already put that directory on sys.path -- so it does so for itself,
# mirroring the convention every adapter already uses one directory up.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from linebreaks import split_lines


def safe_realpath(file: str) -> str | None:
    """`os.path.realpath(file)`, or `None` if the OS refuses to answer.

    The one filesystem touch this module offers, kept as an explicit,
    separately-callable opt-in rather than folded into `path_variants`/
    `anchor` themselves, so those stay pure and callable with no filesystem
    at all -- what most of this module's own tests rely on. `realpath`
    resolves as far as it can even for a path that does not exist, but it
    is NOT exception-free: an embedded NUL byte raises `ValueError`
    (verified: `os.path.realpath("a\\x00b")` -> `ValueError: lstat:
    embedded null character in path`), not `OSError` -- a gap a self-review
    caught in the first cut of this function, which caught only `OSError`
    and let that one through uncaught, contradicting its own claim to never
    do so. `guard_main` (`refusal.py`) is a second, independent net around
    every adapter's whole `main()` and would have turned that escape into a
    distinguishable crash receipt rather than a silent clean result -- so
    this was defended in depth, not reachable end to end from the adapter
    CLI (a NUL byte cannot appear in a POSIX argv element at all) -- but a
    caller building an anchor from output already in hand should not have
    to rely on a second net for a filesystem surprise this function exists
    to absorb. `None` here means "no extra candidate", not "an error".
    """
    try:
        return os.path.realpath(file)
    except (OSError, ValueError):
        return None


def _is_windows(platform: str | None) -> bool:
    """`platform` overrides `sys.platform` when given -- the hook that lets
    this module's Windows-only widening be exercised from a POSIX test
    runner without touching the process's own platform."""
    return (platform if platform is not None else sys.platform).startswith("win")


def path_variants(file: str, platform: str | None = None) -> list[str]:
    """Every SPELLING of `file` a diagnostic parser should accept as this
    adapter's own invoked path, de-duplicated, `file` itself always first.

    Widening (separator direction; a leading drive letter's case) is applied
    ONLY when `platform` (or, if not given, `sys.platform`) is Windows --
    see the module docstring for why: both transforms are unsafe on POSIX,
    where the characters they key off are ordinary filename content. This
    function touches no filesystem; see `anchor`'s `extra_paths` for the
    other, ungated widening (canonicalisation) this module offers.
    """
    if not _is_windows(platform):
        return [file]
    variants = [file]
    for v in (file.replace("\\", "/"), file.replace("/", "\\")):
        if v not in variants:
            variants.append(v)
    for v in list(variants):
        if len(v) >= 2 and v[1] == ":" and v[0].isalpha():
            flipped = (v[0].lower() if v[0].isupper() else v[0].upper()) + v[1:]
            if flipped not in variants:
                variants.append(flipped)
    return variants


def anchor(file: str, tail: str, platform: str | None = None,
           extra_paths: list[str] | None = None) -> re.Pattern[str]:
    """`^(?:variant1|variant2|...)` + `tail`, anchored on every accepted
    spelling of `file` AND of every path in `extra_paths` (typically just
    `os.path.realpath(file)` -- see the module docstring). `tail` is the
    adapter's own regex for everything after the path, including its own
    trailing `$`. `platform` overrides `sys.platform` -- see `path_variants`,
    and applies identically to `file` and to every entry of `extra_paths`.

    The caller computes `extra_paths`, never this function: `os.path.realpath`
    touches the filesystem, and keeping that call at the caller means this
    module stays testable -- including its Windows-only half -- without a
    filesystem detour on every call.
    """
    bases = [file] + list(extra_paths or [])
    all_variants: list[str] = []
    for base in bases:
        for v in path_variants(base, platform):
            if v not in all_variants:
                all_variants.append(v)
    alternatives = "|".join(re.escape(v) for v in all_variants)
    return re.compile(r"^(?:" + alternatives + r")" + tail)


# ---------------------------------------------------------------------------
# When the anchor still matches nothing (#1937, third CI round). Three
# rounds of guessing the transform from an assertion failure and a raw exit
# code -- Windows spelling, then symlink canonicalisation, both reasoned
# from real evidence and both incomplete -- is the cost of an adapter that
# never said what it saw. These two functions do not widen the anchor
# further; they make a MISS legible, so the next red prints the two strings
# that differ instead of handing back a third hypothesis to test.
# ---------------------------------------------------------------------------

#: The SAME non-greedy shape #1934 removed from location-extraction -- safe
#: to reuse here because nothing built from it is ever treated as a
#: verdict. It feeds one string into a message a human reads, never a
#: `line`/`col`/`code` a caller trusts.
_LOOSE_LEADING_PATH_RE = re.compile(r"^(.*?):\d+\b")


def unanchored_path_hint(output: str) -> str | None:
    """A best-effort path-looking prefix from the FIRST line of `output`
    that has one, or `None` when nothing does. Diagnostic only: this
    extracts whatever the tool's own text happens to put before the first
    `:digit`, unverified against anything, on purpose -- verifying it is
    exactly what the anchor already does, and this function exists for the
    case where that verification just failed.
    """
    for line in split_lines(output):
        m = _LOOSE_LEADING_PATH_RE.match(line)
        if m:
            return m.group(1)
    return None


def anchor_miss_message(file: str, output: str, fallback: str) -> str:
    """`fallback` is what an adapter would otherwise say for a non-zero
    exit with nothing anchored. When `output` looks like it names a
    DIFFERENT path than `file` -- the anchor missed rather than the tool
    staying silent -- this prefixes `fallback` with both strings side by
    side. When it does not (the hint matches `file`, or there is no
    path-shaped hint at all, or `output` is empty), `fallback` is returned
    UNCHANGED: there is nothing to compare, and claiming a comparison that
    found nothing would be its own small version of the defect this
    function exists to fix.
    """
    hint = unanchored_path_hint(output) if output else None
    if hint is None or hint == file:
        return fallback
    return (f"anchor matched no accepted spelling of the invoked path "
            f"{file!r}; the tool's own output appears to start with "
            f"{hint!r} instead -- {fallback}")
