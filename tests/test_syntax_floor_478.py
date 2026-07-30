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

#577 — the repo-wide walk below carried #575's defect with an even narrower
exclusion set: `.git` and `node_modules`, and nothing else. A stale
`build/lib/supertool.py` was compiled at the floor, so a copy predating a
syntax fix reported a failure in code already fixed; and an in-repo `.venv`,
which is named by no ignore file in this repo, fed thousands of third-party
files into a 3.9 compile, many of which legitimately target newer syntax. The
walk now lives in `tests/_repo_walk.py`, shared with
`test_no_bare_python3_spawn.py` — which asks the identical question and had
already been fixed two days earlier, the drift #555 is about, caught in the act.

The `slow` marker went with it. The marker's documented contract is ">5s"
(pyproject); this compiles 391 files in one subprocess in 0.6s and never met
it. What the marker actually bought was invisibility: excluded from every
default local run and from the pre-push hook, so the only people who ever ran
it deliberately were maintainers on working machines — the population that has
a `build/` and an in-repo venv, i.e. precisely the population for whom it was
broken. A floor guard that cannot fail before a push is a floor guard that
reports the breakage only after nine CI legs already have (#478 itself).
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _repo_walk import git_ignored_dirs as _git_ignored_dirs
from _repo_walk import is_machine_state as _is_machine_state
from _repo_walk import repo_python_files
from _repo_walk import scanned_with as _scanned_with

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


def test_every_repo_py_file_compiles_at_the_floor() -> None:
    """Deliberately not `slow`-marked. 391 files, one subprocess, 0.6s — the
    marker's documented contract is ">5s" (pyproject) and this never met it.
    See the module docstring for why the marker was the more harmful half of
    #577."""
    _floor_or_skip()
    result = supertool._syntax_floor_check(
        [str(path) for path in repo_python_files()])
    assert result.get("ok") is True, result.get("errors")


def _sandbox_repo(root: Path) -> Path | None:
    """A throwaway git repo carrying both shapes #577 is about, plus one
    ordinary untracked source file. Returns that source file, or None when git
    is unavailable.

    `build/` is gitignored — #575's shape, exempt through git's own answer.
    The two virtualenvs and `node_modules/` are ignored by nothing, which is
    the entire point: git cannot exempt what no ignore file names, so only a
    name rule can. Every artifact holds floor-illegal syntax, so a walk that
    reaches any of them fails loudly rather than subtly.

    Staged in a real throwaway repo rather than in this one, so real
    `git ls-files` runs against a real `.gitignore` and no worker of the
    `-n auto` suite ever sees a stray .py appear under its feet (#576).
    """
    try:
        init = subprocess.run(["git", "init", "-q", str(root)],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if init.returncode != 0:
        return None
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    for rel in ("build/lib/_stale_copy.py",
                ".venv/lib/python3.12/site-packages/newpkg.py",
                "venv/lib/python3.12/site-packages/newpkg.py",
                "node_modules/vendored/tool.py"):
        artifact = root / rel
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(PEP701, encoding="utf-8")
    source = root / "still_being_written.py"
    source.write_text(PEP701, encoding="utf-8")
    return source


def test_machine_state_is_not_compiled_but_untracked_source_still_is(
        tmp_path: Path) -> None:
    """#577, pinned from both sides in one test on purpose.

    A stale `build/lib/supertool.py` left by `pip install .` gets compiled at
    the floor, so a copy predating a syntax fix reports a floor failure in
    code that was fixed hours ago — #575 exactly. An in-repo `.venv` is worse:
    untracked, named by no ignore file here, and thousands of third-party
    files deep, many of which legitimately do not compile at 3.9. Both reds
    are invisible on CI and unfixable by fixing anything, which is how a
    guard stops being read.

    The second half is why this is one test and not two. "Skips build
    artifacts and virtualenvs" is trivially satisfied by a walk that compiles
    nothing at all — the defect #559 and #564 are about — so the same
    floor-illegal source sits in an ordinary *untracked* file beside the
    artifacts and must still be compiled, and still be reported. Untracked is
    deliberate: it is the state of a file being written right now, which is
    when this guard is most worth having, and it is the case that rules out
    narrowing the walk to `git ls-files`.
    """
    source = _sandbox_repo(tmp_path)
    if source is None:
        pytest.skip("git unavailable; the name floor is pinned separately")
    _floor_or_skip()

    ignored = _git_ignored_dirs(tmp_path)
    assert ignored is not None and "build/" in ignored, (
        f"git did not report build/ as ignored: {ignored}")
    assert not any(entry.lstrip(".").startswith("venv") for entry in ignored), (
        "this fixture is only meaningful while the virtualenvs are UNignored, "
        "which is #577's point: no ignore file in this repo names them, so "
        f"git's answer cannot exempt them and a name rule must. Got {ignored}")

    scanned = _scanned_with(ignored, tmp_path)
    assert {p.relative_to(tmp_path).as_posix() for p in scanned} == {
        "still_being_written.py"}, (
        "the walk either compiled machine state or stopped compiling real "
        f"source: {sorted(p.relative_to(tmp_path).as_posix() for p in scanned)}")

    result = supertool._syntax_floor_check([str(path) for path in scanned])
    assert result.get("ok") is False, (
        "floor-illegal syntax in an ordinary untracked file went unreported — "
        f"excluding machine state must not be paid for by seeing less: {result}")
    assert result["count"] == 1, result["errors"]
    assert "still_being_written.py" in result["errors"][0]["file"]


def test_an_in_repo_virtualenv_is_excluded_by_name_and_not_by_git() -> None:
    """The rule, directly. `.venv` falls out of the dot-name floor; bare
    `venv` does not and has to be named — `python -m venv venv` is at least as
    common a spelling, and neither appears in this repo's `.gitignore`. The
    empty ignore set stands in for "git had nothing to say about these", which
    is the true state of an in-repo virtualenv here."""
    none_ignored: frozenset[str] = frozenset()
    assert _is_machine_state(".venv/lib/python3.12/site-packages/x.py", none_ignored)
    assert _is_machine_state("venv/lib/python3.12/site-packages/x.py", none_ignored)
    assert _is_machine_state("node_modules/pkg/x.py", none_ignored)
    assert not _is_machine_state("supertool.py", none_ignored)
    assert not _is_machine_state("tests/fixtures/mock_mcp_server.py", none_ignored)


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
