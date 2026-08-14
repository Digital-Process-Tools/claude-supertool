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
whose first word is this subcommand" -- and the suite-wide scan below is the
class gate, because this one was found by tripping over it rather than by
looking. The tests after that gate hold the gate's *own* rule against the
three shims it was written for, since a scan that gets narrowed and not
re-proved is a gate that may have stopped catching its class without saying
so.
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

#: Only the tests that actually *run* a `/bin/sh` shim need this. It used to be
#: a module-level `pytestmark`, which also skipped the class gate and every
#: rule test -- all of them pure AST and regex work with no shell in sight. A
#: platform mark that reaches past what it is about reports coverage the leg
#: does not have, which is the same defect the gate below exists for.
posix_shim = pytest.mark.skipif(os.name == "nt", reason="POSIX /bin/sh shim")


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


@posix_shim
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


@posix_shim
def test_the_shim_is_reached_through_the_dash_C_form_the_suite_actually_uses(
    tmp_path: Path
) -> None:
    """`git -C <path> <sub>` is how this repo's own helpers spell it."""
    repo = _repo(tmp_path)
    d = t705._bindir(tmp_path)
    t705._failing_git_shim(d, "status", 128, "fatal: unable to read index file")
    r = _run(d, "-C", str(repo), "status", "--porcelain")
    assert r.returncode == 128, repr((r.returncode, r.stdout, r.stderr))


@posix_shim
def test_the_stalling_shim_also_matches_past_a_global_flag(tmp_path: Path) -> None:
    """Same defect, the other builder: it must still hang, not fall through."""
    d = t705._bindir(tmp_path)
    t705._stalling_git_shim(d, "status")
    with pytest.raises(subprocess.TimeoutExpired):
        _run(d, "--literal-pathspecs", "status", "--porcelain", timeout=3)


@posix_shim
def test_the_timeout_suite_shim_matches_past_a_global_flag(tmp_path: Path) -> None:
    """`test_git_timeout_disclosure_650` builds the same shape separately."""
    bindir = Path(t650._failing_git_path(tmp_path, "diff"))
    r = _run(bindir, "--literal-pathspecs", "diff", "--name-only")
    assert r.returncode == 1, repr((r.returncode, r.stdout, r.stderr))
    assert "fatal: shim" in r.stderr


@posix_shim
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

#: `$1` in any of the spellings shell gives it, with the `x` of the defensive
#: `[ x$1 = xstatus ]` idiom allowed in front. The quoting is the shim author's
#: choice and says nothing about whether the word slides to `$2`, so reading it
#: is how this gate returned a zero for two thirds of its own class (#1419).
_D1 = r"[A-Za-z_]*[$](?:1|\{1\})(?![\w{])"

#: The `/bin/sh` constructs that decide on a word. `awk '{print $1}'` and a
#: `printf ... "$1"` reach none of them: they use `$1`, they do not test it.
_TEST_OPENS = r"(?:^|[\s;&|(])(?:\[\[?|test)\s+"

_CASE_ON_DOLLAR_ONE = re.compile(r"(?:^|[\s;&|(])case\s+" + _D1)

#: What `$1` is compared against -- on either side of the operator, because
#: `[ status = $1 ]` is the same question asked backwards.
_COMPARED_WITH_DOLLAR_ONE = re.compile(
    _TEST_OPENS + r"([A-Za-z_]*)[$](?:1|\{1\})(?![\w{])\s*(?:==|!=|=)\s*(\S+)"
)
_COMPARED_WITH_DOLLAR_ONE_REVERSED = re.compile(
    _TEST_OPENS + r"(\S+)\s*(?:==|!=|=)\s*" + r"([A-Za-z_]*)[$](?:1|\{1\})(?![\w{])"
)


def _unquoted(text: str) -> str:
    """Drop `"` and `'`, which is exactly what this gate must not read.

    `[ "$1" = "status" ]`, `[ '$1' = 'status' ]` and `[ $1 = status ]` are one
    defect written three ways. Normalising first means the grammar below is
    stated once instead of once per quoting style -- and a fourth style
    somebody invents tomorrow arrives already handled.
    """
    return text.replace('"', "").replace("'", "")


def _operand(prefix: str, operand: str) -> str:
    """One word `$1` was compared against, with the shell noise taken off."""
    operand = operand.rstrip("];")
    if prefix and operand.startswith(prefix):
        # `[ x$1 = xstatus ]` guards an unset `$1`; the `x` is on both sides.
        operand = operand[len(prefix):]
    return operand


