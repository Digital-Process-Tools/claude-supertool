"""#1789 - a 503 from gh must not render as "cwd is not a GitHub repo".

Observed downstream while GitHub's GraphQL API was intermittently 503-ing:
`gh-pr-merge:1:squash|force` printed *cwd is not a GitHub repo and no repo
target was given* in a directory that was one, and the same op succeeded
minutes later. With `repo:owner/name` it took the other arm of the same
function - *could not be resolved by gh. Check the spelling and your access* -
for a repo whose spelling and access were both correct in that second.

Both sentences are **factual claims about the reader's environment**, rendered
from an answer that only ever said "not the slug".

**The count is one site out of thirteen, and that is the whole shape of the
fix.** Re-derived here rather than taken from the issue, which reasoned about
`no_repo_error` as though every caller reached it the same way. Twelve of the
thirteen match gh's own stderr against the not-a-repo family *before* calling
- `pr.py`, `issue.py`, `issues.py` (three calls), `run.py`, `job.py`,
`labels.py`, `prs.py`, `check.py` (two), `branch.py` - so they arrive having
measured, and their message is correct today. `pr_merge.py` is the thirteenth:
it reaches `no_repo_error` on *any* failure of its identity read and drops the
reason it is holding in `ident_err`. That is the observed defect, and it is
the only one.

So `no_repo_error` takes the reason rather than probing for one. A probe here
would run a *second* lookup a second later and could contradict a correct
measurement at those twelve sites with a fresher one - trading a right answer
for a newer one, which is the same class of defect one layer up. #1701's decision
not to widen `cwd_slug` stands untouched for the same reason it was made: its
caller `api_path_for_display` falls back to gh's own placeholders, which are a
correct command, so it still has nothing to say about a reason.

Every "did not answer" case below is paired with a real not-a-repo case in the
same fixture. A test that only ever feeds in a failure cannot tell a fixed
message from a message that stopped saying anything.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rt = _load("presets/_repo_target.py", "_repo_target")
_hint = _load("presets/_st_hint.py", "_st_hint_1789")

SLUG = "jbkkz/requivo"
EXAMPLE = "gh-pr-merge:1"

#: What gh says when the cwd genuinely is not a GitHub repository - the
#: positive control for every "did not answer" case below.
NOT_A_REPO = ("none of the git remotes configured for this repository point "
              "to a known GitHub host. Please use `gh repo set-default`")
NOT_A_GIT_REPO = "failed to run git: fatal: not a git repository"
#: What gh said on 2026-08-17, in a directory that WAS a GitHub repository.
BLIP = "GraphQL: Something went wrong while executing your query. (repository)"
#: The other ways of not being told. `pr_merge.py`'s `_gh_json` turns each of
#: these into exactly this string before `main()` ever sees it.
GH_MISSING = "gh not found - install from https://cli.github.com"
GH_TIMEOUT = "gh timed out"
#: What gh says for a target that does not exist or is not visible.
NO_SUCH_TARGET = ("GraphQL: Could not resolve to a Repository with the name "
                  "'jbkkz/requivo'. (repository)")


# ===========================================================================
# classify_detail: `absent` is a measurement, `unknown` is the lack of one
# ===========================================================================

def test_gh_saying_there_is_no_repo_here_is_absent() -> None:
    for detail in (NOT_A_REPO, NOT_A_GIT_REPO,
                   "no git remotes found",
                   "could not determine base repository"):
        assert rt.classify_detail(detail) == rt.ABSENT, detail


def test_gh_not_answering_is_unknown_however_it_failed() -> None:
    """A 503, a missing binary, a hang and an unparseable answer are four ways
    of *not being told*, and none of them is evidence about the directory.
    Feeding only a non-zero exit would leave three of the four untested."""
    for detail in (BLIP, GH_MISSING, GH_TIMEOUT,
                   "HTTP 503: Service Unavailable",
                   "error: not logged in to any hosts",
                   "gh returned invalid JSON"):
        assert rt.classify_detail(detail) == rt.UNKNOWN, detail


def test_a_target_is_read_against_the_targets_own_vocabulary() -> None:
    """gh complains about a *directory* one way and about a *named repository*
    another. Read against the cwd markers, every misspelled target would come
    out `unknown` and the spelling hint would become unreachable."""
    assert rt.classify_detail(NO_SUCH_TARGET, SLUG) == rt.ABSENT
    assert rt.classify_detail("HTTP 404: Not Found", SLUG) == rt.ABSENT
    assert rt.classify_detail(BLIP, SLUG) == rt.UNKNOWN
    # ... and the cwd's own phrases are not the target's.
    assert rt.classify_detail(NOT_A_REPO, SLUG) == rt.UNKNOWN


# ===========================================================================
# no_repo_error: the third arm, and the two that must not move
# ===========================================================================

def test_a_blip_is_not_reported_as_not_a_github_repo(monkeypatch) -> None:
    """The reproduction. gh failed for a reason that says nothing about the
    cwd, so the message must not assert that the cwd is not a GitHub repo."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    msg = rt.no_repo_error(EXAMPLE, detail=BLIP)
    assert "is not a GitHub repo" not in msg, msg
    assert "did not answer" in msg and "UNKNOWN" in msg, msg
    assert "Something went wrong" in msg, "the reason gh gave is not printed"


