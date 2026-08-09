"""#931: the entry point must be a shim so the bulk is cached to bytecode.

CPython writes and reuses `__pycache__/*.pyc` for *imported* modules. It never
caches the script named on the command line — `__main__` is compiled from source
on every run. `supertool.py` is ~17.4k lines, so as long as it *is* the script,
every single invocation re-parses and re-compiles all of it: measured at ~145ms
on ubuntu and windows runners and ~100ms on macOS, paid by every user call and
by each of the ~900 spawns in a CI leg.

The fix is to make `supertool.py` a thin shim over an importable module. What
that buys is only real if the bytecode cache is actually written and actually
reused, so that is what these tests assert — not the shape of the diff.

Two of the tests below (`coverage_gate`, `pyproject`) pass before the split as
well as after. They are here because the split can quietly break each of them in
a way nothing else notices: a coverage floor that used to bound 17k lines now
bounding an 80-line shim still prints a green percentage, and a top-level module
missing from `py-modules` ships a package that only the non-editable install
test — which is `slow`, and out of the default run — ever executes.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _symlink import require_symlink

import pytest

import supertool

REPO_ROOT = Path(__file__).parent.parent

#: A `.pyc` at least this big proves it is the *bulk* that got cached, not some
#: incidental helper. The source is ~780KB; an 80-line shim compiles to ~2KB.
BULK_PYC_MIN_BYTES = 200_000


def _module_files() -> list[Path]:
    """Every top-level module of the tool — the shim plus whatever it imports."""
    return sorted(REPO_ROOT.glob("*.py"))


def _install(dest: Path) -> None:
    for src in _module_files():
        shutil.copy2(src, dest / src.name)


def _run_version(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "supertool.py", "version"],
        cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace",
    )


def _bulk_pycs(root: Path) -> list[Path]:
    cache = root / "__pycache__"
    if not cache.is_dir():
        return []
    return [p for p in cache.glob("*.pyc") if p.stat().st_size >= BULK_PYC_MIN_BYTES]


def test_script_invocation_caches_the_bulk_to_bytecode(tmp_path: Path) -> None:
    """`python supertool.py version` must leave a compiled copy of the bulk behind."""
    _install(tmp_path)

    proc = _run_version(tmp_path)
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"

    cached = _bulk_pycs(tmp_path)
    assert cached, (
        "no __pycache__ entry >= {0} bytes after running `supertool.py version`.\n"
        "The bulk of the tool is being compiled from source on every invocation "
        "because it is the `__main__` script, which CPython never caches.\n"
        "__pycache__ contents: {1}".format(
            BULK_PYC_MIN_BYTES,
            sorted(p.name for p in (tmp_path / "__pycache__").glob("*")) or "(absent)",
        )
    )


def test_the_cached_bytecode_is_reused_on_the_next_invocation(tmp_path: Path) -> None:
    """Written once is not enough — the second run must not recompile."""
    _install(tmp_path)

    assert _run_version(tmp_path).returncode == 0
    first = _bulk_pycs(tmp_path)
    assert first, "nothing cached on the first run — see the test above"
    before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in first}

    assert _run_version(tmp_path).returncode == 0
    after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in _bulk_pycs(tmp_path)}

    assert after == before, (
        "the bytecode cache was rewritten by the second invocation, so it is not "
        "being reused:\nbefore: {0}\nafter:  {1}".format(before, after)
    )


def test_entry_point_survives_a_symlink_from_an_unrelated_cwd(tmp_path: Path) -> None:
    """`~/.local/bin/supertool` and `dvsi/supertool` are symlinks to supertool.py.

    A shim that imports a sibling module has to find that sibling through the
    link, from a working directory that has nothing to do with the install.
    """
    install = tmp_path / "install"
    install.mkdir()
    _install(install)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    link = bindir / "supertool"
    require_symlink()
    link.symlink_to(install / "supertool.py")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = subprocess.run(
        [sys.executable, str(link), "version"],
        cwd=str(elsewhere), capture_output=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert supertool.VERSION in proc.stdout, proc.stdout


def test_module_route_still_works(tmp_path: Path) -> None:
    """`python -m supertool` is a supported route and must survive the split."""
    _install(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "supertool", "version"],
        cwd=str(tmp_path), capture_output=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert supertool.VERSION in proc.stdout, proc.stdout


#: A stand-in for "some other `_supertool` is importable in this environment".
#: On CI that is not hypothetical: `pip install -e .[dev]` puts an
#: `_EditableFinder` on `sys.meta_path` that maps the name straight back to the
#: repo checkout, so *every* process in that venv can import `_supertool` from
#: any cwd. A non-editable install does the same via site-packages.
_DECOY = """\
VERSION = '0.0.0-decoy'


def main(argv):
    print('DECOY')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv[1:]))
