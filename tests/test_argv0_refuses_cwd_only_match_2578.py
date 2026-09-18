"""A cwd-only match must not reach `subprocess` as a bare name (#2578).

Follow-up from #2575 / PR #2577's own review: `argv0()` and `_spawnable()`
both ended `which_excluding_cwd(name) or name`. `which_excluding_cwd()`
correctly refuses a match that only exists via the implicit cwd search, but
the `or name` fallback then hands the refused bare name straight back --
and on Windows, `CreateProcess` performs its own PATH+cwd search, so the
bare name resolves the very match `which_excluding_cwd()` just refused.

This is deliberately asked as "does a spawn attempt with the returned value
raise `FileNotFoundError`", not "is the string different from `name`" --
the latter is satisfied by nearly anything, including a value that still
somehow spawns.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "validators" / "common"))
from bin_resolve import _spawnable  # noqa: E402
from spawnable import argv0  # noqa: E402

TOOL = "st-probe-2578.cmd"


def _shim(d: "pathlib.Path", name: str = TOOL) -> "pathlib.Path":
    d.mkdir(parents=True, exist_ok=True)
    shim = d / name
    shim.write_text("", encoding="utf-8")
    shim.chmod(0o755)
    return shim


@pytest.mark.parametrize("resolver", [argv0, _spawnable])
def test_a_cwd_only_match_never_reaches_subprocess_as_the_bare_name(
        resolver, tmp_path, monkeypatch) -> None:
    """The positive check the earlier bare-name-returned tests could not
    make: attempt to actually spawn what came back, and require the
    specific failure a genuinely-absent tool would also produce.
    """
    _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))

    resolved = resolver(TOOL)

    assert resolved != TOOL, (
        "the bare name came back unchanged -- on Windows this is handed "
        "straight to CreateProcess, which performs its own cwd search and "
        "would run the planted shim this module exists to refuse"
    )
    with pytest.raises(FileNotFoundError):
        subprocess.run([resolved], capture_output=True)


@pytest.mark.parametrize("resolver", [argv0, _spawnable])
def test_a_genuinely_absent_tool_still_returns_the_bare_name(
        resolver, tmp_path, monkeypatch) -> None:
    """The negative control: nothing here should touch the case #2540 and
    #2176 already pin -- a tool absent from PATH *and* cwd is not the
    #2578 shape, and the bare-name passthrough for it must be unchanged.
    """
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", str(empty))
    name = "st-definitely-not-a-tool-2578"

    assert resolver(name) == name


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