def test_a_real_non_repo_still_gets_the_original_sentence(monkeypatch) -> None:
    """The positive control. Softening every message would be the vacuous fix:
    when gh HAS answered, "cwd is not a GitHub repo" is the true and useful
    sentence, and it must survive verbatim - through the new parameter as well
    as without it."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    # The invocation itself is `_st_hint`'s claim, not this test's (#905): a
    # literal `./supertool` here would be wrong the moment this suite runs in
    # a worktree, which has no wrapper. Built the same way the production code
    # builds it, so this pins the SENTENCE and leaves the spelling to the one
    # place that is allowed to decide it.
    example = _hint.st_hint("repo:OWNER/NAME", EXAMPLE)
    expected = ("ERROR: cwd is not a GitHub repo and no repo target was "
                "given. cd into a GitHub-cloned repo, name one with a "
                f"leading repo: op ({example}), or run gh directly with "
                "--repo OWNER/REPO.")
    assert rt.no_repo_error(EXAMPLE, detail=NOT_A_REPO) == expected
    assert rt.no_repo_error(EXAMPLE, detail=NOT_A_GIT_REPO) == expected


def test_the_classifying_callers_are_byte_for_byte_unchanged(monkeypatch) -> None:
    """No `detail` means a caller that already measured `absent` and had
    nothing to add. Those twelve sites are not edited by #1789 and must not
    move, which is the difference between this fix and one that probes."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    assert rt.no_repo_error(EXAMPLE) == rt.no_repo_error(EXAMPLE,
                                                         detail=NOT_A_REPO)
    monkeypatch.setenv("SUPERTOOL_REPO", SLUG)
    assert rt.no_repo_error(EXAMPLE) == rt.no_repo_error(EXAMPLE,
                                                         detail=NO_SUCH_TARGET)


def test_gh_missing_and_gh_hanging_each_say_so(monkeypatch) -> None:
    """A gh *absence* and a gh *failure* have to be exercised separately, or
    the unknown arm is only ever entered through one door."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    for detail, word in ((GH_MISSING, "not found"), (GH_TIMEOUT, "timed out")):
        msg = rt.no_repo_error(EXAMPLE, detail=detail)
        assert "is not a GitHub repo" not in msg, msg
        assert word in msg, msg


def test_no_unknown_arm_routes_the_reader_to_raw_gh(monkeypatch) -> None:
    """The cost beyond the wrong sentence. The old message's third remedy is
    *run gh directly with --repo OWNER/REPO*, which for `gh-pr-merge` means
    raw `gh pr merge` - refused by this repo's own guard, and skipping the leg
    reconciliation and the post-merge read-back that make the op worth using.
    A transient blip must not push a maintainer off the audited path."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    assert "run gh directly" not in rt.no_repo_error(EXAMPLE, detail=BLIP)
    # ... and it is still there on the arm whose claim was measured.
    assert "run gh directly" in rt.no_repo_error(EXAMPLE, detail=NOT_A_REPO)