def _operands_compared_with_dollar_one(text: str) -> list:
    """Unquoted text in, every word `$1` is tested against out."""
    return (
        [_operand(p, o) for p, o in _COMPARED_WITH_DOLLAR_ONE.findall(text)]
        + [_operand(p, o) for o, p in _COMPARED_WITH_DOLLAR_ONE_REVERSED.findall(text)]
    )


def cannot_be_a_git_subcommand_test(text: str) -> bool:
    """True when every operand compared with `$1` is an option, not a subcommand.

    **git's own grammar decides this, not the name of the file.** Global flags
    come *before* the subcommand and a subcommand never begins with `-`, so a
    shim asking whether `$1` is `-c` or `-3` is asking about an option that
    genuinely is the first argument -- there is no position for it to slide to.
    A shim asking whether `$1` is `status` is asking the question #1206 is
    about, whichever binary it stands in for.

    Deliberately conservative in one direction: a literal that tests `$1` with
    no readable comparison (a bare `case $1 in`) yields nothing to judge and is
    **not** exempted, because the operands then live in patterns this cannot
    read. One flag-shaped operand among several is not enough either -- all of
    them have to be options, or the literal is still deciding on a word that
    could be a subcommand.

    A `$1` that is never tested at all -- `awk '{print $1}'`, `rm $1`, a
    `printf ... "$1"` into a log -- is not this class and is not reported.
    It decides no subcommand, so no global flag can silently unshim it.
    """
    operands = _operands_compared_with_dollar_one(_unquoted(text))
    return bool(operands) and all(o.startswith("-") for o in operands)


#: A shell loop that consumes its arguments. `$1` inside one is "the argument
#: currently under the cursor", not "the first argument" -- the loop has already
#: walked past every global flag by the time the word is read, so there is no
#: position for it to slide to. Non-greedy to the *first* `done`, so a second
#: loop later in the same literal is a second span rather than one long one.
_LOOP_BODY = re.compile(r"(?:^|[\s;&|(])(?:while|until|for)\b.*?\bdone\b", re.S)

#: `shift` run as a command, not `shift` occurring as a word. The shell only
#: consumes an argument when the word sits where a command can start: at the
#: beginning of the literal, after a separator, or after a keyword that opens a
#: body. `echo "cannot shift here"` puts the letters in an operand and shifts
#: nothing -- which is #1661, where a substring test read that as a dispatcher
#: and exempted every `$1` decision in the loop. The negative lookahead because
#: `shift=1` assigns to a variable that happens to be called `shift`, and the
#: word boundary because `shiftcount` is not the builtin either.
_SHIFT_COMMAND = re.compile(
    r"(?:^|[\n;&|(){}`]|\b(?:do|then|else|elif)\b)[ \t]*shift\b(?!=)"
)

#: The two characters `_outside_quotes` opens a quoted span on.
_QUOTES = '"' + "'"


def _outside_quotes(text: str) -> str:
    """TEXT with every quoted span and comment blanked, offsets preserved.

    The counterpart to `_unquoted`, and needed because that one deletes the
    quote characters rather than what they enclose: `echo "run: shift; go"`
    comes out of it as `echo run: shift; go`, where the `;` the string was
    carrying now reads as a separator and the word after it as a command. Same
    length in as out, so an offset here still indexes into TEXT.

    Not a shell parser. It tracks one level of quoting and a `#` comment, which
    is what the shim literals in this suite are made of. Anything it cannot
    read stays visible rather than being blanked, so the exemption below fails
    *closed* -- an unreadable literal is reported, never exempted.
    """
    out = list(text)
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is None:
            if ch in _QUOTES:
                quote = ch
                out[i] = " "
            elif ch == "#" and (i == 0 or text[i - 1] in " ;&|(" + chr(9) + chr(10)):
                while i < len(text) and text[i] != chr(10):
                    out[i] = " "
                    i += 1
                continue
        else:
            out[i] = " "
            if ch == quote:
                quote = None
            elif ch == chr(92) and quote == '"' and i + 1 < len(text):
                out[i + 1] = " "
                i += 1
        i += 1
    return "".join(out)


#: Every construct `_looks_like_the_class` counts as testing `$1`. Kept as one
#: tuple so the span check below cannot drift from the detection above it.
_TESTS_DOLLAR_ONE = (
    _CASE_ON_DOLLAR_ONE,
    _COMPARED_WITH_DOLLAR_ONE,
    _COMPARED_WITH_DOLLAR_ONE_REVERSED,
)


