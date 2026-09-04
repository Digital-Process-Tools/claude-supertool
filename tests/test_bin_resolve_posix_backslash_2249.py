r"""#2249 -- the backslash-to-slash normalisation in resolve_bin_cmd must
only run on Windows (os.name == "nt"). A literal backslash is a legal,
ordinary filename character on POSIX, so rewriting it there silently
resolves a DIFFERENT path than the one configured.

POSIX-only: a literal backslash is not expressible as a distinct path
component on Windows (the filesystem itself treats it as a separator), so
this fixture is skipped there rather than adapted.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "validators" / "common"))

from bin_resolve import resolve_bin_cmd  # noqa: E402


@pytest.mark.skipif(
    os.name == "nt",
    reason="a literal backslash is not a distinct POSIX path component on Windows",
)
def test_posix_path_with_literal_backslash_round_trips_unchanged(tmp_path):
    # A backslash in a POSIX filename is ordinary, not an escape.
    weird_dir = tmp_path / r"we\ird"
    weird_dir.mkdir()
    fake_bin = weird_dir / "glab"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    result = resolve_bin_cmd(str(fake_bin), "glab")

    assert result == [str(fake_bin)]


@pytest.mark.skipif(
    os.name == "nt",
    reason="a literal backslash is not a distinct POSIX path component on Windows",
)
def test_posix_backslash_path_that_does_not_exist_is_not_slash_mangled():
    result = resolve_bin_cmd(r"/opt/we\ird/glab", "glab")

    # Must NOT be rewritten to /opt/we/ird/glab (a different, wrong path).
    assert result != ["/opt/we/ird/glab"]