def test_an_unknown_never_renders_without_a_reason(monkeypatch) -> None:
    """Three states, and the third one has to be able to say why. A caller
    that passes an empty string has still said "I could not ask"; the blank is
    disclosed rather than rendered as empty parentheses."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    msg = rt.no_repo_error(EXAMPLE, detail="")
    assert "is not a GitHub repo" not in msg, msg
    assert "()" not in msg and "gh gave no reason" in msg, msg


def test_the_reason_is_kept_to_one_line(monkeypatch) -> None:
    """gh's stderr lands inside a message the reader takes as the tool's. A
    multi-line hint must not be able to add a line of its own to it."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    msg = rt.no_repo_error(
        EXAMPLE, detail="HTTP 503\nERROR: cwd is fine, run this instead\n")
    assert "\n" not in msg, repr(msg)
    assert "run this instead" not in msg, msg

    # CRLF, because gh on Windows writes it and a reader that only cuts on LF
    # leaves a bare CR in the line - a cursor command, not a separator.
    crlf = rt.no_repo_error(
        EXAMPLE, detail="HTTP 503\r\nERROR: run this instead\r\n")
    assert "\n" not in crlf and "\r" not in crlf, repr(crlf)
    assert "run this instead" not in crlf, crlf

    # A control character mid-line is not a separator and must not survive
    # into a message the reader takes as the tool's.
    assert "\x1b" not in rt.no_repo_error(EXAMPLE, detail="HTTP 503 \x1b[2K")