def _shifts_through_argv(text: str) -> bool:
    """True when every `$1` test in TEXT sits inside a loop that shifts.

    This is the #1598 exemption, and it is a statement about git's grammar in
    the same way #1412's option rule is -- not about which file the code is in.
    `tests/_gitshim.py`'s dispatcher is the one correct implementation of the
    pattern this gate hunts, and the widened population makes it the gate's
    single hit. Naming the file would re-create the allowlist #1412 deleted,
    and would bless every shim that file ever acquires; naming the *shape*
    exempts the construct wherever anybody writes it.

    Takes the literal with its quotes still on, because the quoting is the only
    thing that separates a `shift` from the word `shift` (#1661).

    Deliberately narrow in four ways, each pinned by a test below. A loop with
    no `shift` consumes nothing, so `$1` is still the first word. A loop that
    merely *mentions* the word -- in an `echo`, a comment, a variable name --
    consumes nothing either, and believing the letters is how this exemption
    silenced the whole gate the morning it shipped. A `$1` test outside the
    loop is not covered by a dispatcher that happens to sit above it. And an
    empty set of tests is not "all of them inside a loop" -- there is nothing
    here to exempt, and `_looks_like_the_class` has already decided that case.

    Every arm returns False when it cannot establish the exemption, which is
    the direction that costs a false report rather than a silent gate: an
    exemption that cannot be established is not an exemption.
    """
    # Two readings of the same literal, and a `shift` has to survive both. The
    # unquoted one carries the offsets the span check below is stated in; the
    # blanked one is the only one that knows a `;` inside an `echo` argument is
    # not a separator, and it cannot supply offsets because `_unquoted` deletes
    # characters. So it is used as a whole-literal veto rather than per span.
    if not _SHIFT_COMMAND.search(_outside_quotes(text)):
        return False
    text = _unquoted(text)
    spans = [m.span() for m in _LOOP_BODY.finditer(text)
             if _SHIFT_COMMAND.search(m.group(0))]
    if not spans:
        return False
    positions = [m.start() for rx in _TESTS_DOLLAR_ONE for m in rx.finditer(text)]
    return bool(positions) and all(
        any(start <= p < end for start, end in spans) for p in positions
    )


def _looks_like_the_class(text: str) -> bool:
    """The gate's entire rule for one string constant: tested, and not exempt.

    Named so it can be held against literals directly. A gate whose only entry
    point walks the tree can be tested for what it finds on disk today and
    never for what it would find tomorrow -- and the shims it has caught are
    all written in the style it reads, so every historical hit corroborates it.
    """
    unquoted = _unquoted(text)
    tested = (_CASE_ON_DOLLAR_ONE.search(unquoted)
              or _operands_compared_with_dollar_one(unquoted))
    if not tested:
        return False
    # Both exemptions are handed the literal as written. They unquote what they
    # need to: dropping the quotes before either of them can look is what let a
    # quoted word stand in for a builtin (#1661).
    return not (
        cannot_be_a_git_subcommand_test(text) or _shifts_through_argv(text)
    )


def _modules_scanned(root: Path = TESTS) -> list:
    """Every Python module under `tests/`, not only the `test_*.py` ones.

    Until #1598 this was `glob("test_*.py")`, and the gate's zero was read as
    "no shim in the suite decides a subcommand from `$1`" when it meant "no
    shim in the files whose names begin with `test_`". `tests/_gitshim.py`,
    `tests/_git_decline.py`, `tests/conftest.py` and everything under
    `tests/fixtures/` were never opened -- and a *shared* shim is exactly the
    thing that lives in a helper module. Recursive, because `tests/fixtures/`
    holds real modules and a directory is not a boundary the defect respects.
    """
    return [p for p in sorted(root.rglob("*.py"))
            if "__pycache__" not in p.parts
            and p.name not in _FIRST_ARG_IS_HONEST
            and p.name != Path(__file__).name]


def _sh_literals_matching_dollar_one(root: Path = TESTS):
    """Every string constant in the suite that tests `$1` inside a shim."""
    hits = []
    for path in _modules_scanned(root):
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
            if text and _looks_like_the_class(text):
                name = path.relative_to(root).as_posix()
                # An f-string is walked as the JoinedStr and again as each of
                # its Constant parts, so the same shim would otherwise be named
                # twice and the list would read as a longer class than it is.
                hit = (name, node.lineno, text.strip()[:70])
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


# ---------------------------------------------------------------------------
# The quoting a shim happens to use is not the question this gate asks.
# ---------------------------------------------------------------------------

