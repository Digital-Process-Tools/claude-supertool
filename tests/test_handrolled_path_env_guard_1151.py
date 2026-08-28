"""#1151 -- a fourth hand-rolled PATH-stripping env cannot reach a Windows leg.

`tests/_winenv.py::empty_path_env()` exists because on Windows
``env={"PATH": ""}`` is not "this environment with PATH replaced", it is "an
environment holding only PATH": SYSTEMROOT and WINDIR go with it, the child
interpreter cannot resolve its system DLLs, it writes nothing at all, and
``json.loads("")`` raises inside the adapter under test. Ten adapter suites now
import the helper. Three times (#658/#717, #725, #833, then #1140) a new one
did not, and every one of those was invisible until a CI log came back: on
POSIX none of `_winenv`'s kept names exist, so the two envs are byte-identical
here **by construction**. No amount of local green can catch this class. A
static check is the only thing that can.

A guard was written on the #1140 branch and deliberately cut rather than
shipped, because it was half-right, and the issue records exactly why -- which
is the specification this one is built to:

  * **Scope-aware.** Module-wide name resolution is not. An ``env`` bound in
    one function and spawned with in another produced a straight false positive
    at ``tests/test_git_env_scrub_692.py:176``. A name is resolved only in the
    function that spawns, and a name that is not bound there is UNRESOLVED --
    never a violation.
  * **Only spawns of ``sys.executable``.** Of the four other sites the first
    scanner flagged, two are genuine instances of the pattern but spawn
    bash/git. A bash child does not need SYSTEMROOT to start, and "fixing" a
    security test's deliberately-scrubbed environment is not obviously correct.
    Different risk, different decision, not this guard's business.
  * **Three states, never two.** ok / violation / unresolved. A scanner that
    folds "I could not tell" into "clean" is this repo's own most-filed defect
    class wearing a linter's clothes -- the zero that means "I did not look".

The unresolved set is asserted against a declared list rather than ignored, so
a new env expression this scanner cannot read forces a human decision instead
of silently widening the blind spot.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import _pathenv_scan as scan

TESTS = Path(__file__).resolve().parent

NL = chr(10)

#: Env expressions at a `sys.executable` spawn that the scanner cannot read.
#: Every entry is a site nobody is checking automatically, so every entry is a
#: hand verdict recorded here rather than a silent `continue` in the scanner --
#: adding one has to be argued for in review, and that is the whole design.
#:
#: Both current entries were read and cleared by hand on 2026-08-09:
#:
#:  * `test_encoding_seam.py:650` -- the spawn is inside a nested `_run_parent`
#:    closing over an `env` bound in the enclosing test body. Resolving it would
#:    mean reading an outer scope from an inner one, which is exactly the
#:    cross-scope resolution that false-positived #692 and got the first scanner
#:    cut. The binding is `dict(os.environ)` + `ASCII_LOCALE`, so it inherits.
#:  * `test_watch_sock_path_581.py:102` -- `env = transport.poller_env()`, a call
#:    into product code the scanner does not read. `presets/watch/transport.py:544`
#:    is `dict(os.environ)` plus one key, so it inherits.
#:
#: Third entry read and cleared by hand on 2026-08-12 (#1496):
#:
#:  * `test_git_worktrees_unpushed_1496.py:107` -- `env=_HERMETIC_ENV`, a
#:    module-level name the scanner does not follow. Its binding at that file's
#:    line 45 is `{**os.environ, ...}` plus five `GIT_*` keys that pin git's
#:    identity and config away from the developer's own, so PATH inherits. The
#:    same dict is passed to every `git` spawn in the file; only the
#:    `sys.executable` one reaches this guard.
DECLARED_UNRESOLVED = [
    "test_encoding_seam.py:650 [unresolved] "
    "env= expression could not be evaluated by this scanner",
    "test_git_worktrees_unpushed_1496.py:107 [unresolved] "
    "env= expression could not be evaluated by this scanner",
    "test_watch_sock_path_581.py:102 [unresolved] "
    "env= expression could not be evaluated by this scanner",
]


# ---------------------------------------------------------------------------
# The repo sweep -- what the guard is actually for
# ---------------------------------------------------------------------------

def test_no_test_hand_rolls_a_path_stripping_env_for_a_python_child() -> None:
    findings = scan.scan_tree(TESTS)
    violations = [f for f in findings if f.kind == "violation"]
    assert violations == [], (
        "a `sys.executable` spawn is handed a PATH-only env dict instead of "
        "`_winenv.empty_path_env()`. On Windows the child cannot start, writes "
        "nothing, and the test dies in `json.loads(\'\')` naming neither the tool "
        "nor the adapter (#1151):" + NL
        + NL.join("  " + f.describe() for f in violations))


def test_an_env_the_scanner_cannot_read_is_declared_rather_than_assumed_clean() -> None:
    """`I did not look` and `there is nothing there` are opposite facts."""
    findings = scan.scan_tree(TESTS)
    unresolved = sorted(f.describe() for f in findings if f.kind == "unresolved")
    assert unresolved == sorted(DECLARED_UNRESOLVED), (
        "the scanner reached a `sys.executable` spawn whose `env=` it cannot "
        "evaluate. That is not a pass. Either make the expression readable, or "
        "add it to DECLARED_UNRESOLVED with a reason:" + NL
        + NL.join("  " + u for u in unresolved))


# ---------------------------------------------------------------------------
# The scanner's own behaviour, on sources with a known right answer
# ---------------------------------------------------------------------------

def _kinds(source: str):
    return [f.kind for f in scan.scan_source(source, "sample.py")]


def test_it_flags_the_exact_shape_that_went_red_on_windows_in_1140() -> None:
    """The pre-#1140 body of `tests/test_html_check.py`, near enough.

    If this does not flag, the guard would not have caught the third occurrence
    of the class it exists for, and is worth nothing.
    """
    source = (
        "import subprocess, sys" + NL
        + "def test_missing_node_is_skipped_not_ok(tmp_path):" + NL
        + "    empty_bin = tmp_path / 'empty'" + NL
        + "    result = subprocess.run(" + NL
        + "        [sys.executable, str(ADAPTER), str(f)]," + NL
        + "        capture_output=True, text=True," + NL
        + "        env={'PATH': str(empty_bin)}," + NL
        + "    )" + NL
        + "    out = json.loads(result.stdout)" + NL
    )
    assert _kinds(source) == ["violation"]


def test_it_flags_the_dict_through_a_local_name_too() -> None:
    source = (
        "import subprocess, sys" + NL
        + "def test_x(tmp_path):" + NL
        + "    env = {'PATH': ''}" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass'], env=env)" + NL
    )
    assert _kinds(source) == ["violation"]


def test_the_helper_is_the_whole_point_and_is_never_flagged() -> None:
    source = (
        "import subprocess, sys" + NL
        + "from _winenv import empty_path_env" + NL
        + "def test_x():" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass'], env=empty_path_env())" + NL
    )
    assert _kinds(source) == []


def test_an_env_bound_in_another_function_is_unresolved_and_not_a_violation() -> None:
    """The #692 false positive, which is why the first scanner was cut.

    `env` is bound at module level (or in a sibling function) and spawned with
    here. Module-wide name resolution reads the wrong binding and reports a
    violation with total confidence. This scanner resolves in the spawning
    function only, so the honest answer is `unresolved`.
    """
    source = (
        "import subprocess, sys" + NL
        + "env = {'PATH': ''}" + NL
        + "def test_x():" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass'], env=env)" + NL
    )
    assert _kinds(source) == ["unresolved"]


def test_a_bash_or_git_child_is_not_this_guards_business() -> None:
    """Genuine instances of the shape, deliberately out of scope.

    `tests/test_security_hardening_150.py` spawns bash/git with a scrubbed
    environment on purpose. Those children do not need SYSTEMROOT to start, and
    a security test's scrubbed env is not obviously wrong. Flagging them would
    make the guard a nag, and a nag gets suppressed wholesale.
    """
    source = (
        "import subprocess, sys" + NL
        + "def test_x():" + NL
        + "    subprocess.run(['bash', '-c', 'true'], env={'PATH': ''})" + NL
        + "    subprocess.run(['git', 'status'], env={'PATH': ''})" + NL
    )
    assert _kinds(source) == []


def test_an_env_that_starts_from_os_environ_is_not_the_defect() -> None:
    """The failure mode is a dict that *replaces* the environment, not one that
    extends it -- a spread of `os.environ` keeps SYSTEMROOT by construction."""
    source = (
        "import os, subprocess, sys" + NL
        + "def test_x():" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass']," + NL
        + "                   env={**os.environ, 'PATH': ''})" + NL
    )
    assert _kinds(source) == []


def test_a_dict_that_keeps_the_windows_essentials_by_hand_is_accepted() -> None:
    """Rewriting the helper inline is not the defect either -- shipping a child
    that cannot start is. A dict carrying SYSTEMROOT is not that."""
    source = (
        "import os, subprocess, sys" + NL
        + "def test_x():" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass']," + NL
        + "                   env={'PATH': '', 'SYSTEMROOT': os.environ.get('SYSTEMROOT', '')})" + NL
    )
    assert _kinds(source) == []


def test_a_spawn_with_no_env_keyword_inherits_and_is_ignored() -> None:
    source = (
        "import subprocess, sys" + NL
        + "def test_x():" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass'])" + NL
    )
    assert _kinds(source) == []


def test_popen_and_check_output_are_the_same_spawn() -> None:
    source = (
        "import subprocess, sys" + NL
        + "def test_x():" + NL
        + "    subprocess.Popen([sys.executable, '-c', 'pass'], env={'PATH': ''})" + NL
        + "    subprocess.check_output([sys.executable, '-c', 'pass'], env={'PATH': ''})" + NL
    )
    assert _kinds(source) == ["violation", "violation"]


def test_a_call_expression_it_cannot_evaluate_is_unresolved_not_clean() -> None:
    source = (
        "import subprocess, sys" + NL
        + "def test_x():" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass'], env=_build_env())" + NL
    )
    assert _kinds(source) == ["unresolved"]


def test_a_finding_names_a_line_somebody_can_open() -> None:
    source = (
        "import subprocess, sys" + NL
        + "def test_x():" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass'], env={'PATH': ''})" + NL
    )
    finding = scan.scan_source(source, "sample.py")[0]
    assert finding.path == "sample.py"
    assert finding.lineno == 3
    assert "sample.py:3" in finding.describe()


def test_a_binding_inside_a_nested_function_does_not_leak_to_the_outer_scope() -> None:
    """The scope rule has to actually prune, not merely intend to.

    `ast.walk` flattens the whole subtree before the consumer sees a node, so a
    `continue` on a nested `FunctionDef` inside a walk loop prunes nothing --
    the nested body's assignments are yielded anyway. That silently restores
    the cross-scope resolution this scanner was written to remove, in the
    direction that produces a confident verdict from another function's
    binding. Found by review of this file's first version, where it was live.
    """
    source = (
        "import subprocess, sys" + NL
        + "def test_x():" + NL
        + "    def _inner():" + NL
        + "        env = {'PATH': ''}" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass'], env=env)" + NL
    )
    assert _kinds(source) == ["unresolved"]


def test_a_return_inside_a_nested_function_is_not_the_helpers_return() -> None:
    """Same flattening bug, one function along, and it points the other way.

    A helper that genuinely returns a safe env is called a violation because it
    happens to contain an unrelated nested function that returns a PATH-only
    dict -- a false positive of exactly the kind that got the first scanner cut.
    """
    source = (
        "import os, subprocess, sys" + NL
        + "def _clean_env():" + NL
        + "    def _inner():" + NL
        + "        return {'PATH': ''}" + NL
        + "    return {'PATH': '', 'SYSTEMROOT': os.environ.get('SYSTEMROOT', '')}" + NL
        + "def test_x():" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass'], env=_clean_env())" + NL
    )
    assert _kinds(source) == []


def test_an_argv_built_in_a_local_is_still_a_python_spawn() -> None:
    """`subprocess.run(argv, ...)` is the same spawn as the inline list.

    Reading only a literal first argument makes the whole call invisible -- not
    even unresolved, which would at least be counted. The defect would sit
    there reported as nothing at all.
    """
    source = (
        "import subprocess, sys" + NL
        + "def test_x():" + NL
        + "    argv = [sys.executable, '-c', 'pass']" + NL
        + "    subprocess.run(argv, env={'PATH': ''})" + NL
    )
    assert _kinds(source) == ["violation"]


def test_keeping_temp_but_not_systemroot_is_still_a_child_that_cannot_start() -> None:
    """`_winenv`'s docstring is explicit: SYSTEMROOT and WINDIR are what the
    interpreter needs to resolve its system DLLs. PATHEXT, TEMP, TMP and
    COMSPEC are kept for behavioural parity, and none of them will start a
    process. A dict carrying one of those and not the other two is the defect
    with a decoy in it."""
    source = (
        "import subprocess, sys" + NL
        + "def test_x():" + NL
        + "    subprocess.run([sys.executable, '-c', 'pass']," + NL
        + "                   env={'PATH': '', 'TEMP': 'x'})" + NL
    )
    assert _kinds(source) == ["violation"]


def test_a_file_it_cannot_parse_is_reported_and_never_skipped_silently() -> None:
    """A syntax error is `I could not look at this file`, which is not `clean`."""
    findings = scan.scan_source("def f(:" + NL, "broken.py")
    assert [f.kind for f in findings] == ["unreadable"]
