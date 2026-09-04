r"""#2250 -- when resolve_bin_cmd's existence check fails and the shlex-split
fallback changes the value's shape, an operator-facing "not found" message
must disclose the ORIGINAL configured value alongside the split result --
not just the mangled/split fragment, which can look like a completely
different (and wrong) path.

`describe_unresolved(raw, resolved)` is the helper that builds that
disclosure. It is exercised two ways here:

* directly, with a plain POSIX-reachable multi-token value (no platform
  patching needed -- a spaced value that doesn't exist gets shlex-split on
  every platform);
* against the literal Windows repro from the issue
  (`C:\Program Files\glab\glab.exe`), with `os.name` forced to "nt" so the
  #2249-gated backslash rewrite actually fires and reproduces the exact
  mangled shape the issue reports, regardless of the host this suite runs
  on.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "validators" / "common"))

import bin_resolve  # noqa: E402
from bin_resolve import describe_unresolved, resolve_bin_cmd  # noqa: E402


def test_unchanged_resolution_is_reported_bare():
    # bin_cmd[0] round-trips to the raw value -- nothing to disclose.
    assert describe_unresolved("glab", "glab") == "glab"


def test_empty_raw_is_reported_bare():
    assert describe_unresolved("", "glab") == "glab"


def test_split_that_changes_shape_discloses_the_original_value():
    raw = "totally bogus path with spaces"
    bin_cmd = resolve_bin_cmd(raw, "glab")

    # The existence check fails (no such file), so this falls through to
    # the shlex-split fallback, which changes bin_cmd[0] from the full raw
    # string to just its first token.
    assert bin_cmd[0] != raw

    described = describe_unresolved(raw, bin_cmd[0])

    assert bin_cmd[0] in described
    assert raw in described


def test_windows_program_files_split_discloses_the_configured_path(monkeypatch):
    monkeypatch.setattr(bin_resolve.os, "name", "nt")

    raw = r"C:\Program Files\glab\glab.exe"
    bin_cmd = resolve_bin_cmd(raw, "glab")

    # Reproduces the issue's exact mangled shape.
    assert bin_cmd == ["C:/Program", "Files/glab/glab.exe"]

    described = describe_unresolved(raw, bin_cmd[0])

    # The split fragment alone AND the original configured value must both
    # be visible -- an operator seeing only "C:/Program" cannot tell a
    # typo from a genuine multi-token command.
    assert bin_cmd[0] in described
    assert raw in described
