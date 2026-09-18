"""Register: a bare string literal at argv[0], with no cwd-excluding
resolution anywhere in the same file, is a third variant this repo's other
two spawn registers cannot see (#2605).

`tests/test_spawnable_register_2540.py` only flags a bare literal that the
SAME file also separately proves with a raw `shutil.which(literal)` call --
`validators/phpstan/phpstan.py` never calls `shutil.which("php")` at all, it
calls `spawnable(phpstan_bin)`, a different name, so the intersection that
register keys on is empty and `"php"` at argv[0] is invisible to it.

`tests/test_gate_matches_spawn_2579.py` only inspects whether a file's GATE
(`shutil.which()` or a bare existence check) disagrees with a spawn already
routed through the chokepoint -- it never inspects the argv construction
itself, so it cannot tell that `argv[0]` is a bare literal at all. Before
#2605's fix, `phpstan.py` imported the chokepoint (`argv0`/`spawnable`) and
called neither raw `which()` nor a bare existence check, so it read as
clean to that register despite `argv[0]` never once passing through
`argv0()`/`spawnable()`/`which_excluding_cwd()`.

This register inspects the constructed argv list directly: for every list
literal used as (or feeding) a subprocess spawn, is its first element a
bare string literal that never appears as an argument to
`spawnable()`/`argv0()`/`which_excluding_cwd()`/`resolve_bin_cmd()`
anywhere in the same file? If so, nothing in this file resolves it through
the cwd-excluding chokepoint before it reaches `subprocess.run()`/`Popen()`
-- the exact shape #2605 found at `phpstan.py:162`, one argv slot over from
the tool (`phpstan_bin`) that WAS gated.

Scoped to files that import the chokepoint at all is deliberately NOT this
register's condition (unlike #2579's), because the two sibling instances
named in #2605's own scope note -- `validators/phplint/phplint.py` (bare
`"php"`) and `validators/xmllint/xmllint.py` (bare `"xmllint"`) -- import no
chokepoint whatsoever, and a register gated on "already uses argv0/
spawnable somewhere" would stay blind to them too, the same way #2579's
does. Both are real, tracked separately (out of scope for #2605's own PR,
per its own scope note), and allowlisted here rather than fixed in this
change -- removing an allowlist entry is the signal that one has been
closed.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ADAPTER_DIRS = ("validators", "formatters")

#: The chokepoint modules themselves construct argv-shaped lists as part of
#: their own implementation (searching PATH, building candidate paths) --
#: that is the mechanism, not an instance of the defect.
CHOKEPOINT_FILES = {"spawnable.py", "bin_resolve.py"}

CHOKEPOINT_FUNCS = {"spawnable", "argv0", "which_excluding_cwd", "resolve_bin_cmd"}

#: Known, real instances of this exact class not fixed by #2605's own PR --
#: named in the issue's own "Scope note for the maintainer" as a separate,
#: larger decision. Kept here rather than silently caught and ignored: an
#: allowlist entry read as "this file is fine" would be the same absence
#: this codebase keeps re-discovering under a different name. Removing an
#: entry is the signal one has actually been fixed.
ALLOWLIST = {
    "validators/phplint/phplint.py",  # bare "php" argv[0], no chokepoint import at all
    "validators/xmllint/xmllint.py",  # bare "xmllint" argv[0], no chokepoint import at all
    # The following were NOT named by #2605's own issue text -- this
    # register found them once it could inspect argv construction directly
    # rather than only a gate/spawn mismatch. Same shape as the two above
    # (no chokepoint import at all, no gate of any kind beyond a bare
    # `except FileNotFoundError`): real, unfixed instances of the class,
    # out of scope for #2605's own PR (one named site, `phpstan.py`), and
    # reported to the maintainer as adjacent findings rather than fixed
    # here. Removing an entry is the signal one has actually been closed.
    "validators/bash-check/bash-check.py",  # bare "bash" argv[0]
    "validators/changelog-fragment/changelog-fragment.py",  # bare "git" argv[0]
    "validators/common/ci_lint_resolve_root.py",  # bare "git" argv[0]
    "validators/common/encoding_seam.py",  # bare "git" argv[0]
    "validators/markdownlint/markdownlint.py",  # bare "git" argv[0]
    "validators/new-file-lint/new-file-lint.py",  # bare "git" argv[0], two sites
    "validators/node-check/node-check.py",  # bare "node" argv[0]
}


def _adapter_sources() -> "list[pathlib.Path]":
    out = []
    for d in ADAPTER_DIRS:
        out.extend(sorted((ROOT / d).rglob("*.py")))
    return [p for p in out if p.name not in CHOKEPOINT_FILES]


def _chokepoint_resolved_names(tree: ast.AST) -> "set[str]":
    """Every string literal passed as the first argument to
    `spawnable()`/`argv0()`/`which_excluding_cwd()`/`resolve_bin_cmd()`,
    called either as a bare name or as `module.func(...)`.
    """
    names = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        fn = node.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if fname not in CHOKEPOINT_FUNCS:
            continue
        arg0 = node.args[0]
        if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
            names.add(arg0.value)
    return names


SPAWN_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}


def _spawn_arg_lists(tree: ast.AST) -> "list[ast.List]":
    """List literals that actually BECOME argv at a subprocess spawn: the
    direct first positional argument to `subprocess.run()`/`Popen()`/etc,
    or a list literal plainly assigned (`cmd = [...]`, never `cmd += [...]`
    or `base + [...]`) to a variable name later used as such a spawn's
    first positional argument.

    Scoped this way rather than "every list literal in the file"
    (`test_spawnable_register_2540.py::_spawned_heads`'s broader net):
    that walker only ever intersects a head against names ALREADY proved
    with `which()`, so a flag list like `["-f", "json", ...]` or
    `["--config", cfg]` never collides with anything and costs nothing.
    Here every bare-string head is a candidate offender on its own, so the
    same broad net would flag every flag list in the tree (measured: 22
    false positives across 12 files on the first draft) -- argv[0] is what
    this class is about, and a flag list concatenated on with `cmd +=` or
    `base + [...]` is never in that position.
    """
    spawn_names = set()
    lists: "list[ast.List]" = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if fname not in SPAWN_FUNCS or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.List):
            lists.append(first)
        elif isinstance(first, ast.Name):
            spawn_names.add(first.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in spawn_names:
                    lists.append(node.value)
    return lists


def _argv_list_heads(tree: ast.AST) -> "list[tuple[int, ast.expr]]":
    """`(line, argv[0]-node)` for every list literal that reaches a
    subprocess spawn as its actual argv (see `_spawn_arg_lists`)."""
    return [(lst.lineno, lst.elts[0]) for lst in _spawn_arg_lists(tree) if lst.elts]


def _offenders() -> "list[str]":
    hits = []
    for path in _adapter_sources():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        resolved = _chokepoint_resolved_names(tree)
        for line, head in _argv_list_heads(tree):
            if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
                continue
            if head.value in resolved:
                continue
            hits.append(
                f"{rel}:{line} heads an argv list with the bare literal "
                f"{head.value!r}, never passed to spawnable()/argv0()/"
                f"which_excluding_cwd()/resolve_bin_cmd() anywhere in this file"
            )
    return hits


def test_no_argv_head_is_a_bare_literal_with_no_chokepoint_resolution() -> None:
    offenders = _offenders()
    assert not offenders, (
        "these argv[0] positions are bare string literals with no "
        "cwd-excluding resolution anywhere in the same file -- the "
        "program subprocess.run()/Popen() actually executes is never "
        "routed through spawnable()/argv0()/which_excluding_cwd() at "
        "all, so a repo-planted binary of that name (the repository "
        "under inspection, since these spawns pass no cwd=) can be "
        "resolved ahead of the real tool on Windows (#2605):\n  "
        + "\n  ".join(offenders)
    )


def test_the_register_catches_phpstans_pre_fix_shape() -> None:
    """Positive control: the exact shape #2605 found at `phpstan.py:162` --
    `phpstan_bin` correctly gated and resolved via `argv0()`, `"php"` left
    as a bare literal at argv[0], with `spawnable()`/`argv0()` imported and
    used in the same file (so #2579's own register sees nothing wrong: the
    chokepoint IS imported and used, and there is no raw `which()` call and
    no bare existence check to disagree with it).
    """
    vulnerable = (
        "import subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(phpstan_bin):\n"
        "    decline()\n"
        "cmd = ['php', '-d', 'memory_limit=1G', argv0(phpstan_bin), 'analyse']\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(vulnerable)
    resolved = _chokepoint_resolved_names(tree)
    assert "phpstan_bin" not in resolved, (
        "phpstan_bin is a variable, not a string literal argument -- "
        "the walker must not accidentally resolve it as a literal name"
    )
    heads = _argv_list_heads(tree)
    assert any(isinstance(h, ast.Constant) and h.value == "php" for _, h in heads), (
        "the argv-head walker does not see the bare 'php' literal at all"
    )
    assert "php" not in resolved, (
        "the walker thinks 'php' was resolved through the chokepoint when "
        "nothing in this fixture ever passes the literal 'php' to "
        "spawnable()/argv0()"
    )


def test_the_fixed_shape_is_not_flagged() -> None:
    """Positive control's mirror: #2605's own fix, resolving `php` the same
    way `phpstan_bin` already was, must clear the register.
    """
    fixed = (
        "import subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(phpstan_bin):\n"
        "    decline()\n"
        "if not spawnable('php'):\n"
        "    decline()\n"
        "cmd = [argv0('php'), '-d', 'memory_limit=1G', argv0(phpstan_bin), 'analyse']\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(fixed)
    resolved = _chokepoint_resolved_names(tree)
    assert "php" in resolved, "spawnable('php') must register 'php' as resolved"
    for line, head in _argv_list_heads(tree):
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            assert head.value in resolved, (
                f"line {line}: literal {head.value!r} at argv[0] is not "
                "recognised as resolved even though the fixture passes it "
                "to spawnable() before the spawn"
            )


def test_a_module_style_chokepoint_call_is_still_seen() -> None:
    """`import spawnable` + `spawnable.argv0('php')` (module-attribute call
    style) is a second way to reach the chokepoint, the same shape
    `test_gate_matches_spawn_2579.py::test_a_module_style_chokepoint_import_is_still_seen`
    already guards for its own walker.
    """
    fixed = (
        "import subprocess, spawnable\n"
        "cmd = [spawnable.argv0('php')]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(fixed)
    resolved = _chokepoint_resolved_names(tree)
    assert "php" in resolved, (
        "the module-attribute call style (spawnable.argv0('php')) is "
        "invisible to the walker"
    )


def test_a_name_or_attribute_head_is_never_flagged() -> None:
    """Negative control: argv[0] built from a resolved variable
    (`cmd = [php_bin, ...]`) or an attribute (`sys.executable`) is not a
    bare literal at all -- nothing for this register to check it against,
    and it must not be flagged just for not being a Constant.
    """
    fine = (
        "import subprocess, sys\n"
        "php_bin = resolve_php()\n"
        "cmd = [php_bin, 'analyse']\n"
        "cmd2 = [sys.executable, '-c', 'pass']\n"
        "subprocess.run(cmd)\n"
        "subprocess.run(cmd2)\n"
    )
    tree = ast.parse(fine)
    resolved = _chokepoint_resolved_names(tree)
    for _, head in _argv_list_heads(tree):
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            assert head.value in resolved


def test_the_allowlisted_sibling_instances_are_still_real() -> None:
    """The two allowlist entries must still name genuine, unfixed
    instances of the class -- an allowlist that outlives the bug it
    excuses is the same silent absence this register exists to prevent.
    """
    for rel in sorted(ALLOWLIST):
        path = ROOT / rel
        assert path.is_file(), f"{rel} no longer exists -- drop it from ALLOWLIST"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        resolved = _chokepoint_resolved_names(tree)
        offending = [
            head.value for _, head in _argv_list_heads(tree)
            if isinstance(head, ast.Constant) and isinstance(head.value, str)
            and head.value not in resolved
        ]
        assert offending, (
            f"{rel} is allowlisted as a known unfixed instance of #2605's "
            "class, but the register no longer finds one -- it has "
            "apparently been fixed; remove it from ALLOWLIST"
        )


def test_the_register_covers_a_population_it_can_name() -> None:
    """A register over zero files is green and means nothing."""
    sources = _adapter_sources()
    assert len(sources) >= 40, (
        f"only {len(sources)} adapter sources found under "
        f"{ADAPTER_DIRS} -- the walk root is wrong, and an empty walk "
        "reads exactly like a clean one"
    )
    assert any(_argv_list_heads(ast.parse(p.read_text(encoding="utf-8")))
               for p in sources), (
        "no adapter builds a list literal at all, so this register is "
        "asserting nothing about anything"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
