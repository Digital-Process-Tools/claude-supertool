"""Guard against #529: real subprocess spawns must use sys.executable, not
a bare "python3" string.

On Windows, PATH resolution of the literal `"python3"` can hit the App
Execution Alias stub, which *blocks* instead of erroring — a child process
that never starts, dressed up as a `subprocess.TimeoutExpired` on an
otherwise-healthy adapter (see PR #527's Windows/3.10 flake on
`test_validators.py::test_phplint_adapter_valid_php`). `sys.executable`
guarantees the child interpreter is the one actually running the suite,
removing that bet entirely.

This is scoped to genuine `subprocess.run` / `subprocess.Popen` /
`subprocess.check_output` / `subprocess.check_call` call sites whose
executable argument is the literal string `"python3"`. It intentionally
does NOT do a blind grep over tests/ — several files (e.g.
test_watch_pid_set_511.py, test_watch_death_supervision_513.py) contain
`"python3"` as *fixture data* describing a fake process-table row that the
code under test parses. That data is the thing being tested, not a spawn,
and must not be flagged or "fixed".

The call must be resolved through whatever name it is actually reachable
under in that file, not just the literal spelling `subprocess.run`. The
suite has an established idiom of `import subprocess as sp` (or `_sp`,
`_sub`) — eight sites across six files as of this writing
(test_op_workspace.py, test_security_mcp_daemon.py, test_map.py x4,
test_git_conflicts.py, test_read.py) — and a guard that only recognizes
the unaliased spelling is blind to a spawn written the way this suite
already writes them routinely. That is the same defect #529 is about, one
level in: a check that cannot see, producing a signal indistinguishable
from clean. So this module resolves, per file, both:

- `import subprocess [as X]` -> `X.run(...)` / `X.Popen(...)` / etc.
- `from subprocess import run [as Y]` (etc.) -> bare `Y(...)` calls

before deciding whether a call site is a subprocess spawn.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).parent

_SUBPROCESS_SPAWN_ATTRS = {"run", "Popen", "check_output", "check_call"}


def _collect_subprocess_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return (module_names, direct_function_names) bound to the subprocess
    module / its spawn functions anywhere in this file, however imported.

    module_names: names that refer to the `subprocess` module itself, e.g.
    `subprocess` (default) or `sp` / `_sp` / `_sub` (aliased). A call
    `X.run(...)` counts as a spawn iff X is one of these and `run` is in
    _SUBPROCESS_SPAWN_ATTRS.

    direct_function_names: names that refer to one of the spawn functions
    directly, e.g. `run` from `from subprocess import run`, or `r` from
    `from subprocess import run as r`. A bare call `Y(...)` counts as a
    spawn iff Y is one of these.
    """
    module_names: set[str] = set()
    direct_function_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    if alias.name in _SUBPROCESS_SPAWN_ATTRS:
                        direct_function_names.add(alias.asname or alias.name)

    return module_names, direct_function_names


def _iter_bare_python3_spawns(source: str, filename: str):
    """Yield line numbers for subprocess spawns using literal "python3",
    resolving whatever name(s) `subprocess` (or its spawn functions) are
    bound to in this specific file — plain, aliased, or imported directly.
    """
    tree = ast.parse(source, filename=filename)
    module_names, direct_function_names = _collect_subprocess_bindings(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_subprocess_call = (
            isinstance(func, ast.Attribute)
            and func.attr in _SUBPROCESS_SPAWN_ATTRS
            and isinstance(func.value, ast.Name)
            and func.value.id in module_names
        ) or (
            isinstance(func, ast.Name) and func.id in direct_function_names
        )
        if not is_subprocess_call or not node.args:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, (ast.List, ast.Tuple)) or not first_arg.elts:
            continue
        executable = first_arg.elts[0]
        if isinstance(executable, ast.Constant) and executable.value == "python3":
            yield node.lineno


def _find_offenders() -> list[str]:
    offenders = []
    # rglob, not glob: tests/fixtures/ holds real .py files (e.g.
    # mock_mcp_server.py, fixtures/resolve/mypkg/utils.py) that are not
    # pytest test modules but ARE Python source that could itself spawn a
    # subprocess with a bare "python3" — nothing about being "a fixture
    # directory" exempts a .py file in it from the same PATH-resolution
    # bet. Recursing costs nothing extra here (a handful of files) and
    # closing this off deliberately beats discovering it the way #529's
    # own bug was discovered: by a flake nobody could explain.
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for lineno in _iter_bare_python3_spawns(source, str(path)):
            offenders.append(f"{path.relative_to(TESTS_DIR.parent)}:{lineno}")
    return offenders


def test_no_subprocess_spawn_uses_bare_python3():
    """Every subprocess.run/Popen/check_output/check_call spawn in tests/
    (including tests/fixtures/), however the subprocess module or its
    functions are imported/aliased in that file, must use sys.executable
    (or an explicitly-resolved interpreter), never the literal "python3" —
    that string is a PATH-resolution bet that flakes on Windows (#529)."""
    offenders = _find_offenders()
    assert not offenders, (
        "subprocess spawn(s) using literal \"python3\" instead of sys.executable "
        "(#529 — flakes on Windows via App Execution Alias PATH resolution):\n"
        + "\n".join(offenders)
    )


def test_guard_detects_aliased_subprocess_import_spawn():
    """Self-test: the guard's own correctness was unpinned, which is how it
    acquired the aliased-import blind spot in the first place (it only
    recognized the literal spelling `subprocess.run`, missing the `import
    subprocess as sp` idiom this suite already uses 8 times across 6
    files). Feed it a snippet using that idiom with a bare "python3" spawn
    and assert it is flagged, so a future edit can't silently reintroduce
    the hole."""
    source = (
        "import subprocess as sp\n"
        "\n"
        "def f():\n"
        "    sp.run([\"python3\", \"script.py\"], capture_output=True)\n"
    )
    offenders = list(_iter_bare_python3_spawns(source, "snippet.py"))
    assert offenders == [4]


def test_guard_detects_direct_from_import_spawn():
    """Self-test: `from subprocess import run` binds `run` directly, with
    no `subprocess.` or module-alias prefix at the call site at all. The
    guard must resolve this binding too, not just attribute access."""
    source = (
        "from subprocess import run as r\n"
        "\n"
        "def f():\n"
        "    r([\"python3\", \"script.py\"], capture_output=True)\n"
    )
    offenders = list(_iter_bare_python3_spawns(source, "snippet.py"))
    assert offenders == [4]


def test_guard_does_not_flag_fixture_argv_data():
    """Self-test: a list literal starting with "python3" that is NOT the
    first argument to a subprocess.* call (e.g. fixture data describing a
    fake process-table row, as in test_watch_pid_set_511.py) must not be
    flagged. This is what keeps the guard from needing a hardcoded
    exclusion list for those files."""
    source = (
        "import subprocess\n"
        "\n"
        "def f(machine):\n"
        "    machine.add_process(201, [\"python3\", \"/x/dispatcher.py\"])\n"
    )
    offenders = list(_iter_bare_python3_spawns(source, "snippet.py"))
    assert offenders == []