#: Every one of these is the #1206 defect exactly -- a word at `$1` that slides
#: to `$2` the moment a git global flag appears in front of it. None of them is
#: written the way the three original shims were.
_THE_SAME_DEFECT_SPELLED_OTHERWISE = (
    'if [ $1 = status ]; then exit 128; fi',
    "if [ '$1' = 'status' ]; then exit 1; fi",
    'if [ "$1" = status ]; then exit 128; fi',
    "if [ '$1' = stash ]; then sleep 30; fi",
    'if [ x$1 = xstatus ]; then exit 128; fi',
    'if [ ${1} = status ]; then exit 128; fi',
    'case $1 in status) exit 128 ;; esac',
    "case '$1' in diff) exit 1 ;; esac",
    'if [[ $1 == status ]]; then exit 128; fi',
    'if test $1 = status; then exit 128; fi',
)


@pytest.mark.parametrize("shim", _THE_SAME_DEFECT_SPELLED_OTHERWISE)
def test_the_gate_reads_the_defect_and_not_the_quoting_style(shim: str) -> None:
    """A gate keyed on one spelling returns its zero while two walk past.

    Worse, it is self-corroborating: every shim it has ever caught was written
    in the style it reads, so each historical hit looks like evidence the
    pattern is right.
    """
    assert _looks_like_the_class(shim), shim


@pytest.mark.parametrize("shim", [
    'if [ $1 = -c ]; then exit 0; fi',
    "if [ '$1' != '-3' ]; then exit 9; fi",
    'if [ ${1} = --version ]; then exit 0; fi',
    'if test $1 = -c; then exit 0; fi',
])
def test_an_option_at_dollar_one_is_not_the_class_in_any_quoting(shim: str) -> None:
    """The grammar exemption has to travel with the widened match.

    #1412 derived it from git's own grammar -- global flags precede the
    subcommand, a subcommand never begins with `-` -- but only ever exercised
    it against double-quoted literals.
    """
    assert not _looks_like_the_class(shim), shim


@pytest.mark.parametrize("literal", [
    # awk's first field. Same three characters, a different language.
    "awk '{print $1}'",
    "git log --format=%H | awk '{print $1}' | head -3",
    # A shellcheck fixture: a shell positional, deciding nothing.
    "#!/bin/sh\nrm $1\n",
    "#!/bin/sh\ncd /tmp\nrm $1\n",
    # Logged, not tested.
    'printf "%s" "$1" >> "$LOG"',
])
def test_widening_the_match_does_not_flag_a_dollar_one_that_decides_nothing(
    literal: str,
) -> None:
    """The gate asks whether `$1` is *tested*, not whether it appears.

    Six literals in this suite contain a bare `$1` that is not a subcommand
    decision at all -- three `awk` field references and three shellcheck
    fixtures. A match widened to any occurrence turns all six red, and a gate
    that cries wolf gets its allowlist grown until it means nothing.
    """
    assert not _looks_like_the_class(literal), literal


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
    scanned = [p.name for p in _modules_scanned()]
    assert len(scanned) > 500, len(scanned)
    assert "test_status_swallowed_705.py" in scanned
    assert "test_hook_interpreter_windows_1401_1402.py" in scanned


# ---------------------------------------------------------------------------
# #1598 -- the population the gate scans, and the shape that exempts the one
# correct implementation of the pattern.
# ---------------------------------------------------------------------------


def test_the_scan_reads_every_python_module_in_the_suite_not_only_test_files(
) -> None:
    """`tests/*.py` is the claim; `tests/test_*.py` was the population (#1598).

    A shared shim belongs in a helper module -- that is what a helper module is
    for -- and every helper, `conftest.py` and every fixture module returned the
    gate's same clean zero because none of them was ever opened.
    """
    scanned = {p.relative_to(TESTS).as_posix() for p in _modules_scanned()}
    for name in ("_gitshim.py", "_git_decline.py", "conftest.py",
                 "fixtures/mock_mcp_server.py"):
        assert name in scanned, (name, len(scanned))


def test_a_shim_planted_in_a_helper_module_is_reported(tmp_path: Path) -> None:
    """The gate's zero has to mean "I looked there", not "I did not look".

    Held against a directory rather than the tree, because the tree is clean by
    construction: a scan that only ever runs over a passing population cannot
    tell a widened glob from a narrow one.
    """
    (tmp_path / "_helper_shim.py").write_text(
        """SHIM = 'if [ "$1" = "status" ]; then exit 128; fi'""" + os.linesep,
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        """SHIM = 'case "$1" in diff) exit 1 ;; esac'""" + os.linesep,
        encoding="utf-8",
    )
    found = {name for name, _line, _text in _sh_literals_matching_dollar_one(tmp_path)}
    assert found == {"_helper_shim.py", "conftest.py"}, found