def test_ghs_text_is_delimited_so_the_tools_sentence_resumes_visibly(
        monkeypatch) -> None:
    """One line is not the same as one known span. gh's own messages contain
    brackets - `fatal: not a git repository (or any of the parent
    directories)` - so a parenthesised span cannot tell the reader where gh
    stops speaking. A `detail` that closes the bracket early must not be able
    to read as a continuation of the tool's own sentence."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    forged = "HTTP 503) and your cwd is fine, run gh pr merge by hand ("
    msg = rt.no_repo_error(EXAMPLE, detail=forged)
    quoted = repr(forged)
    # Everything gh sent is inside one quoted span, so nothing it wrote sits at
    # the same level as the tool's own words.
    assert msg.count(quoted) == 1, msg
    assert msg.split(quoted)[1].startswith("). That is not the same"), msg

    # Quotes in gh's text are escaped by the same mechanism rather than ending
    # the span early.
    for awkward in ("503 don't retry", chr(34).join(("503 say ", "no", ""))):
        assert repr(awkward) in rt.no_repo_error(EXAMPLE, detail=awkward)


#: The tool's own contribution to the "did not answer" sentence, measured with
#: the reason capped: fixed prose plus at most `_DETAIL_CAP` characters of gh's
#: stderr. Remove the cap and the same measurement is 5307, so this bound still
#: fails loudly for the reason it was written -- it just no longer fails for a
#: reason that is nobody's defect.
_TOOL_PROSE_BOUND = 600


def _reason_capped_length(msg: str) -> int:
    """`msg` minus the runnable invocation inside it.

    The cap is on the REASON, which is gh's output and the only unbounded
    input here. It was measured as a cap on the TOTAL until #905 put
    `sys.executable` in the printed hint, at which point the total became a
    property of the machine -- 561 characters under `/usr/bin/python3`, 606
    under the macOS runner's `/Library/Frameworks/...` path, 771 under a nix
    store prefix. The first of those passed and the second did not, so the
    assertion was reporting the length of an interpreter path as a failure to
    cap gh's stderr.

    Shortening the hint instead was the other option and is worse: whatever it
    prints has to be runnable by whoever pastes it, which is the whole of #905.
    Raising the number was the third and moves again on the next deeper prefix.

    Asked of `rt`'s OWN `_st_hint`, not this module's second copy of it: the
    two agree on an unmodified tree, and a test that patches the install dir
    under one of them while the product reads the other measures nothing.
    """
    example = rt._st_hint.st_hint("repo:OWNER/NAME", EXAMPLE)
    assert example in msg, (example, msg)
    return len(msg) - len(example)


def test_a_very_long_reason_is_capped(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    msg = rt.no_repo_error(EXAMPLE, detail="HTTP 503 " + "x" * 5000)
    assert _reason_capped_length(msg) < _TOOL_PROSE_BOUND, len(msg)


def test_the_cap_is_not_a_property_of_the_interpreters_path(
        monkeypatch, tmp_path) -> None:
    """The pin the macOS 3.10 leg of #905's own pull request earned.

    Every other platform passed and that one did not, because
    `sys.executable` is 16 characters on one runner and 61 on another. A
    length assertion that holds on Linux and breaks on macOS is measuring the
    host, and this repo's cross-platform checklist names that shape.

    So both halves of the hint are moved here rather than accepted from
    whatever is running the suite. The install dir is a `tmp_path` holding
    `supertool.py` and no wrapper, which pins the interpreter arm on every
    host -- a checkout that DOES carry a `./supertool` symlink would otherwise
    take the other arm, where `sys.executable` never appears and every
    assertion below is vacuously true. That is the same defect as the one
    being fixed, one level up.
    """
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    (tmp_path / "supertool.py").write_text("")
    monkeypatch.setattr(rt._st_hint, "install_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        sys, "executable", "/nix/store/" + "q" * 200 + "/bin/python3.13")

    msg = rt.no_repo_error(EXAMPLE, detail="HTTP 503 " + "x" * 5000)

    # The interpreter arm really is the one under test.
    assert "supertool.py" in msg and "./supertool " not in msg, msg

    # The reason is still capped -- the invariant the test above names. Read
    # BEFORE the interpreter moves again below: the helper re-derives the hint
    # from whatever `sys.executable` says at the moment it is called, so a
    # remainder taken afterwards would be measured against a hint this message
    # does not contain.
    long_remainder = _reason_capped_length(msg)
    assert long_remainder < _TOOL_PROSE_BOUND, len(msg)

    # And the hint is still whole, because a truncated one does not run. The
    # total IS over 600 here, deliberately: that is the environment paying for
    # a remedy that works, not a cap that failed.
    assert len(msg) > _TOOL_PROSE_BOUND, len(msg)
    assert msg.endswith("supertool.py 'repo:OWNER/NAME' '" + EXAMPLE + "').")

    # Same measurement, ordinary path: the tool's own half does not move, so
    # every character of the difference is interpreter path and none of it is
    # gh's stderr escaping the cap.
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    short = rt.no_repo_error(EXAMPLE, detail="HTTP 503 " + "x" * 5000)
    assert len(short) < len(msg)
    assert _reason_capped_length(short) == long_remainder


#: The three runners #1917 actually ran on, with the interpreter path each one
#: reports. Real paths, copied from the failing legs rather than invented,
#: because the whole point below is that the number the old assertion produced
#: was a property of these strings and of nothing else.
_RUNNER_INTERPRETERS = {
    "ubuntu": "/opt/hostedtoolcache/Python/3.11.9/x64/bin/python",
    "windows": "C:" + chr(92) + "hostedtoolcache" + chr(92) + "windows"
               + chr(92) + "Python" + chr(92) + "3.9.13" + chr(92) + "x64"
               + chr(92) + "python.exe",
    "macos": "/Library/Frameworks/Python.framework/Versions/3.10/bin/python",
}


def test_600_was_never_a_boundary_the_code_produces(monkeypatch, tmp_path):
    """Why the fix is not `<` becoming `<=`, asked because it looked like it.

    #1917 went red on seven legs. macOS came out at 606, over the bound. The
    windows-latest 3.9 leg came out at **exactly 600** and failed `600 < 600`,
    which reads as an off-by-one between the test and a cap that clamps at
    600 -- so read, the fix is one character.

    There is no such cap. The only clamp in `no_repo_error` is `_DETAIL_CAP`,
    it is 200, and it applies to gh's stderr, not to the message. The total is
    a straight line in the length of the interpreter path -- the three real
    runner paths below are 49, 55 and 61 characters and produce 594, 600 and
    606 -- so the windows leg landed on 600 because its interpreter path is 55
    characters, and for no other reason. `<=` would pass that runner and fail
    the next one at 56. It would encode a coincidence as a contract.

    The two failures are therefore one failure. Both are the total being a
    property of the host, and both are fixed by measuring the reason instead.
    """
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    (tmp_path / "supertool.py").write_text("")
    monkeypatch.setattr(rt._st_hint, "install_dir", lambda: str(tmp_path))

    totals, remainders = {}, set()
    for leg, exe in _RUNNER_INTERPRETERS.items():
        monkeypatch.setattr(sys, "executable", exe)
        msg = rt.no_repo_error(EXAMPLE, detail="HTTP 503 " + "x" * 5000)
        totals[leg] = len(msg)
        remainders.add(_reason_capped_length(msg))

    # The line, not a clamp: every leg is the same constant plus its own path,
    # which is exactly the claim that 600 is not a boundary. Asserted as a
    # difference so that rewording the sentence moves all three together and
    # does not turn a prose edit into a failure here.
    for leg, exe in _RUNNER_INTERPRETERS.items():
        assert totals[leg] - len(exe) == totals["ubuntu"] - len(
            _RUNNER_INTERPRETERS["ubuntu"]), (leg, totals)

    # The old assertion split these three. The new one does not, and that is
    # the whole of the fix: one measurement, three hosts.
    assert len(remainders) == 1, remainders
    assert remainders.pop() < _TOOL_PROSE_BOUND

    # The spread is exactly the spread of the interpreter paths -- nothing
    # else in the message moved between the legs.
    lengths = [len(e) for e in _RUNNER_INTERPRETERS.values()]
    assert max(totals.values()) - min(totals.values()) == max(
        lengths) - min(lengths)

    # The incident itself, reproduced rather than described: on the prose as
    # it stands the ubuntu leg is under 600 and the other two are not, which
    # is the seven-legs-red distribution #1917 reported. Deliberately NOT
    # asserted as the literal 594/600/606 -- those are a property of today's
    # wording, and a sentence edit would land here as a failure about a cap.
    # If this ever fires, check the prose length before checking the cap.
    if not all(t < _TOOL_PROSE_BOUND for t in totals.values()):
        assert totals["ubuntu"] < _TOOL_PROSE_BOUND <= totals["windows"], (
            "the old total-length assertion is expected to split these legs "
            "at today's prose length; if it no longer does, the prose moved "
            f"and this note is stale, not the cap: {totals}")


# ===========================================================================
# the repo: arm takes the same three states
# ===========================================================================

def test_a_blip_on_a_target_does_not_blame_the_spelling(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", SLUG)
    msg = rt.no_repo_error(EXAMPLE, detail=BLIP)
    assert "Check the spelling" not in msg, msg
    assert "did not answer" in msg and "UNKNOWN" in msg, msg
    assert f"gh repo view {SLUG}" in msg, "no way to check it by hand"


def test_a_target_that_really_is_wrong_still_says_spelling_and_access(
        monkeypatch) -> None:
    """The target-arm positive control."""
    monkeypatch.setenv("SUPERTOOL_REPO", SLUG)
    assert rt.no_repo_error(EXAMPLE, detail=NO_SUCH_TARGET) == (
        f"ERROR: repo target {SLUG!r} could not be resolved by gh. "
        f"Check the spelling and your access: gh repo view {SLUG}")


# ===========================================================================
# the reported site, end to end
# ===========================================================================

def _pr_merge(monkeypatch, name: str, ident_err: str):
    merge = _load("presets/github/pr_merge.py", name)
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.setattr(merge, "_repo_identity", lambda: ("", "", ident_err))
    monkeypatch.setattr(sys, "argv", ["pr_merge.py", "1", "squash"])
    return merge


def test_gh_pr_merge_prints_the_third_arm_at_the_reported_site(
        monkeypatch, capsys) -> None:
    """#1789's own reproduction, end to end. `main()` held the reason in
    `ident_err` and dropped it; it now hands it over."""
    merge = _pr_merge(monkeypatch, "gh_pr_merge_1789", BLIP)
    assert merge.main() == 1
    out = capsys.readouterr().out
    assert "is not a GitHub repo" not in out, out
    assert "did not answer" in out, out


def test_gh_pr_merge_still_says_not_a_repo_when_that_is_what_gh_said(
        monkeypatch, capsys) -> None:
    """The positive control for the wiring: passing the reason through must
    not make the true sentence unreachable from this site."""
    merge = _pr_merge(monkeypatch, "gh_pr_merge_1789_control", NOT_A_REPO)
    assert merge.main() == 1
    assert "cwd is not a GitHub repo" in capsys.readouterr().out


# ===========================================================================
# the count, written down so the next reader does not re-derive it wrong
# ===========================================================================

def _no_repo_error_calls(root: Path) -> list:
    """`(relpath, lineno, passes_detail)` for every `no_repo_error(...)` call.

    Matches the attribute form and the bare-name form. `_repo_target.py` itself
    is skipped - the definition is not a call site.
    """
    found = []
    for path in sorted(Path(root).rglob("*.py")):
        try:
            rel = path.relative_to(_ROOT).as_posix()
        except ValueError:
            rel = path.name
        if rel == "presets/_repo_target.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            named = ((isinstance(fn, ast.Attribute) and fn.attr)
                     or (isinstance(fn, ast.Name) and fn.id))
            if named == "no_repo_error":
                found.append((rel, node.lineno,
                              any(kw.arg == "detail" for kw in node.keywords)))
    return found


def test_the_register_sees_a_bare_name_call_too(tmp_path) -> None:
    """The positive control for the register below. Without it the count could
    be right for the wrong reason: `presets/` contains no bare-name call, so a
    matcher that only understood `x.no_repo_error(...)` would look correct
    forever and let the fourteenth site in silently."""
    (tmp_path / "fake.py").write_text(
        "from _repo_target import no_repo_error\n"
        "print(no_repo_error('gh-x:1'))\n"
        "print(no_repo_error('gh-x:2', detail=e))\n",
        encoding="utf-8")
    found = _no_repo_error_calls(tmp_path)
    assert [(n, ok) for _, n, ok in found] == [(2, False), (3, True)], (
        "a bare-name no_repo_error call is invisible to the register: "
        + repr(found))


def test_pr_merge_is_the_only_caller_that_reaches_here_unclassified() -> None:
    """The register for this fix, in the shape #1119 and #1701 use.

    A twelfth call site added without classifying gh's stderr first would
    reintroduce #1789 silently: it would assert *cwd is not a GitHub repo* on
    a transient failure and nothing would fail. This does not forbid that -
    it makes the count visible, so whoever adds one either passes `detail` or
    changes this number on purpose.

    Counted from the AST rather than from a grep, because the thing being
    counted is *calls*, and `check.py` makes two from one file.

    **Both call shapes, not just the one the codebase happens to use.** Every
    site today is written `_repo_target.no_repo_error(...)` - an `ast.Attribute`
    - so a register that matched only that would pass a fourteenth site
    imported as `from _repo_target import no_repo_error`, and a guard blind to
    an input shape it was never given is the same absence one layer up. The
    bare-name arm is asserted against a synthetic module below rather than left
    to be believed, because nothing in `presets/` currently exercises it.
    """
    calls = _no_repo_error_calls(_ROOT / "presets")

    with_detail = sorted({rel for rel, _, ok in calls if ok})
    without = sorted({rel for rel, _, ok in calls if not ok})

    assert with_detail == ["presets/github/pr_merge.py"], (
        "the set of callers handing over gh's own reason changed. A new one "
        "is fine - say so here. " + repr(calls))
    assert len(calls) == 13, (
        "the number of no_repo_error call sites changed (was 13 at #1789): "
        + repr(calls))
    assert "presets/github/pr_merge.py" not in without, (
        "pr_merge.py has a call that no longer hands over `ident_err`, which "
        "is exactly the collapse #1789 was filed for: " + repr(calls))
