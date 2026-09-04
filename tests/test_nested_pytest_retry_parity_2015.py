"""#2015 -- the GitHub twin must retry a harness death exactly like the
GitLab twin does (#2235), or the mitigation regresses to whichever file was
ported last.

`_nested_pytest_verdict.run_with_harness_retry` exists because a Windows
runner can race an unrelated process's temp-directory churn during the
nested child's collection -- #2015's own instrumentation and private-tmp
redirect narrow what THIS process touches but cannot stop that race (see
`_isolated_child_tmp.py`'s docstring). #2235 added a retry-on-harness-death
to `test_gl_repo_target_676.py`'s copy of this probe after a sixth
occurrence; a seventh (2026-09-04, `firefox` in the child's `%TEMP%`) landed
on `test_repo_target_673.py` instead -- the one file that never got the
retry, because #2235's title scoped itself to "the #676 test" alone.

This is a static parity check, not a live rerun of the race (unreproducible
on this machine): read both files' source and assert the shared
`nested_pytest_verdict.run_with_harness_retry` name appears in the body of
each one's own `test_the_env_var_main_sets_does_not_survive_into_the_next_test`
function. It fails on the pre-fix source of `test_repo_target_673.py`
(no such call exists there) and is a no-op on `test_gl_repo_target_676.py`,
which already has it.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "tests"
PROBE_TEST = "test_the_env_var_main_sets_does_not_survive_into_the_next_test"


def _function_source(path: Path, func_name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
    raise AssertionError(f"{func_name} not found in {path}")


def test_github_twin_retries_a_harness_death_like_the_gitlab_twin_does():
    gl_source = _function_source(TESTS / "test_gl_repo_target_676.py", PROBE_TEST)
    gh_source = _function_source(TESTS / "test_repo_target_673.py", PROBE_TEST)

    assert "run_with_harness_retry" in gl_source, (
        "the GitLab twin lost its own #2235 retry -- this test's baseline moved"
    )
    assert "run_with_harness_retry" in gh_source, (
        "the GitHub twin (#673) still asserts on a single spawn attempt -- "
        "a harness death (exit 2/3/4/5, no verdict about the product) fails "
        "the whole suite leg instead of retrying once, exactly the gap the "
        "2026-09-04 'firefox' occurrence exposed"
    )
