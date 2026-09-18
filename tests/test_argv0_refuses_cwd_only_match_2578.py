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

#: Pre-plant a file at where an earlier, reviewed-and-rejected version of
#: this fix would have pointed a refused spawn: a FIXED name under the
#: shared temp directory. That version failed review (#2578) precisely
#: because it was pre-plantable -- an attacker who places a file there once
#: creates a standing backdoor for every future refusal, on the whole host,
#: worse than the bug the fix closes. This constant documents the shape a
#: correct fix must never reproduce; it is not imported from product code.
_REJECTED_FIXED_REFUSAL_DIR = "supertool-2578-refused-cwd-only-match"


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


@pytest.mark.parametrize("resolver", [argv0, _spawnable])
def test_the_refused_path_is_not_reused_across_calls(
        resolver, tmp_path, monkeypatch) -> None:
    """The regression this file exists to pin (#2578 review): the refused
    path must differ on every call, so nothing can be planted in advance
    at wherever it points. A fixed location under a shared, commonly
    world-writable temp directory is pre-plantable -- an attacker who
    places a file there once creates a standing, cross-invocation
    backdoor, which is worse than the bare-name fallback this replaces.
    """
    _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))

    first = resolver(TOOL)
    second = resolver(TOOL)

    assert first != second, (
        "the same refused path came back twice -- that path can be "
        "pre-planted once and reused for every future refusal (#2578)"
    )
    assert _REJECTED_FIXED_REFUSAL_DIR not in first, (
        "resolved to the exact fixed, pre-plantable location review "
        "rejected for #2578"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
