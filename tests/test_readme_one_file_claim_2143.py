"""#2143 -- README.md line 16 said "One Python file, zero deps, Python 3.9+."

That was true in April. `supertool.py` is now a 171-line launcher; the core
lives in `_supertool.py` plus `presets/`, `validators/`, `formatters/` and
`notifiers/` -- 1,000+ tracked Python files. "Zero deps" is still true for the
runtime, but "one Python file" is the first claim a stranger can test with
`ls` and it fails.

This does not pin an exact file count -- the count grows on every preset
added, and pinning it would just move the drift into the test. What it pins
is the retracted claim (never say "one Python file" again) and the structural
claim that replaced it (a launcher delegating to a core module), checked
against the files that actually exist on disk.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"


def _lede() -> str:
    text = README.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "Saves tokens" in line and "Saves money" in line:
            return line
    raise AssertionError("no 'Saves tokens. Saves money.' lede line found in README.md")


def test_the_readme_no_longer_claims_one_python_file() -> None:
    lede = _lede()
    assert "One Python file" not in lede, lede
    assert "one Python file" not in lede, lede


def test_the_readme_names_the_launcher_and_core_module_that_exist() -> None:
    lede = _lede()
    assert "supertool.py" in lede, lede
    assert "_supertool.py" in lede, lede
    assert (ROOT / "supertool.py").is_file()
    assert (ROOT / "_supertool.py").is_file()


def test_the_readme_still_claims_zero_deps_and_the_python_floor() -> None:
    # "zero deps" is the claim that matters and is still true -- stdlib only.
    lede = _lede()
    assert "zero deps" in lede, lede
    assert "Python 3.9+" in lede, lede


def test_the_design_decisions_bullet_matches_supertool_pys_actual_line_count() -> None:
    # The reviewer spawned on this fix (#2143) found a second, adjacent stale
    # number in the same file: the "Design decisions" section separately
    # claimed supertool.py was "~80 lines" -- also wrong, and wrong in the
    # same direction (undercounting) as the retracted lede this test file
    # already pins. Rather than hardcode the corrected number and let it
    # drift again, this reads the actual line count and checks the bullet
    # against it, so the bullet cannot go stale silently a third time.
    text = README.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("- **Two files, one of them a shim.**"):
            bullet = line
            break
    else:
        raise AssertionError("no 'Two files, one of them a shim' bullet found in README.md")
    launcher_lines = len((ROOT / "supertool.py").read_text(encoding="utf-8").splitlines())
    assert f"is {launcher_lines} lines" in bullet, (
        bullet, f"expected 'is {launcher_lines} lines' (supertool.py's real line count)"
    )


def test_supertool_py_is_a_thin_launcher_not_the_whole_implementation() -> None:
    # Ground truth for the claim above: if supertool.py ever grows back into
    # the whole implementation, "a thin launcher" is the sentence that goes
    # stale next, the same way "one Python file" did.
    launcher_lines = (ROOT / "supertool.py").read_text(encoding="utf-8").count("\n")
    core_lines = (ROOT / "_supertool.py").read_text(encoding="utf-8").count("\n")
    assert launcher_lines < 500, (
        f"supertool.py has grown to {launcher_lines} lines -- it may no "
        "longer be accurate to call it a thin launcher in README.md"
    )
    assert core_lines > launcher_lines
