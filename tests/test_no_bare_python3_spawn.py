"""Guard against #529/#564: the literal name "python3" must not be the
interpreter anything in this repo runs, or is handed to something to run.

On Windows, PATH resolution of the literal `"python3"` can hit the App
Execution Alias stub, which *blocks* instead of erroring — a child process
that never starts, dressed up as a `subprocess.TimeoutExpired` on an
otherwise-healthy adapter (see PR #527's Windows/3.10 flake on
`test_validators.py::test_phplint_adapter_valid_php`). `sys.executable`
guarantees the child interpreter is the one actually running the parent,
removing that bet entirely. On POSIX the same bet loses more quietly: a
supertool running out of a virtualenv spawns whatever `python3` PATH happens
to name that day, so the child gets a different set of installed packages
than the process that started it.

#564 — this scan walked `tests/` and nothing else, which is the one
directory #529 had already converted. `supertool.py`, `presets/`,
`validators/`, `hooks/`, `formatters/` and `notifiers/` — the shipping code,
where a user meets the defect — were never looked at. That is precisely how
#559 survived: `_vim_render_lint` spawned `["python3", "-m", "py_compile",
path]` for eight months after #529 landed, in this repo, with this guard
green the whole time. A check that cannot see the interesting half produces
a signal indistinguishable from clean — the house defect, applied to the
house's own guard. The walk now starts at the repository root.

#575 — that widened walk then read machine state as source. A `build/`
left by `pip install .` holds a snapshot of `supertool.py` from whenever it
was last built, so the guard reported a violation in a file that is not
source, is not shipped, and had been fixed hours earlier — invisible on CI,
red on every machine that has ever built. Scope is now asked of git rather
than spelled out as names; `scanned_files` below carries that reasoning and
why the two obvious alternatives lost.

## What counts as a violation

The literal is a violation where it *is* the interpreter: where the value
that gets executed, or that is bound for something else to execute, is that
string and nothing else. Four positions, and the widening needed all four —
pointing the old call-site rule at the repo root finds nothing, because the
live instance is a default argument.

- The interpreter element of a `subprocess.run` / `Popen` / `check_output` /
  `check_call` argv (or the whole command, for the `shell=True` string form).
- The program path of an `os.exec*` call. `presets/watch/dispatcher.py`
  spawns its pollers with `os.execve`, so a spawn guard blind to that family
  is blind to the only real spawn mechanism in `presets/watch`.
- The value bound to a name: assignment, annotated assignment, parameter
  default, or call keyword. `presets/mcp/_spawn.py`'s `python: str =
  "python3"` is reached by four validator adapters that pass no `python=` at
  all, so it was not a trap armed for a future caller — it was the
  interpreter every warm daemon in the repo already launched. No rule about
  call sites can see it.
- The argument to `shutil.which`. The PATH lookup of that exact name *is*
  the bet — #559's own gate was `shutil.which("python3")` — and its result
  reaching a spawn arrives there as a `Call` node, slipping past every rule
  above.

## What does not count, and why there is no exemption list

Two shapes mention the literal legitimately. Both are excluded by the rule
rather than by name. An exemption list — file:line, or a `# noqa`-style
marker — is where the next real instance hides, and this repo has now been
burned twice by a scope that quietly excluded the thing it should have
caught: #559 here, and #555, where the same argument was put to a `sys.path`
source scan and an exclusion list was deliberately refused in favour of
describing the pattern in prose.

**Fixture data.** Several files (`test_watch_pid_set_511.py`,
`test_watch_death_supervision_513.py`) contain `"python3"` as the first
element of a list describing a fake process-table row that the code under
test parses. That data is the thing being tested, not a spawn. It is not
flagged because it is not in any of the four positions — which is the whole
reason this is an AST scan and not a grep, and also why a repo-wide walk
does not drown in `#!/usr/bin/env python3` shebangs.

**A fallback behind an already-resolved interpreter.** `sys.executable or
"python3"` (`presets/watch/transport.py`) is not the literal; it is an
expression that resolves the running interpreter and mentions the name only
on the branch where that resolution came back empty. Tolerated — but
narrowly: the tolerance requires *every* operand before the fallback to be
`sys.executable` itself. `shutil.which(...) or "python3"` and `cfg.python or
"python3"` are the PATH bet with extra steps and remain violations. That is
a statement about a shape, which the next instance has to satisfy to hide,
not a statement about a line, which it only has to be adjacent to.

This file names the literal plainly and is scanned like every other file,
with no self-exemption: its own mentions sit in `==` comparisons, which are
not one of the four positions — the rule is narrow enough that the file
checking it needs no hole cut for itself. That is load-bearing rather than
tidy: a guard that has to be left out of its own scan is the bug it is
checking for. (It cost one line to keep. Binding the name to a constant —
`BARE_NAME = "python3"` — was a violation of the very rule below, correctly
flagged on the first run, so the literal is written out at each comparison
instead.)

The call must be resolved through whatever name it is actually reachable
under in that file, not just the literal spelling `subprocess.run`. The
suite has an established idiom of `import subprocess as sp` (or `_sp`,
`_sub`) — eight sites across six files as of this writing
(test_op_workspace.py, test_security_mcp_daemon.py, test_map.py x4,
test_git_conflicts.py, test_read.py) — and a guard that only recognizes the
unaliased spelling is blind to a spawn written the way this suite already
writes them routinely. That is the same defect #529 is about, one level in.
So this module resolves, per file, both:

- `import subprocess [as X]` -> `X.run(...)` / `X.Popen(...)` / etc.
- `from subprocess import run [as Y]` -> bare `Y(...)` calls

before deciding whether a call site is a subprocess spawn, and does the same
for `os.exec*` and `shutil.which`.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path
from typing import Iterator, NamedTuple

import pytest

from _repo_walk import REPO_ROOT
from _repo_walk import git_ignored_dirs as _git_ignored_dirs
from _repo_walk import is_machine_state as _is_machine_state
from _repo_walk import scanned_with as _scanned_with

_SUBPROCESS_SPAWN_ATTRS = {"run", "Popen", "check_output", "check_call"}
_OS_EXEC_ATTRS = {"execv", "execve", "execvp", "execvpe",
                  "execl", "execle", "execlp", "execlpe"}
_WHICH_ATTRS = {"which"}


class _Bindings(NamedTuple):
    """Every name in one file that reaches a spawn or a PATH lookup.

    Split module-vs-direct because the two call shapes differ: `X.run(...)`
    needs X to be the module, `Y(...)` needs Y to be the function itself.
    """
    subprocess_modules: set[str]
    subprocess_funcs: set[str]
    os_modules: set[str]
    os_exec_funcs: set[str]
    shutil_modules: set[str]
    which_funcs: set[str]


_MODULE_ATTRS = {
    "subprocess": _SUBPROCESS_SPAWN_ATTRS,
    "os": _OS_EXEC_ATTRS,
    "shutil": _WHICH_ATTRS,
}


def _collect_bindings(tree: ast.AST) -> _Bindings:
    """Resolve, for this file, the names `subprocess` / `os` / `shutil` and
    their spawn functions are actually reachable under — plain, aliased, or
    imported directly.
    """
    modules: dict[str, set[str]] = {m: set() for m in _MODULE_ATTRS}
    funcs: dict[str, set[str]] = {m: set() for m in _MODULE_ATTRS}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules:
                    modules[alias.name].add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            wanted = _MODULE_ATTRS.get(node.module or "")
            if wanted is None:
                continue
            for alias in node.names:
                if alias.name in wanted:
                    funcs[node.module or ""].add(alias.asname or alias.name)

    return _Bindings(
        subprocess_modules=modules["subprocess"],
        subprocess_funcs=funcs["subprocess"],
        os_modules=modules["os"],
        os_exec_funcs=funcs["os"],
        shutil_modules=modules["shutil"],
        which_funcs=funcs["shutil"],
    )


def _is_sys_executable(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "executable"
            and isinstance(node.value, ast.Name) and node.value.id == "sys")


def _names_bare_python3(node: ast.expr) -> bool:
    """True when this expression *is* the bare name, or degrades to it from
    something that is not already a resolved interpreter.

    `sys.executable or "python3"` is not a violation: the name is reached
    only where the resolution it prefers came back empty. Every other `or`
    chain ending in the literal is — `shutil.which(...)` and a config value
    are both PATH bets, and putting one in front of the literal launders the
    bet rather than removing it.
    """
    if isinstance(node, ast.Constant) and node.value == "python3":
        return True
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        *preferred, fallback = node.values
        if not (isinstance(fallback, ast.Constant) and fallback.value == "python3"):
            return False
        return not all(_is_sys_executable(v) for v in preferred)
    return False


def _resolves_to(func: ast.expr, module_names: set[str], attrs: set[str],
                 direct_names: set[str]) -> bool:
    return ((isinstance(func, ast.Attribute) and func.attr in attrs
             and isinstance(func.value, ast.Name)
             and func.value.id in module_names)
            or (isinstance(func, ast.Name) and func.id in direct_names))


def _call_violations(node: ast.Call, b: _Bindings) -> Iterator[tuple[int, str]]:
    func = node.func

    if _resolves_to(func, b.subprocess_modules, _SUBPROCESS_SPAWN_ATTRS,
                    b.subprocess_funcs) and node.args:
        first = node.args[0]
        if isinstance(first, (ast.List, ast.Tuple)):
            if first.elts and _names_bare_python3(first.elts[0]):
                yield first.elts[0].lineno, "subprocess argv[0]"
        elif _names_bare_python3(first):
            yield first.lineno, "subprocess command string"

    if _resolves_to(func, b.os_modules, _OS_EXEC_ATTRS,
                    b.os_exec_funcs) and node.args:
        if _names_bare_python3(node.args[0]):
            yield node.args[0].lineno, "os.exec* program path"

    if _resolves_to(func, b.shutil_modules, _WHICH_ATTRS,
                    b.which_funcs) and node.args:
        if _names_bare_python3(node.args[0]):
            yield node.args[0].lineno, "shutil.which PATH lookup"

    for kw in node.keywords:
        if kw.arg is not None and _names_bare_python3(kw.value):
            yield kw.value.lineno, f"keyword argument {kw.arg}="


def _iter_violations(source: str, filename: str) -> Iterator[tuple[int, str]]:
    """Yield (lineno, why) for every position in this file where the bare
    name is, or becomes, an interpreter."""
    tree = ast.parse(source, filename=filename)
    bindings = _collect_bindings(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield from _call_violations(node, bindings)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is not None and _names_bare_python3(node.value):
                yield node.lineno, "bound to a name"
        elif isinstance(node, ast.arguments):
            defaults = [*node.defaults, *[d for d in node.kw_defaults if d]]
            for default in defaults:
                if _names_bare_python3(default):
                    yield default.lineno, "parameter default"


def scanned_files() -> list[Path]:
    """Every Python source file in the repository.

    rglob from the root, because #564 is what scoping this to one directory
    cost. `tests/fixtures/` is *not* excluded — it holds real .py files
    (mock_mcp_server.py, fixtures/resolve/mypkg/utils.py) that are not pytest
    modules but are Python that could spawn, and nothing about sitting in a
    fixture directory exempts a file from the same PATH-resolution bet. That
    is this guard's reasoning about its own scope and it stays here.

    What counts as repository source rather than machine state is not: it is
    the same question `test_syntax_floor_478.py` asks to decide what to
    compile at the floor, and it now has one implementation, in
    `tests/_repo_walk.py`. #577 is why — the floor walk was found carrying
    this file's pre-#576 defect with an even narrower exclusion set, two days
    after #575 was filed, which is #555's six-drifting-`_load`-helpers
    happening in real time. That module's docstring carries the reasoning:
    names first (`.git`, `.venv`, `venv`, `node_modules` — spellings git
    cannot be relied on to report), then git's own ignored set (#449, #575),
    and never the *tracked* set, which would exempt the file being written
    right now.
    """
    return _scanned_with(_git_ignored_dirs())


def _find_offenders() -> list[str]:
    offenders = []
    for path in scanned_files():
        source = path.read_text(encoding="utf-8")
        for lineno, why in _iter_violations(source, str(path)):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} ({why})")
    return offenders


def _linenos(source: str) -> list[int]:
    return [lineno for lineno, _ in _iter_violations(source, "snippet.py")]


def test_no_python3_literal_is_ever_the_interpreter():
    """Nothing in this repository — shipping code included — may run, or hand
    on, the literal "python3". That string is a PATH-resolution bet that
    blocks on Windows via the App Execution Alias (#529) and silently picks a
    foreign set of packages inside a virtualenv."""
    offenders = _find_offenders()
    assert not offenders, (
        "literal \"python3\" used as an interpreter instead of sys.executable "
        "(#529/#564 — blocks on Windows via App Execution Alias PATH "
        "resolution):\n" + "\n".join(offenders)
    )


def test_the_scan_reaches_shipping_code_and_not_only_tests():
    """#564 itself, pinned. The guard was scoped to `tests/` — the one
    directory #529 had already cleaned — so `_vim_render_lint` spawned the
    banned name for eight months underneath a green check. Re-narrowing the
    walk has to fail here rather than quietly reporting clean, which is the
    failure mode that made #559 possible."""
    # `.as_posix()`, not `str()`: on Windows `relative_to` renders
    # `presets\mcp\_spawn.py`, which never equals the forward-slash literals
    # below, so this guard failed on all four Windows legs while the scan it
    # checks was working perfectly. A scope test that reports a narrowed walk
    # when the walk is fine is the same false alarm, pointed the other way.
    scanned = {p.relative_to(REPO_ROOT).as_posix() for p in scanned_files()}
    # `.github/scripts/junit_summary.py` is #593: the walk excluded every
    # dot-prefixed component, so the one Python file this repo ships inside a
    # dot-directory — and runs on all twelve CI legs — was never scanned.
    for required in ("supertool.py",
                     "_supertool.py",
                     "presets/mcp/_spawn.py",
                     "presets/watch/transport.py",
                     "validators/common/refusal.py",
                     "tests/test_watch_pid_set_511.py",
                     ".github/scripts/junit_summary.py"):
        assert required in scanned, (
            f"{required} is not scanned — the walk has been narrowed back "
            "towards #564's scope")


def test_the_guard_scans_itself():
    """No self-exemption. The rule is narrow enough that this file can name
    the literal in a `==` comparison and still pass, so excluding it buys
    nothing and costs the one property #564 is about."""
    assert Path(__file__).resolve() in scanned_files()


def test_guard_detects_aliased_subprocess_import_spawn():
    """Self-test: the guard's own correctness was unpinned, which is how it
    acquired the aliased-import blind spot in the first place (it only
    recognized the literal spelling `subprocess.run`, missing the `import
    subprocess as sp` idiom this suite already uses 8 times across 6
    files)."""
    source = (
        "import subprocess as sp\n"
        "\n"
        "def f():\n"
        "    sp.run([\"python3\", \"script.py\"], capture_output=True)\n"
    )
    assert _linenos(source) == [4]


def test_guard_detects_direct_from_import_spawn():
    """Self-test: `from subprocess import run` binds `run` directly, with no
    `subprocess.` or module-alias prefix at the call site at all."""
    source = (
        "from subprocess import run as r\n"
        "\n"
        "def f():\n"
        "    r([\"python3\", \"script.py\"], capture_output=True)\n"
    )
    assert _linenos(source) == [4]


def test_guard_does_not_flag_fixture_argv_data():
    """Self-test: a list literal starting with "python3" that is NOT the
    first argument to a spawn (e.g. fixture data describing a fake
    process-table row, as in test_watch_pid_set_511.py) must not be flagged.
    This is what keeps the guard from needing a hardcoded exclusion list for
    those files."""
    source = (
        "import subprocess\n"
        "\n"
        "def f(machine):\n"
        "    machine.add_process(201, [\"python3\", \"/x/dispatcher.py\"])\n"
    )
    assert _linenos(source) == []


def test_guard_does_not_flag_a_shebang():
    """Self-test: 486 files in this repo open with `#!/usr/bin/env python3`.
    A repo-wide *grep* for the name is unusable for that reason alone; an AST
    walk never sees a comment."""
    source = "#!/usr/bin/env python3\nimport subprocess\n"
    assert _linenos(source) == []


def test_guard_detects_the_default_argument_form():
    """Self-test for the position the widening was actually about
    (`presets/mcp/_spawn.py`). A rule about call sites cannot see this: the
    literal is a default, and the four adapters that reach it pass no
    `python=` at all, so the spawn site itself reads `[python, ...]` and
    looks clean."""
    source = (
        "import subprocess\n"
        "\n"
        "def ensure(name, *, python: str = \"python3\"):\n"
        "    subprocess.Popen([python, name])\n"
    )
    assert _linenos(source) == [3]


def test_guard_detects_a_module_level_binding():
    source = "PY = \"python3\"\n"
    assert _linenos(source) == [1]


def test_guard_detects_a_keyword_argument():
    """Passing the name in is the same defect as defaulting to it."""
    source = (
        "import _spawn\n"
        "\n"
        "def f(cwd):\n"
        "    return _spawn.ensure_daemon(cwd, \"phpstan\", python=\"python3\")\n"
    )
    assert _linenos(source) == [4]


def test_guard_detects_os_exec_spawns():
    """`presets/watch/dispatcher.py` starts its pollers with `os.execve`, so
    the exec family is a real spawn mechanism here, not a hypothetical one."""
    source = (
        "import os\n"
        "\n"
        "def f(argv, env):\n"
        "    os.execve(\"python3\", argv, env)\n"
    )
    assert _linenos(source) == [4]


def test_guard_detects_the_path_lookup_itself():
    """#559's gate was `shutil.which("python3")`. Resolving the name and then
    spawning the result hands the spawn a `Call` node, which no rule about
    argv literals can read — so the lookup is the violation."""
    source = (
        "import shutil\n"
        "import subprocess\n"
        "\n"
        "def f():\n"
        "    subprocess.run([shutil.which(\"python3\"), \"-V\"])\n"
    )
    assert _linenos(source) == [5]


def test_guard_does_not_flag_a_versioned_interpreter_lookup():
    """`shutil.which("python3.13")` is a different question. The App
    Execution Alias stubs are `python` and `python3`; a versioned name either
    resolves to a real interpreter or to nothing, which is a normal
    skip-if-absent decision the repo makes in several places
    (`tests/test_yaml_check.py`, `supertool.py`'s syntax-floor probe)."""
    source = (
        "import shutil\n"
        "import sys\n"
        "\n"
        "PY = shutil.which(\"python3.13\") or sys.executable\n"
    )
    assert _linenos(source) == []


def test_guard_tolerates_a_fallback_behind_sys_executable():
    """`sys.executable or "python3"` (`presets/watch/transport.py`) is not
    the literal — it is a resolution that mentions the name only where the
    interpreter it prefers came back empty. Tolerating it is what lets the
    widened guard be adopted at all, and it is done by narrowing the rule,
    never by exempting the file."""
    source = (
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def f(script):\n"
        "    subprocess.run([sys.executable or \"python3\", script])\n"
    )
    assert _linenos(source) == []


def test_guard_still_flags_a_fallback_behind_anything_else():
    """The narrowing has to be a shape, not a hole. `which(...) or "python3"`
    reads like the tolerated form and is the banned bet twice over: the
    lookup can return the blocking stub, and the fallback is the stub's own
    name."""
    source = (
        "import shutil\n"
        "import subprocess\n"
        "\n"
        "def f(script):\n"
        "    subprocess.run([shutil.which(\"python\") or \"python3\", script])\n"
    )
    assert _linenos(source) == [5]


def test_guard_flags_a_config_value_fallback():
    """Same shape, no `shutil` in sight: an operator-supplied value that can
    be empty, degrading to the one name that must never be spawned."""
    source = (
        "import subprocess\n"
        "\n"
        "def f(cfg, script):\n"
        "    subprocess.run([cfg.python or \"python3\", script])\n"
    )
    assert _linenos(source) == [4]


_VIOLATING_SOURCE = """import shutil


def probe():
    return shutil.which("python3")
"""


def _sandbox_repo(root: Path) -> Path | None:
    """A throwaway git repo with a gitignored `build/`, the stale artifact
    inside it, and an ordinary *untracked* source file beside it. Returns
    None when git is unavailable, which is the fallback's territory."""
    try:
        init = subprocess.run(["git", "init", "-q", str(root)],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if init.returncode != 0:
        return None
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    artifact = root / "build" / "lib" / "_stale_copy.py"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(_VIOLATING_SOURCE, encoding="utf-8")
    (root / "still_being_written.py").write_text(_VIOLATING_SOURCE,
                                                 encoding="utf-8")
    return artifact


def test_a_build_artifact_is_not_scanned_but_untracked_source_still_is(tmp_path):
    """#575, pinned from both sides in one test on purpose.

    A stale `build/lib/supertool.py` left by `pip install .` reported a
    violation the real file had not contained for hours — a red the guard's
    own CI can never see, on a file that is not source, is not shipped, and
    cannot be fixed by fixing anything. The first person to hit it
    reasonably concludes the check is noisy, and the next real instance
    ships underneath a guard nobody reads.

    The second half is the half that matters more, and it is why this is one
    test and not two. "Ignores build artifacts" is trivially satisfied by a
    scan that walks nothing at all — the exact defect #564 and #559 are
    about — so the same literal sits in an ordinary, *untracked* file beside
    the artifact and must still be reported. Untracked is deliberate: it is
    the state of a file being written right now, which is when this guard is
    most worth having, and it is the case that rules out narrowing the walk
    to `git ls-files`.

    Staged in a real throwaway repo rather than in this one, so that real
    `git ls-files` runs against a real `.gitignore` and no worker of the
    `-n auto` suite ever sees a violating .py appear under its feet.
    """
    artifact = _sandbox_repo(tmp_path)
    if artifact is None:
        pytest.skip("git unavailable; the fallback is pinned separately")

    ignored = _git_ignored_dirs(tmp_path)
    assert ignored is not None and "build/" in ignored, (
        f"git did not report build/ as ignored: {ignored}")

    scanned = _scanned_with(ignored, tmp_path)
    assert artifact not in scanned, (
        "a gitignored build artifact is being scanned as repository source")

    probe = tmp_path / "still_being_written.py"
    assert probe in scanned, (
        "an untracked source file is not scanned — the walk has been "
        "narrowed to tracked files, which exempts every file still being "
        "written, and this guard exists for exactly those files")
    assert _linenos(probe.read_text(encoding="utf-8")) == [5], (
        "the literal in ordinary source went unreported — excluding build "
        "artifacts must not be paid for by seeing less real code")


def test_the_exclusion_is_a_property_and_not_a_list_of_names():
    """What #575 actually bought. `build/` is exempt because git says it is
    ignored, not because anyone wrote "build" down — so `dist/`, an
    `.egg-info/`, `htmlcov/` and whatever the next packaging tool emits are
    all covered before they are filed as bugs. Ordinary source under a
    similarly-named path is untouched."""
    ignored = frozenset({"build/", "vendor/dist/"})
    assert _is_machine_state("build/lib/supertool.py", ignored)
    assert _is_machine_state("vendor/dist/pkg/mod.py", ignored)
    assert not _is_machine_state("presets/mcp/_spawn.py", ignored)
    assert not _is_machine_state("tests/fixtures/mock_mcp_server.py", ignored)


def test_the_no_git_fallback_is_wider_than_it_needs_to_be_never_narrower():
    """The degraded path, where git could not answer. It is a denylist,
    which #564 and #555 are both against — tolerable only here, because it
    cannot cause the failure those issues are about. Its worst outcome is a
    false alarm on an unnamed artifact directory, which someone reads and
    files; a tracked-files scan's worst outcome is silence, which nobody
    reads at all."""
    assert _is_machine_state("build/lib/supertool.py", None)
    assert _is_machine_state("dist/supertool/mod.py", None)
    assert _is_machine_state("supertool.egg-info/x.py", None)
    assert not _is_machine_state("presets/mcp/_spawn.py", None)
    assert not _is_machine_state("supertool.py", None)


def test_a_pathological_ignore_answer_fails_loudly_instead_of_reporting_clean():
    """The one way this guard could inherit the defect it checks for: an
    exclusion rule broad enough to prune everything would report zero
    offenders and read exactly like a clean repository. An empty scan is a
    hard error, never a pass — this is the whole reason `git ls-files`
    (tracked files only) was not the shape chosen, and it is pinned here so
    that no future widening of the exclusions can reintroduce it quietly."""
    with pytest.raises(AssertionError, match="scanning nothing"):
        _scanned_with(frozenset({""}))


#: Dot-prefixed paths of both kinds, each holding the banned literal so that a
#: walk which reaches the wrong one fails loudly rather than subtly. `.githooks`
#: and `.github` are version-controlled and shipped; `.venv`, `.pytest_cache`
#: and `.git` are machine state. The distinction is not the leading dot.
_DOT_SANDBOX = {
    ".githooks/lint.py": _VIOLATING_SOURCE,
    ".github/scripts/report.py": _VIOLATING_SOURCE,
    ".venv/lib/python3.12/site-packages/newpkg.py": _VIOLATING_SOURCE,
    ".pytest_cache/v/stale.py": _VIOLATING_SOURCE,
    ".git/hooks/planted.py": _VIOLATING_SOURCE,
    #: Ordinary source, so a walk that prunes everything fails as a readable
    #: set difference rather than as `scanned_with`'s empty-scan assertion.
    "ordinary.py": "x = 1\n",
}


def _dot_dir_sandbox(root: Path) -> bool:
    """A throwaway git repo carrying both kinds of dot-directory, with no
    `.gitignore` at all — so git's ignored answer is empty and the name rule is
    the only thing that can exempt anything. False when git is unavailable.
    """
    try:
        init = subprocess.run(["git", "init", "-q", str(root)],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    if init.returncode != 0:
        return False
    for rel, body in _DOT_SANDBOX.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return True


def test_a_dot_directory_this_repo_ships_is_scanned_and_dot_machine_state_is_not(
        tmp_path):
    """#593, pinned from both sides in one test on purpose.

    `is_machine_state` excluded every dot-prefixed *component*, so `.githooks/`
    and `.github/` — version-controlled, shipped, and asked of every
    contributor — were invisible to both guards built on this walk. A scanner
    whose exclusion list quietly contains a directory it ships is
    indistinguishable, from the outside, from a scanner that found nothing.

    The second half is why this is one test and not two. "Scans dot-directories
    now" is trivially satisfied by dropping the rule, which is what #577 is
    about: an in-repo `.venv` is thousands of third-party files deep, named by
    no ignore file here, and legitimately un-compilable at the 3.9 floor. So
    the same literal sits in `.venv/`, `.pytest_cache/` and `.git/` and must
    still be unseen — the predicate was too broad, not wrong.

    Staged in a real throwaway repo with no `.gitignore`, so git's ignored set
    is genuinely empty and only the name rule is under test, and so no worker of
    the `-n auto` suite ever sees a violating .py appear under its feet (#576).
    """
    if not _dot_dir_sandbox(tmp_path):
        pytest.skip("git unavailable; the name floor is pinned separately")

    ignored = _git_ignored_dirs(tmp_path)
    assert ignored is not None and not ignored, (
        "this fixture is only meaningful while git reports nothing ignored, "
        "which is what makes it a test of the name rule rather than of git's "
        f"answer. Got {sorted(ignored) if ignored else ignored}")

    scanned = _scanned_with(ignored, tmp_path)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in scanned) == [
        ".githooks/lint.py",
        ".github/scripts/report.py",
        "ordinary.py",
    ], (
        "the walk either skipped a dot-directory this repo ships or descended "
        "into dot-prefixed machine state: "
        f"{sorted(p.relative_to(tmp_path).as_posix() for p in scanned)}")

    offenders = sorted(
        f"{p.relative_to(tmp_path).as_posix()}:{lineno}"
        for p in scanned
        for lineno, _ in _iter_violations(p.read_text(encoding="utf-8"), str(p)))
    assert offenders == [".githooks/lint.py:5", ".github/scripts/report.py:5"], (
        "widening the walk must be paid out in violations actually reported — "
        f"the guard saw the files and said nothing: {offenders}")


def test_a_dot_prefix_is_not_by_itself_machine_state():
    """The rule, directly, on both the answered and the unanswered path.

    A leading dot is a *shape*, and the shape contains source. Machine state is
    named — the same treatment `venv` and `node_modules` already had — so the
    next dot-directory of ours starts visible, and the drift lands on the side
    where the worst outcome is a false alarm somebody reads rather than a
    silence nobody does. That direction is this walk's stated invariant, and
    the blanket dot rule was the one line in the file that broke it.
    """
    for ignored in (frozenset(), None):
        for source in (".github/scripts/junit_summary.py",
                       ".githooks/lint.py",
                       "presets/.ci/generate.py"):
            assert not _is_machine_state(source, ignored), (source, ignored)
        for machine in (".git/hooks/x.py",
                        ".venv/lib/python3.12/site-packages/x.py",
                        "venv/lib/python3.12/site-packages/x.py",
                        ".tox/py39/lib/x.py",
                        ".nox/py39/lib/x.py",
                        ".eggs/x.py",
                        ".pytest_cache/v/x.py",
                        ".mypy_cache/3.12/x.py",
                        ".ruff_cache/x.py",
                        ".hypothesis/x.py",
                        "presets/.venv/lib/x.py"):
            assert _is_machine_state(machine, ignored), (machine, ignored)
