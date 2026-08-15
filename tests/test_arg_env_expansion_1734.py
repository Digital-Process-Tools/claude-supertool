"""#1734 — `_expand_env` reached the caller's ARGUMENT, not just the template.

`_expand_env`'s docstring names its subject: a project's own ``cmd`` template,
so a user can write ``$MY_TOOL_HOME`` in it and have it resolved without a
shell. The expansion ran on the *assembled* command, though — after
``{args}`` / ``{arg}`` / ``{argjoin}`` / ``{file}`` had already been
interpolated — so the expander could not tell template text from caller data
and substituted both.

Measured before the fix, from a clean scratch repository::

    $ python3 supertool.py 'git-commit:::chore: about $HOME:::f.txt'
    $ git log -1 --format=%s
    chore: about /Users/floriandavid

The op string was single-quoted, so this was not the shell — and an
*undefined* variable was left literal, which is `_expand_env`'s signature and
not a shell's (a shell empties it). `discloses`: the value of an environment
variable was written into a commit object because its NAME appeared in an
argument, and a commit gets pushed.

**Every "must survive verbatim" case here is paired with a "must still
expand" case in the same fixture.** A fix that simply deleted the expansion
would pass the first half of this file and silently break the documented
feature; the controls are what makes the first half mean anything.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent

#: Set in the child environment for every run below. The name is what a
#: caller types in an argument; the value is what must never appear.
_SECRET_NAME = "SUPERTOOL_1734_FAKE_TOKEN"
_SECRET_VALUE = "ghp_notarealsecret1734"

#: Reported by the probe so a template-side expansion has something to prove.
_TPL_NAME = "SUPERTOOL_1734_TEMPLATE_VAR"
_TPL_VALUE = "template-side-expansion-worked"

#: Echoes its own argv as JSON. This is the whole point: the assertion is
#: about the bytes the child process actually received, not about a helper's
#: return value.
_PROBE = "import sys, json; print(json.dumps(sys.argv[1:]))"


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)
    env[_SECRET_NAME] = _SECRET_VALUE
    env[_TPL_NAME] = _TPL_VALUE
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    return env


def _run(cwd: Path, *ops: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_ROOT / "supertool.py"), *ops],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd), env=_child_env(), timeout=120)


def _project(tmp_path: Path, cmd: str, **extra: object) -> Path:
    entry: Dict[str, object] = {
        "safety": "read-only", "cmd": cmd, "lines": 80}
    entry.update(extra)
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": {"probe": entry}}), encoding="utf-8")
    return tmp_path


def _argv(proc: subprocess.CompletedProcess) -> List[str]:
    """The argv list the probe printed, or fail loudly.

    A test that cannot see the child's argv must FAIL, never pass quietly:
    every verbatim-survival assertion below is a negative one, and a negative
    assertion over an empty harness passes for the wrong reason.
    """
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
    raise AssertionError(
        "probe never reported an argv line — the harness saw nothing, which "
        "is not the same as seeing no substitution.\n"
        "STDOUT:\n" + proc.stdout + "\nSTDERR:\n" + proc.stderr)


# --------------------------------------------------------------------------
# The defect: caller data must reach the op byte-exact.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "chore: about $HOME and $CLAUDE_PLUGIN_ROOT",
    "token was $" + _SECRET_NAME,
    "braced ${" + _SECRET_NAME + "} form",
    "a bare $ and $1 and $_ stay put",
])
def test_a_dollar_var_in_an_argument_reaches_the_op_verbatim(tmp_path, text):
    """The argument the caller typed is the argument the op receives."""
    proj = _project(tmp_path, "{python} -c " + json.dumps(_PROBE) + " {args}")
    proc = _run(proj, "probe:::" + text)
    argv = _argv(proc)
    assert argv == [text], (
        "argument was rewritten between the caller and the op\n"
        "  typed:    " + text + "\n"
        "  received: " + repr(argv))


def test_the_secret_value_never_appears_anywhere_in_the_run(tmp_path):
    """Not in argv, not in the receipt. Naming a variable is not asking for it."""
    proj = _project(tmp_path, "{python} -c " + json.dumps(_PROBE) + " {args}")
    proc = _run(proj, "probe:::deploying with $" + _SECRET_NAME)
    _argv(proc)  # positive control: the harness demonstrably saw the child
    assert _SECRET_VALUE not in proc.stdout, proc.stdout
    assert _SECRET_VALUE not in proc.stderr, proc.stderr


@pytest.mark.parametrize("placeholder", ["{arg}", "{argjoin}"])
def test_the_other_argument_placeholders_are_shielded_too(tmp_path, placeholder):
    """`{args}` was the reported one; all three carry caller data."""
    proj = _project(
        tmp_path, "{python} -c " + json.dumps(_PROBE) + " " + placeholder)
    text = "subject naming $" + _SECRET_NAME
    proc = _run(proj, "probe:::" + text)
    assert _argv(proc) == [text], proc.stdout


# --------------------------------------------------------------------------
# The control: the documented feature must keep working.
# --------------------------------------------------------------------------

def test_a_dollar_var_in_the_template_still_expands_from_the_environment(tmp_path):
    """The feature `_expand_env`'s docstring actually claims.

    Without this, a fix that deletes the expansion outright passes every
    assertion above.
    """
    proj = _project(
        tmp_path,
        "{python} -c " + json.dumps(_PROBE) + " $" + _TPL_NAME + " {args}")
    proc = _run(proj, "probe:::literal-arg")
    assert _argv(proc) == [_TPL_VALUE, "literal-arg"], proc.stdout


def test_a_braced_dollar_var_in_the_template_still_expands(tmp_path):
    proj = _project(
        tmp_path,
        "{python} -c " + json.dumps(_PROBE) + " ${" + _TPL_NAME + "}")
    assert _argv(_run(proj, "probe")) == [_TPL_VALUE]


def test_an_env_prefix_on_the_template_still_sets_and_expands(tmp_path):
    """`KEY=VAL cmd` prefix, lifted by `_extract_env_prefix`, then expanded.

    This is the shipped-template shape (`MCP_*_WORKING_DIR=...`), and it is
    the ordering most at risk from any reshuffle of the render pipeline.
    """
    proj = _project(
        tmp_path,
        "SUPERTOOL_1734_PREFIXED=prefix-value {python} -c "
        + json.dumps(_PROBE) + " $SUPERTOOL_1734_PREFIXED")
    assert _argv(_run(proj, "probe")) == ["prefix-value"]


def test_an_env_prefix_value_holding_a_placeholder_still_resolves(tmp_path):
    """`KEY={dir} cmd` — the prefix VALUE is substituted, not left literal."""
    probe = ("import os, json; "
             "print(json.dumps([os.environ.get('SUPERTOOL_1734_DIR', '')]))")
    proj = _project(
        tmp_path,
        "SUPERTOOL_1734_DIR={dir} {python} -c " + json.dumps(probe),
        paths={"args": [1], "root": "cwd"})
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("x", encoding="utf-8")
    argv = _argv(_run(proj, "probe:sub/f.txt"))
    assert argv == ["sub"], argv


def test_an_undefined_var_in_the_template_is_still_left_literal(tmp_path):
    """Documented behaviour, and the half that distinguishes this from a shell."""
    proj = _project(
        tmp_path,
        "{python} -c " + json.dumps(_PROBE) + " $SUPERTOOL_1734_DEFINITELY_UNSET")
    assert _argv(_run(proj, "probe")) == ["$SUPERTOOL_1734_DEFINITELY_UNSET"]


def test_an_env_value_holding_a_placeholder_token_is_not_re_substituted(tmp_path):
    """Expansion output is never rescanned for `{args}`.

    An environment variable whose VALUE reads `{args}` must not become a
    second interpolation site — that would be the same defect pointed the
    other way.
    """
    env = _child_env()
    env["SUPERTOOL_1734_HOSTILE"] = "{args}"
    proj = _project(
        tmp_path,
        "{python} -c " + json.dumps(_PROBE) + " $SUPERTOOL_1734_HOSTILE")
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "supertool.py"), "probe:::injected"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(proj), env=env, timeout=120)
    argv = _argv(proc)
    assert argv == ["{args}"], argv
    assert "injected" not in argv


# --------------------------------------------------------------------------
# End to end, through the op the issue was filed against.
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)


@pytest.fixture()
def scratch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    (repo / ".supertool.json").write_text(
        json.dumps({"presets": ["git"]}), encoding="utf-8")
    return repo


def test_git_commit_writes_the_subject_the_caller_typed(scratch_repo):
    """The reported reproduction, asserted on the commit object itself."""
    subject = "chore: about $HOME and $CLAUDE_PLUGIN_ROOT"
    proc = _run(scratch_repo, "git-commit:::" + subject + ":::f.txt")
    log = _git(scratch_repo, "log", "-1", "--format=%s")
    if log.returncode != 0 or not log.stdout.strip():
        raise AssertionError(
            "no commit was written, so this test proves nothing about "
            "substitution\nSTDOUT:\n" + proc.stdout
            + "\nSTDERR:\n" + proc.stderr + "\nGIT:\n" + log.stderr)
    assert log.stdout.strip() == subject, (
        "commit subject was rewritten\n  typed:     " + subject
        + "\n  committed: " + log.stdout.strip())


def test_git_commit_does_not_write_a_secrets_value_into_the_object(scratch_repo):
    subject = "chore: token was $" + _SECRET_NAME
    _run(scratch_repo, "git-commit:::" + subject + ":::f.txt")
    log = _git(scratch_repo, "log", "-1", "--format=%s")
    assert log.stdout.strip(), "no commit written — assertion would be vacuous"
    assert _SECRET_VALUE not in log.stdout, log.stdout
    assert log.stdout.strip() == subject, log.stdout


# --------------------------------------------------------------------------
# The other four call sites: validator / formatter / resolve take `{file}`,
# which is a caller-named path.
# --------------------------------------------------------------------------

_RECORDER = (
    "import sys, json, pathlib; "
    "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:])); "
    "print(json.dumps({'ok': True}))")


def _dollar_named_file(tmp_path: Path) -> Optional[Path]:
    """A file whose NAME contains `$SECRET`, or None where that is unwritable.

    The variable name must be TERMINATED by the extension's dot. `_expand_env`
    matches `[A-Za-z_][A-Za-z0-9_]*` greedily, so `has_$SECRET_in_name.txt`
    names the variable `SECRET_in_name`, which is undefined and therefore left
    literal — the assertion then passes without the code doing anything. That
    is the exact defect class this repository keeps filing, met while writing
    the test for it.
    """
    target = tmp_path / ("has_$" + _SECRET_NAME + ".txt")
    try:
        target.write_text("x\n", encoding="utf-8")
    except OSError:
        return None
    return target if target.exists() else None


def test_a_dollar_in_a_target_path_is_not_expanded_by_a_formatter(tmp_path):
    """`{file}` at `_formatter_run_one` is caller-named data, same as `{args}`."""
    import supertool

    target = _dollar_named_file(tmp_path)
    if target is None:
        pytest.skip("filesystem will not hold a '$' in a filename here")

    record = tmp_path / "seen.json"
    spec = {"cmd": "{python} -c " + json.dumps(_RECORDER) + " "
                   + json.dumps(str(record)) + " {file}"}
    os.environ[_SECRET_NAME] = _SECRET_VALUE
    try:
        supertool._formatter_run_one("probe", spec, str(target))
    finally:
        os.environ.pop(_SECRET_NAME, None)

    assert record.exists(), "formatter never ran — nothing was observed"
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen == [str(target)], seen
    assert _SECRET_VALUE not in seen[0], seen


def test_a_formatter_template_can_still_use_a_dollar_var(tmp_path):
    """Control for the case above."""
    import supertool

    target = tmp_path / "plain.txt"
    target.write_text("x\n", encoding="utf-8")
    record = tmp_path / "seen2.json"
    spec = {
        "cmd": "{python} -c " + json.dumps(_RECORDER) + " "
               + json.dumps(str(record)) + " $" + _TPL_NAME,
        "env": {_TPL_NAME: _TPL_VALUE},
    }
    supertool._formatter_run_one("probe", spec, str(target))
    assert record.exists(), "formatter never ran — nothing was observed"
    assert json.loads(record.read_text(encoding="utf-8")) == [_TPL_VALUE]
