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


def _hits_in_scope(scope_node, aliases):
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
        elif isinstance(node, ast.Dict):
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
    for scope in _all_scopes(tree):
        aliases = _scope_aliases(scope)
        hits.extend(_hits_in_scope(scope, aliases))
    return hits


def test_no_dynamic_environ_alias_read_anywhere_in_the_shipped_tree(tmp_path):
    """The AST check (non-literal key, with PER-SCOPE alias tracking), over
    every .py file the real build actually ships -- derived from the
    deny-list, never hand-picked."""
    built = _build_tree(tmp_path)
    all_py = sorted(built.rglob("*.py"))
    assert all_py, "the build produced no .py files at all -- this test would pass on an empty tree"
    results = {}
    for f in all_py:
        hits = environ_hits(f.read_text(encoding="utf-8", errors="replace"))
        if hits:
            results[str(f.relative_to(built))] = hits
    if results:
        lines = "\n".join(f"  {path}: {hits}" for path, hits in sorted(results.items()))
        pytest.fail("non-literal/aliased os.environ reads found:\n" + lines)
