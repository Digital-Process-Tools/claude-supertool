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
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).parent

_SUBPROCESS_SPAWN_ATTRS = {"run", "Popen", "check_output", "check_call"}


def _iter_bare_python3_spawns(source: str, filename: str):
    """Yield (lineno, filename) for subprocess spawns using literal "python3"."""
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_subprocess_call = (
            isinstance(func, ast.Attribute)
            and func.attr in _SUBPROCESS_SPAWN_ATTRS
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )
        if not is_subprocess_call or not node.args:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, (ast.List, ast.Tuple)) or not first_arg.elts:
            continue
        executable = first_arg.elts[0]
        if isinstance(executable, ast.Constant) and executable.value == "python3":
            yield node.lineno


def test_no_subprocess_spawn_uses_bare_python3():
    """Every subprocess.run/Popen/check_output/check_call spawn in tests/
    must use sys.executable (or an explicitly-resolved interpreter), never
    the literal "python3" — that string is a PATH-resolution bet that flakes
    on Windows (#529)."""
    offenders = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        for lineno in _iter_bare_python3_spawns(source, str(path)):
            offenders.append(f"{path.relative_to(TESTS_DIR.parent)}:{lineno}")

    assert not offenders, (
        "subprocess spawn(s) using literal \"python3\" instead of sys.executable "
        "(#529 — flakes on Windows via App Execution Alias PATH resolution):\n"
        + "\n".join(offenders)
    )
