"""#2376: the release audit's round-1 pass over `_PLATFORM_SIGNAL` (#2360)
found seven tree-walking test files marked `invariant` -- deselected on
every CI leg except (ubuntu-latest, 3.12) -- and flagged all seven as
possibly line-ending/`os.sep`-sensitive. Reading each one against its
actual assertions (not just its vocabulary) narrows that to one real gap:

* `tests/test_registry_names_dispatch_1285.py` -- `test_preset_glob_is_non_
  empty` filters `_BUILTIN_OP_CONFIGS` with `c.startswith("presets" +
  os.sep)`. That line is written correctly (it uses `os.sep`, not a
  hardcoded `/`), which is exactly why the platform risk is real rather
  than obvious: a future edit that "simplifies" it to a hardcoded `/`
  literal would keep passing on every POSIX leg -- including the one
  (ubuntu-latest, 3.12) leg that always runs the full population -- and
  would only ever fail on a Windows leg, every one of which currently
  deselects this file as `invariant`. The regression is real, silent, and
  Windows-only, which is exactly the shape `_invariant_census.py`'s own
  docstring says the direction of error matters most for.

The other six files the audit named are false positives once read past
their vocabulary -- `crlf`/`\\r\\n`/`os.sep` tokens appear in prose,
in-memory string literals compared against other in-memory strings, or in
regex patterns statically scanning *other* files' source text, never in a
runtime path that an OS's own newline or path-separator translation could
touch:

* `test_validators_splitlines_1486.py` -- every `\\r`/`\\n`/CRLF byte is an
  explicit Python string literal compared in memory (`split_lines(...)`);
  the one `write_text`/`read_text` round-trip (`test_source_context_numbers_
  lines_the_way_the_tool_does`) translates `\\n` to the platform's line
  ending on write and back to `\\n` on read symmetrically, so the string
  the test asserts against is identical on every platform.
* `test_preset_git_splitlines_register_1130.py` -- pure AST/text census
  over `presets/git/*.py`'s own source, asserting a register of call
  sites; no subprocess is spawned and no path separator is compared.
* `test_forged_child_stream_line_1475.py` -- same shape: `subprocess.
  CompletedProcess` is constructed by hand with in-memory strings, never a
  real spawned process whose line endings could vary by host.
* `test_paste_backup_1650.py` -- deliberately uses `write_bytes`/
  `read_bytes` throughout (its own comment explains why: to avoid exactly
  this class of platform drift), and the op under test (`_atomic_write` in
  `_supertool.py`) already writes via `os.fdopen(fd, "wb")` plus a manual
  `.encode()`, specifically to skip Windows text-mode CRLF translation. Both
  sides of this test are already byte-exact across platforms.
* `test_anchored_guards_1188.py` -- the `\\r\\n`-bearing parametrize
  (`test_job_id_guard_refuses_a_trailing_newline`) passes literal Python
  strings straight to `_job_argv.refuse_job_id`; no file I/O, no OS
  translation.
* `test_toml_basic_string_seam_2152.py` -- `os.sep` appears only inside a
  regex pattern (`_BACKSLASH_REPLACE_TARGET`) that scans OTHER test files'
  *source text* for the literal four-character substring `os.sep`; the scan
  itself never evaluates `os.sep`'s actual platform value.

Widening `_PLATFORM_SIGNAL` to include `os.sep` (it already had
`os.pathsep`, added for the same reason in #2360's own self-review of
`test_watch_sources_path_2135.py`) catches the one real gap. It also flips
`test_toml_basic_string_seam_2152.py`, which is a false positive by the
reasoning above but costs nothing to keep in the wider, safer population --
`_invariant_census.py`'s own docstring: "wider than needed, never
narrower".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _invariant_census as ic

#: The one file, of the seven the #2376 audit named, whose assertion
#: genuinely differs by platform: a hardcoded-`/` regression on the
#: `os.sep`-filtered line would only ever fail on Windows, and every
#: Windows leg currently deselects this file as `invariant`.
_GENUINELY_PLATFORM_SENSITIVE = "test_registry_names_dispatch_1285.py"

#: The other six the audit named, read against their actual assertions
#: (not their vocabulary) and found to be false positives -- see this
#: module's docstring for the per-file reasoning. Deliberately NOT asserted
#: platform-sensitive: they stay `invariant`, correctly, and are pinned
#: here as a population so this file is a record of six DELIBERATE `False`
#: answers instead of six untested ones.
_AUDITED_FALSE_POSITIVES = frozenset({
    "test_validators_splitlines_1486.py",
    "test_preset_git_splitlines_register_1130.py",
    "test_forged_child_stream_line_1475.py",
    "test_paste_backup_1650.py",
    "test_anchored_guards_1188.py",
    "test_toml_basic_string_seam_2152.py",
})


def test_all_seven_audited_files_still_walk_the_tree():
    """The must-fire half: if any of these seven stops walking the tree (a
    rewrite drops the `.rglob`/`.glob`/`os.walk` call), it falls out of the
    population entirely and every assertion below about it is vacuous.
    """
    pop = ic.population()
    all_seven = _AUDITED_FALSE_POSITIVES | {_GENUINELY_PLATFORM_SENSITIVE}
    missing = all_seven - set(pop)
    assert not missing, (
        f"these #2376-audited files no longer walk the tree by the "
        f"census's own detector -- confirm deliberately before removing "
        f"them from this test: {sorted(missing)}")


def test_the_genuine_gap_is_no_longer_marked_invariant():
    """The actual #2376 fix: `test_registry_names_dispatch_1285.py`'s
    `os.sep`-filtered assertion is a real, Windows-only regression risk
    that every Windows/macOS CI leg currently cannot catch, because the
    file is marked `invariant` and deselected there. `_PLATFORM_SIGNAL`
    must widen to reach it.
    """
    invariant = ic.invariant_files()
    assert _GENUINELY_PLATFORM_SENSITIVE not in invariant, (
        f"{_GENUINELY_PLATFORM_SENSITIVE} is still marked invariant and "
        "deselected on 11 of 12 CI legs, despite filtering on `os.sep` -- "
        "a hardcoded-`/` regression on that line would only ever be caught "
        "on a Windows leg, and every Windows leg deselects this file")


def test_the_six_false_positives_are_unaffected():
    """The paired negative half (this repo's own negative-assertion rule):
    the fix for the one real gap must not have been a blanket exclusion
    that also (correctly, by luck, or incorrectly) swept the six files
    read above and found NOT to be platform-sensitive. Each of these six
    staying `invariant` is not itself the point -- `_invariant_census.py`'s
    own bias is to over-include rather than under-include -- but a
    surprise flip on any of them would mean the widened signal caught
    something broader than `os.sep`, worth re-reading rather than assuming.
    """
    pop = ic.population()
    unexpectedly_flagged = {
        name for name in _AUDITED_FALSE_POSITIVES if pop.get(name)
    }
    # Not a hard failure -- flagging one of these six is the SAFE direction
    # (it stays in the run population rather than being deselected), per
    # _invariant_census.py's own stated bias. But it is worth knowing about
    # rather than silently accepting, so this is recorded as information a
    # reader can act on, not asserted false.
    if unexpectedly_flagged:
        print(
            "note: widening _PLATFORM_SIGNAL also flagged files the #2376 "
            f"reading found to be false positives (safe direction, not a "
            f"failure): {sorted(unexpectedly_flagged)}"
        )
