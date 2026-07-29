"""#478 — a syntax floor guard that runs a real old interpreter.

PR #473 shipped nested same-type quotes inside an f-string replacement field:
PEP 701 syntax, legal on 3.12+, a SyntaxError on 3.9/3.10/3.11. Nine of twelve
CI legs went red on a change every local check called clean, because the
prescribed local check was:

    ast.parse(src, feature_version=(3, 9))

`feature_version` gates *grammar productions* (walrus, `match`, `except*`). It
does not touch the tokenizer change PEP 701 made, so on a modern host that call
returns clean before and after the fix. `test_feature_version_is_blind_to_pep701`
pins that gap directly so nobody re-derives the false clean and concludes it is
fine.

The guard therefore compiles under an interpreter that genuinely IS the floor,
and when it cannot find one it returns a `skipped` — the #515 shape — rather
than a pass. `test_ci_matrix_covers_the_syntax_floor` is what stops that skip
from becoming universal: it fails if the matrix ever loses the floor leg, which
is the only place the guard is guaranteed to actually run.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import supertool  # noqa: E402

# Nested same-type quotes in a replacement field. A *string*, never source of
# this file: on 3.9 this module itself must still parse.
PEP701 = 'f"got: {d["k"]!r}"\n'
CLEAN = 'x = 1\n'


def test_feature_version_is_blind_to_pep701() -> None:
    """The trap, pinned. On 3.12+ the wrong check reports clean on the exact
    source that breaks nine CI legs."""
    if sys.version_info >= (3, 12):
        ast.parse(PEP701, feature_version=(3, 9))  # must NOT raise — that is the bug
    else:
        with pytest.raises(SyntaxError):
            ast.parse(PEP701, feature_version=(3, 9))


def _floor_or_skip() -> str:
    interp = supertool._syntax_floor_interpreter()
    if interp is None:
        floor = "{0}.{1}".format(*supertool.SYNTAX_FLOOR)
        pytest.skip(
            f"NO FLOOR INTERPRETER: nothing at Python {floor} to compile with. "
            f"Set ${supertool.SYNTAX_FLOOR_ENV} or install python{floor}. "
            "This check did NOT run — the floor CI leg is the guarantee it runs at all."
        )
    return interp


def test_the_guard_catches_what_feature_version_missed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(PEP701, encoding="utf-8")
    _floor_or_skip()
    result = supertool._syntax_floor_check([str(bad)])
    assert "skipped" not in result, result
    assert result["ok"] is False, "PEP 701 source passed the floor check"
    assert result["count"] == 1
    assert result["errors"][0]["code"] == "syntax"
    assert "bad.py" in result["errors"][0]["file"]


def test_the_guard_passes_clean_source(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(CLEAN, encoding="utf-8")
    _floor_or_skip()
    result = supertool._syntax_floor_check([str(good)])
    assert result.get("ok") is True, result
    assert result["count"] == 0


def test_missing_floor_interpreter_is_a_loud_skip_not_a_pass(monkeypatch, tmp_path: Path) -> None:
    """The absence-as-evidence failure this repo has filed twelve times. With
    no floor interpreter the check must decline, in the documented skip shape
    (#515), naming the escape hatch — never report ok."""
    bad = tmp_path / "bad.py"
    bad.write_text(PEP701, encoding="utf-8")
    monkeypatch.setattr(supertool, "_syntax_floor_interpreter", lambda *a, **k: None)
    result = supertool._syntax_floor_check([str(bad)])
    assert "ok" not in result, "a skip that carries ok reads as a pass"
    assert "count" not in result and "errors" not in result
    reason = result["skipped"]
    assert supertool.SYNTAX_FLOOR_ENV in reason, reason
    assert "3.9" in reason, reason


def test_a_mismatched_escape_hatch_is_refused_not_trusted(monkeypatch) -> None:
    """$PYTHON39 pointing at a 3.12 binary is worse than it being unset: it
    would silently restore the false clean. The resolver must reject it."""
    monkeypatch.setenv(supertool.SYNTAX_FLOOR_ENV, sys.executable)
    if sys.version_info[:2] <= supertool.SYNTAX_FLOOR:
        pytest.skip("running interpreter IS the floor — nothing to mismatch")
    assert supertool._syntax_floor_interpreter() is None


def test_the_running_interpreter_counts_when_it_is_the_floor(monkeypatch) -> None:
    """On the floor CI leg the guard must run for real, with no extra install."""
    monkeypatch.delenv(supertool.SYNTAX_FLOOR_ENV, raising=False)
    if sys.version_info[:2] > supertool.SYNTAX_FLOOR:
        pytest.skip("this interpreter is above the floor")
    assert supertool._syntax_floor_interpreter() == sys.executable


def test_an_interpreter_above_the_floor_says_what_it_does_not_cover(tmp_path: Path) -> None:
    """A 3.11 lying around is worth running — it catches PEP 701 — but it is not
    the floor, and a check that overstates its own reach is how the next false
    clean gets built. The gap must be named in the result, not assumed."""
    interp = _floor_or_skip()
    ver = supertool._interpreter_version(interp)
    good = tmp_path / "good.py"
    good.write_text(CLEAN, encoding="utf-8")
    result = supertool._syntax_floor_check([str(good)])
    assert result["interpreter_version"] == "{0}.{1}".format(*ver)
    if ver > supertool.SYNTAX_FLOOR:
        assert "NOT covered" in result["partial"], result
        assert "{0}.{1}".format(*supertool.SYNTAX_FLOOR) in result["partial"]
    else:
        assert "partial" not in result


def test_a_lying_python39_on_path_is_not_trusted(monkeypatch, tmp_path: Path) -> None:
    """The name is not the version. A `python3.9` shim that is really the host
    interpreter would hand back exactly the false clean this guard exists to
    stop, so the binary is asked rather than believed."""
    if sys.version_info[:2] <= supertool.SYNTAX_FLOOR:
        pytest.skip("running interpreter IS the floor")
    fake = tmp_path / "python3.9"
    fake.write_text("#!/bin/sh\nexec '" + sys.executable + "' \"$@\"\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.delenv(supertool.SYNTAX_FLOOR_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert supertool._syntax_floor_interpreter() is None


@pytest.mark.slow
def test_every_repo_py_file_compiles_at_the_floor() -> None:
    _floor_or_skip()
    files = [str(p) for p in _ROOT.rglob("*.py")
             if ".git" not in p.parts and "node_modules" not in p.parts]
    assert files
    result = supertool._syntax_floor_check(files)
    assert result.get("ok") is True, result.get("errors")


def test_ci_matrix_covers_the_syntax_floor() -> None:
    """Without a floor leg the guard skips everywhere and the check renders as
    a pass — the exact failure it exists to prevent."""
    floor = "{0}.{1}".format(*supertool.SYNTAX_FLOOR)
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'requires-python\s*=\s*">=\s*([0-9.]+)"', pyproject)
    assert declared and declared.group(1) == floor, (
        f"pyproject requires-python does not match SYNTAX_FLOOR {floor}"
    )
    workflow = (_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    matrix = re.search(r"python-version:\s*\[([^\]]*)\]", workflow)
    assert matrix, "no python-version matrix in tests.yml"
    versions = [v.strip().strip('"').strip("'") for v in matrix.group(1).split(",")]
    assert floor in versions, (
        f"CI matrix {versions} has no leg at the supported floor {floor} — "
        "the syntax guard would skip on every leg and render as a pass"
    )