def test_a_fixture_module_below_tests_is_reached_too(tmp_path: Path) -> None:
    """`tests/fixtures/` holds real modules, so `glob` is not deep enough."""
    sub = tmp_path / "fixtures" / "resolve"
    sub.mkdir(parents=True)
    (sub / "helper.py").write_text(
        """SHIM = 'if [ "$1" = "stash" ]; then sleep 30; fi'""" + os.linesep,
        encoding="utf-8",
    )
    found = {name for name, _line, _text in _sh_literals_matching_dollar_one(tmp_path)}
    assert found == {"fixtures/resolve/helper.py"}, found


def test_the_canonical_dispatcher_is_exempt_by_its_shape_not_by_its_filename(
) -> None:
    """`_gitshim`'s `while`/`shift` loop is the *correct* implementation.

    It is the one hit the widened population produces, and it must not be
    answered by putting `_gitshim.py` back on a filename list: #1412 removed
    that list on purpose, so that the judgement stops depending on where the
    code sits. `$1` inside a loop that shifts is "the argument being examined",
    not "the first argument" -- there is no position for it to slide to, which
    is the same grammar argument #1412 used for an option at `$1`.
    """
    import _gitshim

    assert not _looks_like_the_class(_gitshim._SUBCOMMAND_FUNCTION)
    assert not _looks_like_the_class(
        _gitshim.dispatch_on_subcommand("status", "exit 128", "/usr/bin/git")
    )
    assert "_gitshim.py" not in _FIRST_ARG_IS_HONEST


@pytest.mark.parametrize("shim", [
    # A dispatcher loop above does not bless a first-argument decision below it.
    'while [ $# -gt 0 ]; do case "$1" in -*) shift ;; *) break ;; esac; done'
    '\nif [ "$1" = "status" ]; then exit 128; fi\n',
    # A loop that never shifts is not walking argv; `$1` stays the first word.
    'while true; do if [ "$1" = "status" ]; then exit 128; fi; done',
    # `for` over a fixed list shifts nothing either.
    'for x in a b; do if [ "$1" = "diff" ]; then exit 1; fi; done',
])
def test_the_dispatcher_exemption_does_not_bless_a_first_argument_decision(
    shim: str,
) -> None:
    assert _looks_like_the_class(shim), shim


@pytest.mark.parametrize("shim", [
    # #1661: the letters in an operand. `echo` runs; `shift` is a word it
    # prints. This exact literal is the reproduction filed on the issue.
    'while true; do echo "cannot shift here"; '
    'if [ "$1" = "status" ]; then exit 128; fi; break; done',
    # The letters in a comment, which the shell never executes at all.
    'while true; do  # shift past the flags one day' + chr(10)
    + 'if [ "$1" = "status" ]; then exit 128; fi; break; done',
    # The letters inside a quoted string that also carries a separator. This
    # is the one that survives `_unquoted`, and it is why the exemption reads
    # the literal with its quotes on as well as with them off.
    'while true; do echo "run: shift; then look"; '
    'if [ "$1" = "status" ]; then exit 128; fi; break; done',
    # A variable whose name merely begins with the word.
    'while true; do shiftcount=1; '
    'if [ "$1" = "status" ]; then exit 128; fi; break; done',
    # An assignment to a variable *named* `shift` is not the builtin either.
    'while true; do shift=1; '
    'if [ "$1" = "status" ]; then exit 128; fi; break; done',
])
def test_the_word_shift_in_a_loop_is_not_a_shift(shim: str) -> None:
    """#1661: the exemption tested for the letters, so an `echo` silenced it.

    The gate's live population was zero at the time, in the file whose whole
    subject is a detector whose zero meant "I did not look" -- so a shim could
    opt itself out of the class by mentioning the word in a message.
    """
    assert _looks_like_the_class(shim), shim


@pytest.mark.parametrize("shim", [
    # `shift` after `;` -- the ordinary spelling.
    'while [ $# -gt 0 ]; do case "$1" in status) exit 128 ;; esac; shift; done',
    # `shift` opening a line, and `until` rather than `while`.
    'until [ $# -eq 0 ]; do' + chr(10)
    + '  if [ "$1" = "status" ]; then exit 128; fi' + chr(10)
    + '  shift' + chr(10)
    + 'done',
    # `shift` with a count, reached through `then` rather than a separator.
    'while [ $# -gt 0 ]; do if [ "$1" = "status" ]; then shift 2; fi; done',
])
def test_a_shift_that_actually_runs_still_exempts_the_loop(shim: str) -> None:
    """The #1598 exemption has to survive #1661's narrowing, or the fix for a
    silenced gate is a gate that reports its one correct implementation."""
    assert not _looks_like_the_class(shim), shim
