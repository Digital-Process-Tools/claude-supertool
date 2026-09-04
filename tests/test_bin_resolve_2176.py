r"""#2176 -- an unquoted single binary path containing a space (the default
Windows install location, e.g. `C:\Program Files\glab\glab.exe`) must
resolve as ONE path, not get shlex-split at the space into two tokens.

Cross-platform on purpose: the bug is in string handling, not in whether a
real `glab`/`ruff` binary is installed, so this exercises
`validators/common/bin_resolve.resolve_bin_cmd` directly rather than
spawning a real toolchain.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "validators" / "common"))

import bin_resolve  # noqa: E402
from bin_resolve import resolve_bin_cmd  # noqa: E402


def _make_executable(path: Path) -> None:
    path.write_text("")
    path.chmod(0o755)


def test_program_files_style_path_with_space_is_not_split_at_the_space(tmp_path):
    bin_dir = tmp_path / "Program Files" / "glab"
    bin_dir.mkdir(parents=True)
    fake_bin = bin_dir / "glab.exe"
    _make_executable(fake_bin)

    result = resolve_bin_cmd(fake_bin.as_posix(), "glab")

    assert result == [fake_bin.as_posix()]


def test_windows_style_backslashes_in_a_spaced_path_still_resolve(tmp_path, monkeypatch):
    bin_dir = tmp_path / "Program Files" / "glab"
    bin_dir.mkdir(parents=True)
    fake_bin = bin_dir / "glab.exe"
    _make_executable(fake_bin)

    # Simulate the literal env-var value an operator would set on Windows:
    # the same path, but with backslash separators instead of forward ones.
    windows_style = str(fake_bin).replace(os.sep, "\\") if os.sep != "\\" else str(fake_bin)

    # The backslash-to-forward-slash rewrite is now gated on os.name == "nt"
    # (#2249): a literal backslash is an ordinary POSIX filename character,
    # not a Windows path separator, so it must not be rewritten when this
    # suite happens to run on a POSIX host. Force the Windows branch here
    # to keep this fixture exercising it regardless of the runner's own
    # platform.
    monkeypatch.setattr(bin_resolve.os, "name", "nt")

    result = resolve_bin_cmd(windows_style, "glab")

    assert result == [fake_bin.as_posix()]


def test_quoted_multi_token_command_line_still_splits(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text("")
    import shlex
    quoted = f"{shlex.quote(sys.executable)} {shlex.quote(stub.as_posix())}"

    result = resolve_bin_cmd(quoted, "glab")

    # resolve_bin_cmd forward-slashes its input before splitting (#2176),
    # so a backslash-separated sys.executable (Windows) comes back with
    # forward slashes -- compare against the SAME normalization, not the
    # raw value, or this assertion only holds by accident on POSIX.
    assert result == [sys.executable.replace("\\", "/"), stub.as_posix()]


def test_default_used_when_env_var_unset():
    assert resolve_bin_cmd("", "glab") == ["glab"]


def test_plain_name_with_no_path_and_no_binary_falls_back_to_split():
    result = resolve_bin_cmd("glab-that-does-not-exist-xyz", "glab")
    assert result == ["glab-that-does-not-exist-xyz"]
