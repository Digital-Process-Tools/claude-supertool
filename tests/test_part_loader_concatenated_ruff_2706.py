"""The real undefined-name check for a part (#2706), as ruff cannot run it.

`_supertool_guard.py` -- and any future `_load_part`-loaded part -- is
`exec(code, globals())`'d into `_supertool.py`'s own namespace at runtime, so
a name it uses that the core (or an earlier part) defines is genuinely bound
by the time it runs. ruff lints each file standalone and has no way to see
that, so `pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]` silences F821
for the part file -- which buys back the false positive at the cost of losing
the one case that silencing is supposed to risk: a REAL undefined name (a
typo, a name that moved away) inside the part itself.

This file is what closes that gap: it builds one synthetic source by
concatenating `_supertool.py` with every part's body (header boilerplate --
docstring, `from __future__ import annotations`, the standalone-import guard
-- stripped, since literally splicing a second `from __future__ import` or a
dangling `if` into the middle of one file would itself be the artifact, not
a finding about the real code), in load order, and runs ruff's F821/F811
over THAT. Proven by actually planting a typo (see the test below) and
watching ruff catch it, rather than trusting that the mechanism would.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _core_sources import core_source_paths, part_call_sites  # noqa: E402

RUFF = shutil.which("ruff")


def _part_body_lines(path: Path) -> list:
    """`path`'s own lines, minus the header: docstring, `__future__` import,
    and the standalone-import guard `if` block. What is left is exactly the
    real code ruff needs to see once, in the concatenated file, without a
    second `from __future__ import` or a dangling bare `if` confusing the
    parser."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    lines = text.splitlines(keepends=True)
    idx = 0
    body = tree.body
    if (idx < len(body) and isinstance(body[idx], ast.Expr)
            and isinstance(body[idx].value, ast.Constant)
            and isinstance(body[idx].value.value, str)):
        idx += 1
    if (idx < len(body) and isinstance(body[idx], ast.ImportFrom)
            and body[idx].module == "__future__"):
        idx += 1
    if idx < len(body) and isinstance(body[idx], ast.If):
        idx += 1
    if idx >= len(body):
        return []
    start = body[idx].lineno - 1
    return lines[start:]


def _concatenated_source() -> str:
    """Core + every part's body, each part SPLICED IN at its own call site.

    Not core-then-every-part-appended-at-the-end: `_load_part` executes each
    part at the exact line its call occupies, and a part loaded near the HEAD
    of the file, used by code throughout the rest of it (the config/presets/
    env-knobs/exclude/gitignore/rtk/display part, #2706), has most of its
    real callers sitting between its own call site and the file's tail --
    appending its body after the whole core (and after every other part) puts
    its definitions textually AFTER code that already uses them, which is a
    fabricated F821 this test would otherwise report as a real one. Splicing
    at the call site is what `exec(code, globals())` actually does, so it is
    the one construction that cannot invent an ordering defect of its own.
    """
    paths = core_source_paths()
    core = paths[0]
    bodies = {part.stem: "".join(_part_body_lines(part)) for part in paths[1:]}
    core_text = core.read_text(encoding="utf-8")
    sites = part_call_sites(core_text)
    lines = core_text.splitlines(keepends=True)
    by_line = {lineno: name for lineno, name in sites}
    out = []
    for i, line in enumerate(lines, start=1):
        name = by_line.get(i)
        if name is not None and name in bodies:
            out.append(bodies[name])
        else:
            out.append(line)
    return "".join(out)


def _ruff_undefined_names(source: str, tmp_path: Path) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "_concatenated_core.py"
    target.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [RUFF, "check", "--select", "F821,F811", "--no-cache",
         "--isolated", str(target)],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    return proc.stdout + proc.stderr


@pytest.mark.skipif(RUFF is None, reason="ruff not on PATH")
def test_the_real_tree_has_no_undefined_name_across_the_split(tmp_path: Path):
    """The actual check: core + every part, concatenated, is clean."""
    out = _ruff_undefined_names(_concatenated_source(), tmp_path)
    assert "F821" not in out and "F811" not in out, (
        "the concatenated core + parts has an undefined-name or "
        "redefinition finding ruff's per-file F821 silencing on the part "
        "hides from a normal run:" + chr(10) + out)


@pytest.mark.skipif(RUFF is None, reason="ruff not on PATH")
def test_a_planted_typo_in_the_part_is_caught(tmp_path: Path):
    """Proof, not trust: the mechanism is shown catching a real defect.

    `_guard_normalise` is defined once, inside the guard part, and called
    from `guard_command` a few hundred lines below in that same file.
    Misspelling the call site is exactly the shape of mistake the per-file
    F821 ignore on `_supertool_guard.py` would otherwise hide forever.
    """
    source = _concatenated_source()
    planted = source.replace(
        "_guard_normalise(argv, heads)",
        "_guard_normalisz_typo_2706(argv, heads)", 1)
    assert planted != source, "the name to typo was not found in the source"

    red = _ruff_undefined_names(planted, tmp_path / "red")
    assert "_guard_normalisz_typo_2706" in red and "F821" in red, (
        "planting an undefined name did not produce an F821 finding -- the "
        "concatenated-ruff check is not actually running:" + chr(10) + red)

    green = _ruff_undefined_names(source, tmp_path / "green")
    assert "_guard_normalisz_typo_2706" not in green, (
        "the untouched source should not mention the planted name at all")
