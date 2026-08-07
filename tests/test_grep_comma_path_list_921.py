"""`grep` handed a comma-separated path list (#921).

`git-resolve` accepts `PATH[,PATH...]`, so reaching for the same spelling in
`grep` is the convention the operator was taught. `grep` joins it into one
filename, fails, and offers `cwd:PATH` — the one remedy that provably cannot
apply, since every file in the list resolves from the current directory. The
list is still refused; what changes is that the refusal names the real cause.
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _files(tmp_path: Path, *names: str) -> list[Path]:
    made = []
    for n in names:
        f = tmp_path / n
        f.write_text("needle here\n")
        made.append(f)
    return made


def test_comma_list_is_named_not_blamed_on_cwd(tmp_path: Path) -> None:
    a, b, c = _files(tmp_path, "a.py", "b.py", "c.py")
    out = supertool.dispatch(f"grep:needle:{a},{b},{c}:10:0")
    assert "wrong CWD?" not in out, out
    assert "comma" in out.lower(), out


def test_comma_list_says_all_three_exist(tmp_path: Path) -> None:
    a, b, c = _files(tmp_path, "a.py", "b.py", "c.py")
    out = supertool.dispatch(f"grep:needle:{a},{b},{c}:10:0")
    assert "all 3" in out.lower(), out


def test_comma_list_none_existing_keeps_the_cwd_hint(tmp_path: Path) -> None:
    """No entry resolves — this is an ordinary bad path, and the generic
    cwd advice is still the honest default. Do not trade one misreport for
    another."""
    out = supertool.dispatch(f"grep:needle:{tmp_path}/x.py,{tmp_path}/y.py:10:0")
    assert "wrong CWD?" in out, out


def test_plain_missing_path_is_unchanged(tmp_path: Path) -> None:
    out = supertool.dispatch(f"grep:needle:{tmp_path}/nope.py:10:0")
    assert "wrong CWD?" in out, out
    assert "comma" not in out.lower(), out


def test_single_real_path_still_greps(tmp_path: Path) -> None:
    (a,) = _files(tmp_path, "a.py")
    out = supertool.dispatch(f"grep:needle:{a}:10:0")
    assert "needle here" in out, out
