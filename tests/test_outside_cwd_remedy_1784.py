"""The `path escapes cwd` remedy named two config edits and omitted the
third: moving cwd for the one call, via the `cwd:PATH` op prefix, which is
what both agents who filed #1784 item 5 actually did and which leaves no
residue in either repository's `.supertool.json` (#1784).

Before this fix, `_ALLOW_OUTSIDE_HINT` named only the two knobs that widen
what the *diagnosed* repository's config permits for every future call.
Reproducing a one-off read of a file in another root, that is the wrong
lever: the caller wants to change what THIS call resolves against, not what
every future call may reach.
"""

from __future__ import annotations

import supertool

NL = chr(10)


def test_outside_cwd_remedy_names_cwd_prefix_first(monkeypatch, tmp_path) -> None:
    """The remedy must name `cwd:PATH` -- no config edit, no residue."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "f.txt"
    target.write_text("hi" + NL, encoding="utf-8")
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.chdir(box)

    out = supertool.dispatch("read:" + supertool._fwd(str(target)))

    assert "escapes cwd" in out, out
    assert "cwd:PATH" in out, (
        "the remedy must name the no-residue option -- moving cwd for this "
        "one call -- not only the two knobs that widen every future call:"
        + NL + out
    )


def test_outside_cwd_remedy_still_names_the_config_knobs(monkeypatch, tmp_path) -> None:
    """The two existing knobs are still named -- this adds a line, not a swap."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "f.txt"
    target.write_text("hi" + NL, encoding="utf-8")
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.chdir(box)

    out = supertool.dispatch("read:" + supertool._fwd(str(target)))

    assert "SUPERTOOL_ALLOW_OUTSIDE_CWD=1" in out, out
    assert "allow_outside_cwd" in out, out
