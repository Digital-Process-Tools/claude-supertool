"""#2015 -- the GitHub twin must retry a harness death exactly like the
GitLab twin does (#2235), or the mitigation regresses to whichever file was
ported last.

`_nested_pytest_verdict.run_with_harness_retry` exists because a Windows
runner can race an unrelated process's temp-directory churn during the
nested child's collection -- #2015's own instrumentation and private-tmp
redirect narrow what THIS process touches but cannot stop that race (see
`_isolated_child_tmp.py`'s docstring). #2235 added a retry-on-harness-death
to `test_gl_repo_target_676.py`'s copy of this probe after a seventh
occurrence of that race; the sixth (2026-09-04, `firefox` in the child's
`%TEMP%`) landed on `test_repo_target_673.py` instead -- the one file that
never got the retry, because #2235's title scoped itself to "the #676 test"
alone.

This is a static parity check, not a live rerun of the race
(unreproducible on this machine): parse both files, resolve each one's own
import alias for `_nested_pytest_verdict.run_with_harness_retry` (the two
files spell it differently -- plain and `_`-prefixed), and assert an actual
`ast.Call` node using that alias sits inside the body of each file's own
`test_the_env_var_main_sets_does_not_survive_into_the_next_test`.

Deliberately not a substring check over the function's source text: a
docstring that explains the retry mentions the function's name too, so
`"run_with_harness_retry" in source` is satisfied by prose alone and would
stay green even if the real call were reverted -- it fails on the
pre-fix source of `test_repo_target_673.py` for the wrong reason (the name
appears nowhere at all there, prose included), which is exactly the case
that would stop distinguishing "never ported" from "ported, then quietly
dropped" once the docstring existed. Resolving the call node is what tells
the two apart.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "tests"
PROBE_TEST = "test_the_env_var_main_sets_does_not_survive_into_the_next_test"
TARGET_MODULE = "_nested_pytest_verdict"
TARGET_NAME = "run_with_harness_retry"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _local_alias(tree: ast.Module, module: str, name: str) -> str:
    """The local name a `from module import name [as alias]` bound, in this
    file -- so a caller can look for the alias actually used here rather
    than assuming every file spells the import the same way."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            for alias in node.names:
                if alias.name == name:
                    return alias.asname or alias.name
    raise AssertionError(f"no 'from {module} import {name}' found")


def _function_node(tree: ast.Module, func_name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    raise AssertionError(f"{func_name} not found")


def _calls_by_local_name(func_node: ast.FunctionDef, local_name: str) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == local_name
        for n in ast.walk(func_node)
    )


def _probe_retries_via_shared_helper(path: Path) -> bool:
    tree = _parse(path)
    alias = _local_alias(tree, TARGET_MODULE, TARGET_NAME)
    func_node = _function_node(tree, PROBE_TEST)
    return _calls_by_local_name(func_node, alias)


def test_github_twin_retries_a_harness_death_like_the_gitlab_twin_does():
    gl_path = TESTS / "test_gl_repo_target_676.py"
    gh_path = TESTS / "test_repo_target_673.py"

    assert _probe_retries_via_shared_helper(gl_path), (
        "the GitLab twin lost its own #2235 retry -- this test's baseline moved"
    )
    assert _probe_retries_via_shared_helper(gh_path), (
        "the GitHub twin (#673) still asserts on a single spawn attempt -- "
        "a harness death (exit 2/3/4/5, no verdict about the product) fails "
        "the whole suite leg instead of retrying once, exactly the gap the "
        "2026-09-04 'firefox' occurrence exposed"
    )


def test_a_docstring_mentioning_the_helper_is_not_enough_on_its_own():
    """Guards the guard: a function that only *talks about* the retry, with
    no call node using the resolved local alias, must not pass."""
    src = (
        "from _nested_pytest_verdict import run_with_harness_retry\n\n\n"
        "def test_the_env_var_main_sets_does_not_survive_into_the_next_test():\n"
        '    """`run_with_harness_retry` retries only a harness death."""\n'
        "    pass\n"
    )
    tree = ast.parse(src, filename="<prose-only-fixture>")
    alias = _local_alias(tree, TARGET_MODULE, TARGET_NAME)
    func_node = _function_node(tree, PROBE_TEST)

    assert not _calls_by_local_name(func_node, alias)
