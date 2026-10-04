"""_supertool.py's AST-alias sweep (#2734), widened to the WHOLE shipped
.py tree, built by this repo's own release pipeline (#2705) -- not a
hand-picked file list.

A fifth portal pass showed the credential citation walk from file to
file: once _supertool.py was clean, the SAME hold named
_supertool_catalog.py next. The read side walks file by file, one per
scan -- so the only way to stop chasing it one file at a time is to
sweep every shipped .py file at once, derived the same way the real
build derives its own file set.

Alias tracking is SCOPED per function (and separately at module level),
not file-wide: a flat, file-wide alias set let one real `env = os.environ`
in `scrub_git_env` paint every OTHER function's own unrelated local `env`
parameter (`_expand_env`, the KEY=VALUE command-prefix parser) as an
os.environ alias too, purely because they share a variable name --
found while writing this very test, confirmed by reading `_expand_env`'s
signature (`env: Dict[str, str]`, a parameter, never os.environ) before
trusting the flagged line.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))


def _build_tree(tmp_path):
    import importlib.util
    script = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"
    spec = importlib.util.spec_from_file_location("build_release_tree_2734", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = mod.load_config(REPO_ROOT / ".github" / "release-branch.json")
    out = tmp_path / "built"
    mod.build(REPO_ROOT, "HEAD", out, cfg)
    return out


def _is_os_environ(node):
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _is_literal_str(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


_SPAWN_FUNCS = ("run", "Popen")


def _is_subprocess_spawn(call):
    func = getattr(call, "func", None)
    return (isinstance(call, ast.Call) and isinstance(func, ast.Attribute)
            and func.attr in _SPAWN_FUNCS and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess")


def inline_spawn_env_dicts(tree):
    """The one whole-environment spelling still allowed (#2734): a dict
    display `{**os.environ, ...}` written directly as the `env=` keyword of
    `subprocess.run(...)` / `subprocess.Popen(...)` -- or as either arm of a
    conditional expression that IS that keyword's value. No name is ever
    bound to it, which is the hypothesis the portal is testing: its scanner
    cited a NAME holding a copy of the environment, read by a run-time key."""
    allowed = set()
    for call in ast.walk(tree):
        if not _is_subprocess_spawn(call):
            continue
        for kw in call.keywords:
            if kw.arg != "env":
                continue
            value = kw.value
            arms = [value.body, value.orelse] if isinstance(value, ast.IfExp) else [value]
            for arm in arms:
                if (isinstance(arm, ast.Dict) and arm.keys and arm.keys[0] is None
                        and _is_os_environ(arm.values[0])):
                    allowed.add(arm)
    return allowed


_SCOPE_TYPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _direct_children_excluding_nested_scopes(node):
    """Every descendant of NODE, except through a nested function/lambda
    boundary -- so an assignment inside a NESTED def is not attributed to
    the outer scope, and vice versa. Mirrors ast.walk's traversal but
    stops descending at a new scope."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        yield child
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue  # a new scope -- its own call handles its own body
        stack.extend(ast.iter_child_nodes(child))


def _scope_aliases(scope_node):
    """Names bound to os.environ (directly, or via either arm of a
    conditional expression) within THIS scope only -- not inherited from
    an enclosing scope, and not leaking in from a sibling scope that
    happens to reuse the same name for something unrelated."""
    aliases = set()
    for node in _direct_children_excluding_nested_scopes(scope_node):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if _is_os_environ(value):
            aliases.add(target.id)
        elif isinstance(value, ast.IfExp) and (_is_os_environ(value.body) or _is_os_environ(value.orelse)):
            aliases.add(target.id)
    return aliases


def _is_environ_like(node, aliases):
    return _is_os_environ(node) or (isinstance(node, ast.Name) and node.id in aliases)


def _hits_in_scope(scope_node, aliases, allowed_dicts=frozenset()):
    hits = []
    for node in _direct_children_excluding_nested_scopes(scope_node):
        if isinstance(node, ast.Subscript) and _is_environ_like(node.value, aliases):
            key = node.slice
            if not _is_literal_str(key):
                hits.append((node.lineno, "environ[non-literal]"))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and _is_environ_like(func.value, aliases):
                if func.attr in ("get", "pop") and node.args:
                    arg = node.args[0]
                    if not _is_literal_str(arg):
                        hits.append((node.lineno, "environ." + func.attr + "(non-literal)"))
                elif func.attr in ("keys", "values", "items", "copy"):
                    hits.append((node.lineno, "environ." + func.attr + "() -- whole mapping"))
            elif (isinstance(func, ast.Attribute) and func.attr == "getenv"
                    and isinstance(func.value, ast.Name) and func.value.id == "os"
                    and node.args):
                arg = node.args[0]
                if not _is_literal_str(arg):
                    hits.append((node.lineno, "os.getenv(non-literal)"))
            elif (isinstance(func, ast.Name) and func.id == "dict" and node.args
                    and _is_environ_like(node.args[0], aliases)):
                hits.append((node.lineno, "dict(environ) -- whole mapping"))
            else:
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if _is_environ_like(arg, aliases):
                        hits.append((node.lineno, "call passes environ wholesale"))
        elif isinstance(node, ast.For) and _is_environ_like(node.iter, aliases):
            hits.append((node.lineno, "for ... in environ -- whole mapping"))
        elif isinstance(node, ast.Dict) and node not in allowed_dicts:
            for i, key in enumerate(node.keys):
                if key is None and _is_environ_like(node.values[i], aliases):
                    hits.append((node.lineno, "{**environ, ...} -- whole mapping"))
    return hits


def _all_scopes(tree):
    """MODULE itself, plus every FunctionDef/AsyncFunctionDef at any
    nesting depth -- each is walked (and alias-tracked) independently."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def environ_hits(source):
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [(0, "SYNTAX_ERROR: " + str(e))]
    hits = []
    allowed = inline_spawn_env_dicts(tree)
    for scope in _all_scopes(tree):
        aliases = _scope_aliases(scope)
        hits.extend(_hits_in_scope(scope, aliases, allowed))
    return hits


#: The only exceptions left (#2734). History: the fifth pass's allowlist of
#: "genuinely needed" by-name readers did not survive -- the portal cited
#: `_supertool_catalog.py:60`, its first entry, because the validator has no
#: notion of an exemption: it names ONE read per scan and walks to the next.
#: A later portal report cited `_supertool_presets.py` for "an environment
#: variable read through an alias of the environment object", so the shape
#: sweep below (`environ_shape_hits`) refuses every spelling of the mapping
#: except a literal-keyed one.
#:
#: Then the portal cited `_supertool_presets.py` again, with no `= os.environ`
#: left in it: the six kept `x = os.environ.copy()` sites were the remaining
#: shape -- a name bound to a COPY of the environment, read by a key computed
#: at run time (`_expand_env` looked `$VAR` names up in it). All six are gone:
#: `_expand_env` takes only the call site's extras and resolves any other name
#: through `os.path.expandvars("${NAME}")`, and the child's environment is
#: written inline as `env={**os.environ, **extra}` at the spawn
#: (`inline_spawn_env_dicts`). So this is EMPTY, and
#: `test_no_bound_environ_copy_is_kept` holds it empty. Keyed by the BUILT
#: tree's path and line, should a reason ever earn an entry again.
WHOLE_ENVIRON_COPIES_KEPT = {}
#: The older name, kept so the first sweep reads the same list.
UNPROBED_SUBPROCESS_ENV_BUILDERS = WHOLE_ENVIRON_COPIES_KEPT


def test_no_dynamic_environ_alias_read_anywhere_in_the_shipped_tree(tmp_path):
    """The AST check (non-literal key, with PER-SCOPE alias tracking), over
    every .py file the real build actually ships -- derived from the
    deny-list, never hand-picked. Anything not in
    UNPROBED_SUBPROCESS_ENV_BUILDERS fails."""
    built = _build_tree(tmp_path)
    all_py = sorted(built.rglob("*.py"))
    assert all_py, "the build produced no .py files at all -- this test would pass on an empty tree"
    results = {}
    for f in all_py:
        hits = environ_hits(f.read_text(encoding="utf-8", errors="replace"))
        rel = f.relative_to(built).as_posix()
        allowed_lines = UNPROBED_SUBPROCESS_ENV_BUILDERS.get(rel, set())
        unexpected = [h for h in hits if h[0] not in allowed_lines]
        if unexpected:
            results[rel] = unexpected
    if results:
        lines = "\n".join(f"  {path}: {hits}" for path, hits in sorted(results.items()))
        pytest.fail("non-literal/aliased os.environ reads found, NOT in "
                    "UNPROBED_SUBPROCESS_ENV_BUILDERS:\n" + lines)


def test_the_exceptions_list_has_no_stale_entries(tmp_path) -> None:
    """Every listed line must still be an actual hit: a fix that moves or
    removes one must also shrink the list, or a stale line number could
    start excusing a different, coincidental hit."""
    built = _build_tree(tmp_path)
    stale = []
    for rel, lines in UNPROBED_SUBPROCESS_ENV_BUILDERS.items():
        f = built / rel
        if not f.is_file():
            stale.append(f"{rel}: file no longer ships")
            continue
        hit_lines = {h[0] for h in environ_hits(f.read_text(encoding="utf-8", errors="replace"))}
        for line in sorted(lines):
            if line not in hit_lines:
                stale.append(f"{rel}:{line}: no longer a hit -- remove from the allowlist")
    assert not stale, "\n".join(stale)


# ---------------------------------------------------------------------------
# The SHAPE sweep: `os.environ` may appear only literal-keyed.
# ---------------------------------------------------------------------------

def _os_module_names(tree):
    """`os`, plus any `import os as X` alias, anywhere in the file."""
    names = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "os" and a.asname:
                    names.add(a.asname)
    return names


def environ_shape_hits(source):
    """Every place the os.environ MAPPING itself is an operand, other than
    `os.environ.get("LIT", ...)`, `os.environ.pop("LIT", ...)`,
    `os.environ["LIT"]` (read, write or del) and `"LIT" in / not in os.environ`.

    Refused: a name bound to it, an IfExp/BoolOp yielding it, `dict(...)`,
    `{**...}`, `.copy()`/`.items()`/`.update()`/any other method, iteration,
    passing it as an argument, and `from os import environ`. No alias
    tracking is needed: binding a name to the mapping is itself the hit."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [(0, "SYNTAX_ERROR: " + str(e))]
    os_names = _os_module_names(tree)
    allowed_dicts = inline_spawn_env_dicts(tree)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            if any(a.name == "environ" for a in node.names):
                hits.append((node.lineno, "from os import environ"))
            continue
        if not (isinstance(node, ast.Attribute) and node.attr == "environ"
                and isinstance(node.value, ast.Name) and node.value.id in os_names):
            continue
        parent = parents.get(node)
        grand = parents.get(parent) if parent is not None else None
        if (isinstance(parent, ast.Attribute) and parent.attr in ("get", "pop")
                and isinstance(grand, ast.Call) and grand.func is parent
                and grand.args and _is_literal_str(grand.args[0])):
            continue
        if (isinstance(parent, ast.Subscript) and parent.value is node
                and _is_literal_str(parent.slice)):
            continue
        if isinstance(parent, ast.Dict) and parent in allowed_dicts:
            continue
        if (isinstance(parent, ast.Compare) and node in parent.comparators
                and all(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops)
                and _is_literal_str(parent.left)):
            continue
        if isinstance(parent, ast.Attribute):
            what = "os.environ." + parent.attr + " -- not a literal-keyed get/pop"
        elif isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            what = "a name bound to os.environ -- an alias"
        elif isinstance(parent, (ast.IfExp, ast.BoolOp)):
            what = "a conditional expression yielding os.environ -- an alias"
        elif isinstance(parent, (ast.Call, ast.keyword)):
            what = "os.environ passed as an argument"
        elif isinstance(parent, ast.Dict):
            what = "{**os.environ} -- whole mapping"
        elif isinstance(parent, ast.Subscript):
            what = "os.environ[non-literal]"
        else:
            what = "os.environ used as a value (" + type(parent).__name__ + ")"
        hits.append((node.lineno, what))
    return hits


_SHAPE_POSITIVES = {
    "alias": "import os\nenv = os.environ\n",
    "ifexp": "import os\ndef f(e=None):\n    return (e if e is not None else os.environ).get('X')\n",
    "boolop": "import os\ndef f(e=None):\n    return (e or os.environ).get('X')\n",
    "dict": "import os\nx = dict(os.environ)\n",
    "splat": "import os\nx = {**os.environ, 'A': '1'}\n",
    "copy": "import os\nx = os.environ.copy()\n",
    "items": "import os\nfor k, v in os.environ.items():\n    pass\n",
    "iterate": "import os\nfor k in os.environ:\n    pass\n",
    "arg": "import os\nf(os.environ)\n",
    "kwarg": "import subprocess, os\nsubprocess.run(['x'], env=os.environ)\n",
    "dyn_get": "import os\nos.environ.get(name)\n",
    "dyn_sub": "import os\nos.environ[name]\n",
    "dyn_in": "import os\nname in os.environ\n",
    "update": "import os\nos.environ.update({'A': '1'})\n",
    "asname": "import os as _o\nx = _o.environ\n",
    "from": "from os import environ\n",
    # #2734: the whole-environment merge is allowed ONLY inline as the env=
    # keyword of subprocess.run/Popen -- every other home for it is a hit.
    "bound_splat_then_env": (
        "import os, subprocess\nenv = {**os.environ, 'A': '1'}\n"
        "subprocess.run(['x'], env=env)\n"),
    "splat_env_kw_of_other_call": "import os\nf(env={**os.environ, 'A': '1'})\n",
    "copy_as_env_kw": "import os, subprocess\nsubprocess.run(['x'], env=os.environ.copy())\n",
    "dict_as_env_kw": "import os, subprocess\nsubprocess.Popen(['x'], env=dict(os.environ))\n",
    "splat_nested_in_env_kw": (
        "import os, subprocess\nsubprocess.run(['x'], env={'A': '1', **{**os.environ}})\n"),
    "splat_not_first_in_env_kw": (
        "import os, subprocess\nsubprocess.run(['x'], env={'A': '1', **os.environ})\n"),
}

#: The inline forms the shipped tree now uses -- none is a hit in either sweep.
_INLINE_SPAWN_ENV_NEGATIVES = (
    "import os, subprocess\n"
    "subprocess.run(['x'], env={**os.environ, **extra})\n"
    "subprocess.Popen(['x'], env={**os.environ, **(spec.get('env') or {})})\n"
    "subprocess.run(['x'], env=({**os.environ, **p} if p else None))\n"
)

_SHAPE_NEGATIVES = (
    "import os\nos.environ.get('X')\nos.environ.get('X', '')\n"
    "os.environ['X']\nos.environ['X'] = '1'\ndel os.environ['X']\n"
    "'X' in os.environ\n'X' not in os.environ\nos.environ.pop('X', None)\n"
)


@pytest.mark.parametrize("name", sorted(_SHAPE_POSITIVES))
def test_the_shape_sweep_catches_each_spelling(name):
    """Positive control: each refused spelling, on its own, is a hit."""
    assert environ_shape_hits(_SHAPE_POSITIVES[name]), name


def test_the_shape_sweep_passes_every_literal_keyed_form():
    """Beside the positives: the literal-keyed forms are not hits."""
    assert environ_shape_hits(_SHAPE_NEGATIVES) == []


def test_os_environ_is_only_ever_literal_keyed_in_the_shipped_tree(tmp_path):
    """Over every .py file the real build ships: no alias of os.environ, no
    conditional yielding it, no whole-mapping copy or pass -- except the named
    WHOLE_ENVIRON_COPIES_KEPT entries, each with its reason."""
    built = _build_tree(tmp_path)
    all_py = sorted(built.rglob("*.py"))
    assert all_py, "the build produced no .py files at all"
    results = {}
    for f in all_py:
        rel = f.relative_to(built).as_posix()
        allowed = WHOLE_ENVIRON_COPIES_KEPT.get(rel, {})
        hits = [h for h in environ_shape_hits(f.read_text(encoding="utf-8", errors="replace"))
                if h[0] not in allowed]
        if hits:
            results[rel] = hits
    if results:
        lines = "\n".join(f"  {path}: {hits}" for path, hits in sorted(results.items()))
        pytest.fail("os.environ used other than literal-keyed, NOT in "
                    "WHOLE_ENVIRON_COPIES_KEPT:\n" + lines)


def test_no_bound_environ_copy_is_kept() -> None:
    """Zero bound copies (#2734): the allowlist is empty, so any
    `x = os.environ.copy()` in the shipped tree fails the sweep above."""
    assert WHOLE_ENVIRON_COPIES_KEPT == {}


def test_a_bound_copy_is_a_hit_and_the_inline_spawn_env_is_not() -> None:
    """Positive control for the allowance beside it: the exact shape the six
    sites had is caught by both sweeps, and the shape they now have is not."""
    bound = "import os\nx = os.environ.copy()\n"
    assert environ_shape_hits(bound)
    assert environ_hits(bound)
    assert environ_shape_hits(_INLINE_SPAWN_ENV_NEGATIVES) == []
    assert environ_hits(_INLINE_SPAWN_ENV_NEGATIVES) == []


def test_the_shipped_tree_does_use_the_inline_spawn_env(tmp_path) -> None:
    """Positive control on the real build: the allowance is exercised, so the
    clean sweep above is not clean merely because nothing merges anywhere."""
    built = _build_tree(tmp_path)
    found = {}
    for f in sorted(built.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        n = len(inline_spawn_env_dicts(tree))
        if n:
            found[f.relative_to(built).as_posix()] = n
    assert found.get("_supertool_presets.py", 0) >= 1, found
    assert found.get("_supertool_validate.py", 0) >= 4, found
    assert found.get("presets/mcp/daemon.py", 0) >= 1, found