"""


def _decoy_on_path(tmp_path: Path) -> dict:
    """An env whose `sys.path` offers an `_supertool` from a foreign tree."""
    elsewhere = tmp_path / "foreign"
    elsewhere.mkdir()
    (elsewhere / "_supertool.py").write_text(_DECOY, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(elsewhere) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_an_incomplete_install_refuses_instead_of_tracebacking(tmp_path: Path) -> None:
    """Copying `supertool.py` alone used to be a complete install. It is not now.

    The refusal has to be decided by what is *on disk beside the shim*, not by
    what `import _supertool` happens to find. Wherever the tool is installed as
    a package — CI's editable install, any `pip install claude-supertool` — the
    name resolves fine from a directory that contains none of it, and a shim
    that just tries the import runs a completely different tree while printing
    a perfectly convincing version banner.
    """
    shutil.copy2(REPO_ROOT / "supertool.py", tmp_path / "supertool.py")

    proc = subprocess.run(
        [sys.executable, "supertool.py", "version"],
        cwd=str(tmp_path), env=_decoy_on_path(tmp_path),
        capture_output=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode != 0, (
        "a supertool.py with no _supertool.py beside it exited 0 having run "
        f"nothing. stdout: {proc.stdout!r}"
    )
    assert "DECOY" not in proc.stdout, (
        "the shim ran an `_supertool` from another tree instead of refusing: "
        f"{proc.stdout!r}"
    )
    assert "_supertool.py" in proc.stderr and "incomplete install" in proc.stderr, (
        f"the refusal does not name the missing file: {proc.stderr!r}"
    )
    assert "Traceback" not in proc.stderr, proc.stderr


def test_the_sibling_wins_over_an_importable_supertool_from_another_tree(
    tmp_path: Path,
) -> None:
    """A complete install runs its own `_supertool.py`, whatever else is on the path."""
    install = tmp_path / "install"
    install.mkdir()
    _install(install)

    proc = subprocess.run(
        [sys.executable, "supertool.py", "version"],
        cwd=str(install), env=_decoy_on_path(tmp_path),
        capture_output=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert "DECOY" not in proc.stdout, proc.stdout
    assert supertool.VERSION in proc.stdout, proc.stdout
    assert "mixed supertool trees" not in proc.stderr, (
        f"the sibling was loaded, so nothing is mixed: {proc.stderr!r}"
    )


def test_running_the_implementation_module_directly_still_runs_it(tmp_path: Path) -> None:
    """Anything spawning `supertool.__file__` now lands on `_supertool.py`.

    Without a `__main__` block there it would print nothing and exit 0 — a
    success receipt for an op that never executed.
    """
    _install(tmp_path)
    proc = subprocess.run(
        [sys.executable, "_supertool.py", "version"],
        cwd=str(tmp_path), capture_output=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert supertool.VERSION in proc.stdout, repr(proc.stdout)


def test_the_mixed_tree_guard_does_not_fire_on_its_own_checkout() -> None:
    """The guard compared the invoked file against the project's `supertool.py`.

    After the split those can never be the same file, so the identity test would
    have reported a mixed tree on every single invocation made from inside a
    supertool checkout — a warning that is always on is a warning nobody reads.
    """
    proc = subprocess.run(
        [sys.executable, "supertool.py", "version"],
        cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    assert "mixed supertool trees" not in proc.stderr, (
        "the mixed-tree guard fired on the repo's own checkout invoking its own "
        f"entry point: {proc.stderr!r}"
    )


def _bulk_module_relpath() -> str:
    """The repo-relative file that actually defines `main` — shim or not."""
    return os.path.relpath(inspect.getsourcefile(supertool.main), REPO_ROOT).replace(os.sep, "/")


def test_the_coverage_floor_still_bounds_the_file_that_holds_the_code() -> None:
    """A floor over a 20-line shim is a green number measuring nothing."""
    spec = importlib.util.spec_from_file_location(
        "_coverage_gate_931", REPO_ROOT / ".github" / "scripts" / "coverage_gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    bulk = _bulk_module_relpath()
    enforced = list(gate.ENFORCED)
    assert any(bulk == prefix or bulk.startswith(prefix) for prefix in enforced), (
        f"`main` lives in {bulk!r}, which no enforced coverage prefix covers: "
        f"{enforced}. The floor would pass while measuring nothing."
    )

    names = set(gate._source_lines())
    assert bulk in names or bulk[:-3] in names, (
        f"coverage `source` {sorted(names)} names neither {bulk!r} nor its module "
        f"name, so the bulk would not be measured at all."
    )


def test_the_changelog_gate_still_sees_the_file_that_holds_the_code() -> None:
    """`changelog.yml` decides "user-visible" from a path regex.

    It named `supertool.py`, which after the split is a shim nobody edits —
    every future core change lands in `_supertool.py` instead and would have
    been waved through as docs-only. A gate that answers "nothing to announce"
    because it stopped looking is the failure mode, not a relaxed rule.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "changelog.yml").read_text(encoding="utf-8")
    opener, sep, rest = workflow.partition("grep -E " + chr(39))
    assert sep, "could not find the shipped-paths grep in changelog.yml"
    pattern, sep, _ = rest.partition(chr(39))
    assert sep and pattern.startswith("^("), (
        "the shipped-paths grep in changelog.yml no longer has the shape this "
        "test reads: " + repr(pattern)
    )

    bulk = _bulk_module_relpath()
    assert re.match(pattern, bulk), (
        f"changelog.yml treats {bulk!r} as not user-visible (regex {pattern!r}), "
        f"so a core change would be exempt from needing a fragment."
    )


def test_pyproject_declares_every_top_level_module() -> None:
    """A second module that `py-modules` does not list ships a broken package."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py3.9/3.10
        pytest.skip("tomllib requires Python 3.11+")

    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        cfg = tomllib.load(fh)
    declared = set(cfg["tool"]["setuptools"]["py-modules"])
    present = {p.stem for p in _module_files()}

    assert present <= declared, (
        f"top-level module(s) {sorted(present - declared)} exist in the repo but "
        f"are not in [tool.setuptools] py-modules {sorted(declared)} — "
        f"`pip install .` would ship a package that cannot import itself."
    )
