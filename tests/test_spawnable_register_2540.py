"""No adapter may spawn a name it only proved with `shutil.which()` (#2540).

`shutil.which()` consults ``PATHEXT`` and answers about `eslint.cmd`;
``subprocess`` without a shell goes through ``CreateProcess``, which appends
only ``.exe`` when it searches ``PATH`` and so cannot find that file at all.
Gate on the first and spawn the second and the two disagree on every Windows
install -- `which()` says present, the spawn raises ``FileNotFoundError``.

Measured rather than argued: ``tests/test_windows_cmd_spawn_2540.py`` puts a
``.cmd`` shim on ``PATH`` and asks the platform. It also measured the fix --
spawning the string ``which()`` returned works, so the bare name is the whole
defect and the resolved path is the whole repair. No shell, no ``cmd /c``.

This is the register that keeps the class closed. Seven adapters had it when
#2540 was filed and a new one would not have been noticed: the failure mode
is a `skipped` verdict, which is indistinguishable from a tool nobody
installed, on a platform none of us writes on.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ADAPTER_DIRS = ("validators", "formatters")
SPAWNERS = {"run", "Popen", "check_output", "call", "check_call"}


def _adapter_sources() -> "list[pathlib.Path]":
    out = []
    for d in ADAPTER_DIRS:
        out.extend(sorted((ROOT / d).rglob("*.py")))
    return out


def _which_arguments(tree: ast.AST) -> "set[str]":
    """Every expression handed to `shutil.which(...)`, as source text."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name == "which":
            found.add(ast.unparse(node.args[0]))
    return found


def _spawned_heads(tree: ast.AST) -> "list[tuple[int, str]]":
    """`(line, source of the first element)` for every list literal in the file.

    Every list literal, not only the ones handed straight to `subprocess`.
    Three of the seven adapters #2540 named build their argv prefix in a
    helper and return it -- `eslint._resolve_cmd` returns `[TOOL]` or
    `["npx", "--no-install", TOOL]` -- and the spawn is then
    `subprocess.run(base + [...])`, whose first element is a Name this
    walker cannot resolve. A register that only looked at spawn sites saw
    16 of them and silently missed those three, which is the same defect
    it exists to catch, one level up.

    The cost is that an unrelated list whose first element happens to be a
    which()-probed name is flagged too. That direction is the safe one: a
    false positive is read and dismissed, a false negative ships.
    """
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts:
            out.append((node.lineno, ast.unparse(node.elts[0])))
    return out


def _offenders(path: pathlib.Path) -> "list[str]":
    tree = ast.parse(path.read_text(encoding="utf-8"))
    gated = _which_arguments(tree)
    if not gated:
        return []
    hits = []
    for line, head in _spawned_heads(tree):
        if head in gated:
            rel = path.relative_to(ROOT)
            hits.append(f"{rel}:{line} heads an argv with `{head}`, which "
                        f"this file only proved with shutil.which({head})")
    return hits


def test_no_adapter_spawns_a_name_it_only_proved_with_which() -> None:
    """Scope note (#2606): `_offenders()` only flags a bare-spawned
    literal when the SAME file also separately calls `shutil.which()` on
    that exact literal -- the `which()`-then-spawn-the-name-anyway
    mismatch #2540 itself measured. A file that bare-spawns a literal it
    never looks up at all (no `which()` call for it anywhere -- the
    #2605 phpstan.py shape) is a DIFFERENT, unconditional class this
    register was never built to see: it passes here silently, and is
    caught instead by `tests/test_bare_argv0_construction_2605.py`, which
    inspects argv construction directly rather than requiring a `which()`
    call to intersect against. A green result here is "no adapter both
    proves a name via which() and then spawns that same name anyway" --
    not "no adapter spawns an unresolved bare literal"."""
    offenders = []
    for path in _adapter_sources():
        offenders.extend(_offenders(path))
    assert not offenders, (
        "on Windows shutil.which() resolves these to a .cmd that "
        "CreateProcess cannot execute, so the adapter declines on every "
        "install where the tool is present (#2540). Spawn the string "
        "which() returned instead -- `spawnable(name)` in "
        "validators/common/spawnable.py:\n  " + "\n  ".join(offenders)
    )


def test_the_register_can_actually_see_the_defect() -> None:
    """Positive control. Without it the test above also passes when the
    walker is broken, matches nothing, or is pointed at an empty tree --
    which is this repository's recurring defect wearing a test's clothes.
    """
    import tempfile
    bad = (
        "import shutil, subprocess\n"
        "if shutil.which('eslint'):\n"
        "    subprocess.run(['eslint', '--fix', f], capture_output=True)\n"
    )
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "fake_adapter.py"
        p.write_text(bad, encoding="utf-8")
        tree = ast.parse(bad)
        assert "'eslint'" in _which_arguments(tree), "the which() walker is blind"
        heads = [h for _, h in _spawned_heads(tree)]
        assert "'eslint'" in heads, "the argv walker is blind"


def test_the_register_covers_a_population_it_can_name() -> None:
    """A register over zero files is green and means nothing."""
    sources = _adapter_sources()
    assert len(sources) >= 40, (
        f"only {len(sources)} adapter sources found under "
        f"{ADAPTER_DIRS} -- the walk root is wrong, and an empty walk "
        "reads exactly like a clean one"
    )
    assert any(_which_arguments(ast.parse(p.read_text(encoding="utf-8")))
               for p in sources), (
        "no adapter calls shutil.which() at all, so this register is "
        "asserting nothing about anything"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
