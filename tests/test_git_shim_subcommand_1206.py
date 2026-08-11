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

#: `$1` really is the first argument here and the scan cannot see why, so the
#: reason is stated rather than assumed. `test_watch_mine_defaults` stubs
#: `supertool`, whose op is always the first and only argument -- and an op name
#: is spelled exactly like a subcommand, so nothing about the text distinguishes
#: it. `test_pre_push_interpreter_572` used to be listed here and no longer needs
#: to be: it compares `$1` against `-c`, and the rule below now works that out
#: from git's grammar instead of from the file it lives in. That is the point of
#: the change -- a filename exemption blesses every shim the file will ever
#: contain, including a git one added tomorrow.
_FIRST_ARG_IS_HONEST = {
    "test_watch_mine_defaults.py",
}

_DOLLAR_ONE = re.compile('"[$]1"')

#: What `$1` is tested against, in the spellings a `/bin/sh` shim uses.
_COMPARED_WITH_DOLLAR_ONE = re.compile('"[$]1"' + r'\s*(?:==|!=|=)\s*"([^"]*)"')


def cannot_be_a_git_subcommand_test(text: str) -> bool:
    """True when every operand compared with `$1` is an option, not a subcommand.

    **git's own grammar decides this, not the name of the file.** Global flags
    come *before* the subcommand and a subcommand never begins with `-`, so a
    shim asking whether `$1` is `-c` or `-3` is asking about an option that
    genuinely is the first argument -- there is no position for it to slide to.
    A shim asking whether `$1` is `status` is asking the question #1206 is
    about, whichever binary it stands in for.

    Deliberately conservative in one direction: a literal with no comparison at
    all (a bare `case "$1" in`, a `printf ... "$1"`) yields nothing to judge and
    is **not** exempted, because the operands then live in patterns this cannot
    read. One flag-shaped operand among several is not enough either -- all of
    them have to be options, or the literal is still deciding on a word that
    could be a subcommand.
    """
    operands = _COMPARED_WITH_DOLLAR_ONE.findall(text)
    return bool(operands) and all(o.startswith("-") for o in operands)


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
            if (text and _DOLLAR_ONE.search(text)
                    and not cannot_be_a_git_subcommand_test(text)):
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

# ---------------------------------------------------------------------------
# The class gate's own rule, tested. A scan that was narrowed and not re-proved
# is a gate that may have stopped catching the thing it exists for, and nothing
# downstream would say so.
# ---------------------------------------------------------------------------

#: The three shims #1206 was filed about, as they were written then. They have
#: since been routed through `_gitshim.dispatch_on_subcommand`, so the suite no
#: longer contains them -- which is exactly why the rule has to be held against
#: them here rather than against whatever happens to be on disk.
_THE_ORIGINAL_DEFECT = (
    'if [ "$1" = "status" ]; then exit 128; fi',
    'if [ "$1" = "diff" ]; then exit 1; fi',
    'if [ "$1" = "stash" ]; then sleep 30; fi',
)


@pytest.mark.parametrize("shim", _THE_ORIGINAL_DEFECT)
def test_the_rule_still_catches_the_shims_it_was_written_for(shim: str) -> None:
    assert not cannot_be_a_git_subcommand_test(shim), shim


@pytest.mark.parametrize("shim", [
    'if [ "$1" = "-c" ]; then exit 0; fi',
    'if [ "$1" != "-3" ]; then exit 9; fi',
    'if [ "$1" == "--version" ]; then exit 0; fi',
    'if [ "$1" = "-c" ]; then exit 1; fi; if [ "$1" = "-m" ]; then exit 2; fi',
])
def test_an_option_at_dollar_one_is_not_the_class(shim: str) -> None:
    """git's global flags precede the subcommand, so an option really is first.

    There is no position for `-c` to slide to; the whole defect is a *word*
    moving to `$2` when a flag appears in front of it.
    """
    assert cannot_be_a_git_subcommand_test(shim), shim


@pytest.mark.parametrize("shim", [
    # One option among the operands is not enough: the other is still a word.
    'if [ "$1" = "-c" ]; then exit 1; fi; if [ "$1" = "stash" ]; then exit 2; fi',
    # Nothing to judge -- the operands are in patterns this cannot read.
    'case "$1" in status) exit 1 ;; esac',
    'printf "%s" "$1" >> "$LOG"',
    # A placeholder is not an option, and substitution could put anything there.
    'if [ "$1" = "{subcommand}" ]; then exit 1; fi',
])
def test_the_exemption_does_not_widen_past_what_it_can_read(shim: str) -> None:
    assert not cannot_be_a_git_subcommand_test(shim), shim


def test_the_narrowing_did_not_just_move_the_hole_into_the_allowlist() -> None:
    """The filename exemption shrank; it must not have been traded for silence.

    `test_pre_push_interpreter_572.py` came off the list because the rule now
    derives its exemption from git's grammar. If it were still producing a hit,
    the removal would have converted a stated exemption into a red test -- and
    if the *scan* had been widened to compensate, this file would be exempt for
    a reason nobody wrote down.
    """
    assert "test_pre_push_interpreter_572.py" not in _FIRST_ARG_IS_HONEST
    assert (TESTS / "test_pre_push_interpreter_572.py").is_file()
    named = {name for name, _line, _text in _sh_literals_matching_dollar_one()}
    assert "test_pre_push_interpreter_572.py" not in named


def test_the_scan_still_reads_every_test_module_it_did_before() -> None:
    """A rule can also be narrowed by quietly reading fewer files."""
    scanned = [p.name for p in sorted(TESTS.glob("test_*.py"))
               if p.name not in _FIRST_ARG_IS_HONEST
               and p.name != Path(__file__).name]
    assert len(scanned) > 500, len(scanned)
    assert "test_status_swallowed_705.py" in scanned
    assert "test_hook_interpreter_windows_1401_1402.py" in scanned
