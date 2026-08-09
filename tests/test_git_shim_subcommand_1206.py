"""#1206 -- a PATH shim that stops shimming without saying so.

The fake `git` executables in this suite decide whether they are standing in
for the call under test by comparing `$1` against a subcommand name. git takes
its global flags *before* the subcommand, so `git -C <path> status` and
`git --literal-pathspecs status` both put `status` in `$2`: the comparison
fails, the `exec` fallthrough runs the real binary, and the fake is silently
not in effect.

It surfaced loudly once, by luck. The tests it broke assert a *refusal*, so a
real git succeeding contradicted them visibly. A shim standing in for a
**passing** expectation fails the other way round -- real git returns the
answer the fake would have returned, the test still passes, and it is asserting
nothing about the path it was written for. That is indistinguishable from a
working test, forever.

So the shim has to describe "a git invoked with this subcommand", not "a git
whose first word is this subcommand" -- and the last test here is the class
gate, because this one was found by tripping over it rather than by looking.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

import test_git_timeout_disclosure_650 as t650
import test_status_swallowed_705 as t705

TESTS = Path(__file__).resolve().parent

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX /bin/sh shim")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    for args in (["init", "-b", "master"],
                 ["config", "user.email", "t@test.com"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)
    (repo / "tracked.txt").write_text("original" + os.linesep, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"],
                   check=True, capture_output=True)
    return repo


def _run(bindir: Path, *args: str, timeout: float = 30):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env=dict(os.environ, PATH=str(bindir)))


@pytest.mark.parametrize("prefix", [
    ["--literal-pathspecs"],
    ["--no-optional-locks"],
    ["-c", "core.quotepath=false"],
])
def test_a_global_flag_before_the_subcommand_still_reaches_the_failing_shim(
    tmp_path: Path, prefix
) -> None:
    """The refusal has to survive a flag git itself accepts in that position."""
    d = t705._bindir(tmp_path)
    t705._failing_git_shim(d, "status", 128, "fatal: unable to read index file")
    r = _run(d, *prefix, "status", "--porcelain")
    assert r.returncode == 128, (
        "the shim did not intercept `git " + " ".join(prefix) + " status`; real "
        "git answered instead, so a test built on this fake tests nothing: "
        + repr((r.returncode, r.stdout, r.stderr))
    )
    assert "unable to read index file" in r.stderr


def test_the_shim_is_reached_through_the_dash_C_form_the_suite_actually_uses(
    tmp_path: Path
) -> None:
    """`git -C <path> <sub>` is how this repo's own helpers spell it."""
    repo = _repo(tmp_path)
    d = t705._bindir(tmp_path)
    t705._failing_git_shim(d, "status", 128, "fatal: unable to read index file")
    r = _run(d, "-C", str(repo), "status", "--porcelain")
    assert r.returncode == 128, repr((r.returncode, r.stdout, r.stderr))


def test_the_stalling_shim_also_matches_past_a_global_flag(tmp_path: Path) -> None:
    """Same defect, the other builder: it must still hang, not fall through."""
    d = t705._bindir(tmp_path)
    t705._stalling_git_shim(d, "status")
    with pytest.raises(subprocess.TimeoutExpired):
        _run(d, "--literal-pathspecs", "status", "--porcelain", timeout=3)


def test_the_timeout_suite_shim_matches_past_a_global_flag(tmp_path: Path) -> None:
    """`test_git_timeout_disclosure_650` builds the same shape separately."""
    bindir = Path(t650._failing_git_path(tmp_path, "diff"))
    r = _run(bindir, "--literal-pathspecs", "diff", "--name-only")
    assert r.returncode == 1, repr((r.returncode, r.stdout, r.stderr))
    assert "fatal: shim" in r.stderr


def test_a_subcommand_named_only_as_an_argument_is_not_intercepted(
    tmp_path: Path
) -> None:
    """The fix must not become "match anywhere in argv".

    `git status -- stash` is not a stash call, and a shim that answered it as
    one would break the tests it is supposed to hold up -- trading a shim that
    silently stops firing for one that silently fires too often.
    """
    repo = _repo(tmp_path)
    d = t705._bindir(tmp_path)
    t705._failing_git_shim(d, "stash", 128, "fatal: unable to read index file")
    r = _run(d, "-C", str(repo), "status", "--porcelain", "--", "stash")
    assert r.returncode == 0, repr((r.returncode, r.stdout, r.stderr))
    assert "unable to read index file" not in r.stderr


# ---------------------------------------------------------------------------
# The class gate
# ---------------------------------------------------------------------------

#: `$1` really is the first argument for these, and the reason is stated rather
#: than assumed. `test_pre_push_interpreter_572` shims a *python* interpreter and
#: matches `-c`, which is not a git subcommand and carries no global-flag
#: prefix; `test_watch_mine_defaults` stubs `supertool`, whose op is always the
#: first and only argument.
_FIRST_ARG_IS_HONEST = {
    "test_pre_push_interpreter_572.py",
    "test_watch_mine_defaults.py",
}

_DOLLAR_ONE = re.compile('"[$]1"')


def _sh_literals_matching_dollar_one():
    """Every string constant in the suite that tests `"$1"` inside a shim."""
    hits = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name in _FIRST_ARG_IS_HONEST or path.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken fixture module
            continue
        for node in ast.walk(tree):
            text = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(
                    v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            if text and _DOLLAR_ONE.search(text):
                # An f-string is walked as the JoinedStr and again as each of
                # its Constant parts, so the same shim would otherwise be named
                # twice and the list would read as a longer class than it is.
                hit = (path.name, node.lineno, text.strip()[:70])
                if not any(h[:2] == hit[:2] for h in hits):
                    hits.append(hit)
    return hits


def test_no_shim_in_this_suite_decides_a_git_subcommand_from_dollar_one() -> None:
    """A shim keyed on `$1` un-shims itself the moment a global flag appears.

    Nothing else in the suite reports that: the fallthrough runs real git, which
    answers, and only a test asserting a refusal notices. Route shims through
    `_gitshim.dispatch_on_subcommand`, which skips git's global flags and reads
    the first argument that is actually a subcommand.
    """
    hits = _sh_literals_matching_dollar_one()
    assert hits == [], (
        "these shims key off the first argument, so any git global flag ahead "
        "of the subcommand silently disables them:" + os.linesep
        + os.linesep.join("  {0}:{1} {2!r}".format(*h) for h in hits)
    )
