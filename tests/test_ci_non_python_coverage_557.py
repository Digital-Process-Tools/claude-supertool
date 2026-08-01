"""CI must execute the non-Python code it reports green on (#557).

`.github/workflows/tests.yml` ran `pytest` across {ubuntu, macos, windows} ×
py3.9–3.12 and nothing else. `notifiers/claude-channel/channel.ts` — 200-odd
lines of TypeScript started in every radar session — had never had a line
executed by any leg. Its tests exist and are real integration tests, but they
`skipif` on `shutil.which("bun")`, which was `None` on all twelve legs, so they
were collected, skipped, and counted as neither a pass nor a failure. A PR
touching only `.ts` was indistinguishable from a fully verified one.

The scope decided, and the reason for the split:

* **TypeScript** needs a toolchain, so it gets its own job — one that installs
  bun, type-checks under the channel's own strict `tsconfig.json`, and runs the
  two channel test files with `SUPERTOOL_REQUIRE_JS=1`, which converts a missing
  prerequisite into a collection error. A job that can only pass proves nothing.
* **Shell syntax** needs nothing, so it is checked here, in the suite, on all
  twelve legs, for no extra wall-clock and no new job. `bash -n` answers a
  platform-independent question, so running it wherever `bash` is present is
  complete coverage rather than partial.
* **What stays uncovered is enumerated below** rather than left to be inferred
  from a green tick, and the enumeration is itself asserted — a new `.ts` file
  fails this suite until somebody classifies it.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from _toolchain_gate import (ToolchainPromiseBroken, js_promised,
                             posix_ci_promised, require_or_skip, which)
from _workflow_parse import job_blocks, job_steps, matrix_os, run_blocks

REPO = Path(__file__).resolve().parents[1]

_SHEBANG = re.compile(rb"^#!.*\b(?:ba|da|z|k)?sh\b")

#: A tracked channel test file, and the same name as a pytest argument inside a
#: step's `run:` block. Discovered rather than listed — #730's second half: the
#: workflow guard below named two of these while the job ran five, so three
#: could have been dropped from CI without this file noticing. #605 and #609
#: had already arrived without being added to the job, which is the same gap in
#: the other direction. A list here would have needed the same edit the
#: workflow did, at the same moment nobody made it.
_CHANNEL_TEST_RE = re.compile(
    r"^tests/test_notifiers_claude_channel_[A-Za-z0-9_]+\.py$")
_CHANNEL_ARG_RE = re.compile(
    r"tests/test_notifiers_claude_channel_[A-Za-z0-9_]+\.py")


def _tracked() -> list[str]:
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(REPO),
                          capture_output=True, timeout=60)
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def shell_files() -> list[Path]:
    """Every tracked file this repository ships that a shell will execute.

    Discovered rather than listed: `.sh` by name plus anything whose first line
    is a shell shebang, which is how `.githooks/pre-commit` and `pre-push` get
    in — they have no extension. A list would have gone stale at the next hook.

    Tracked files specifically, unlike `tests/_repo_walk.py`'s deliberately
    wider walk. That walk exists to catch a file *being written right now*; this
    one is about what the repository ships, and its own population is asserted
    below so a git that cannot answer fails rather than reporting a clean sheet.
    """
    found = []
    for rel in _tracked():
        path = REPO / rel
        if not path.is_file():
            continue
        if rel.endswith(".sh"):
            found.append(path)
            continue
        with open(path, "rb") as handle:
            if _SHEBANG.match(handle.readline()):
                found.append(path)
    return sorted(found)


def channel_test_files() -> set[str]:
    """Every tracked channel test file, by discovery.

    Its non-emptiness is asserted by every caller, for the same reason
    `shell_files()`'s is: a discovery bug here would render the guards below
    green while checking nothing, which is #557 reproduced inside the fix for
    #557 — and #731 in the file that names it.
    """
    return {rel for rel in _tracked() if _CHANNEL_TEST_RE.match(rel)}


# --- shell syntax, on every leg -------------------------------------------

_BASH = which("bash")

#: Scoped to the three tests that shell out, deliberately not `pytestmark`. A
#: module-level skip would take the gate's own unit tests and the workflow
#: guards down with it on a machine without bash — silence in the place this
#: file exists to remove silence from.
needs_bash = require_or_skip(
    _BASH is not None,
    "bash not on PATH, so shell syntax cannot be checked here",
    promised=posix_ci_promised(),
)


@needs_bash
def test_the_shell_population_is_not_empty() -> None:
    """A discovery bug must not read as a clean sheet.

    This is the failure that let #557 live: a check that ran over nothing
    rendered the same green as one that ran over everything.
    """
    found = shell_files()
    assert found, (
        "no shell files discovered — either git could not answer `ls-files` or "
        "the shebang match broke. Either way this file is now checking nothing "
        "and reporting a pass, which is the defect it exists to remove.")


@needs_bash
@pytest.mark.parametrize("rel", [
    ".githooks/pre-commit",
    ".githooks/pre-push",
    "hooks/session-start.sh",
    "notifiers/claude-channel/install.sh",
    "notifiers/cursor-witness/install.sh",
    "presets/watch/watch-mine.sh",
])
def test_the_known_shell_files_are_all_discovered(rel: str) -> None:
    """Named so a discovery regression names the file it stopped seeing.

    Still not `_repo_walk.is_machine_state`, but no longer because that rule
    excluded `.githooks/` — #593 removed the blanket dot-prefix exclusion, so
    the two walks now agree about scope. They stay separate because they answer
    different questions: that one is "which `.py` is source", `rglob`-wide so it
    can see a file being written right now; this one is "which files does this
    repository ship that a shell executes", which needs `git ls-files` plus a
    shebang read and has nothing to do with `.py`. Merging them would be one
    name wearing two rules, which is what `_repo_walk`'s own docstring refuses.
    """
    assert (REPO / rel) in shell_files(), (
        f"{rel} is no longer being syntax-checked")


@needs_bash
@pytest.mark.parametrize("path", shell_files(), ids=lambda p: p.name)
def test_every_shell_file_parses(path: Path) -> None:
    """`bash -n`: parse, do not execute.

    Cheap enough to run on all twelve legs, and it catches the class of defect
    that shipped `.githooks/pre-push` breakage in a repo whose CI ran no shell
    at all. It is not a substitute for running the installers — see the
    uncovered inventory below.
    """
    proc = subprocess.run([_BASH, "-n", str(path)], capture_output=True,
                          timeout=60)
    stderr = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, (
        f"{path.relative_to(REPO)} is not valid bash:\n{stderr}")


# --- the promise mechanism ------------------------------------------------


def test_a_present_prerequisite_yields_an_inactive_mark() -> None:
    mark = require_or_skip(True, "reason", promised=False)
    assert mark.args == (False,), mark
    assert mark.name == "skipif"


def test_an_absent_prerequisite_nobody_promised_skips_with_the_reason() -> None:
    mark = require_or_skip(False, "no bun on PATH", promised=False)
    assert mark.args == (True,), mark
    assert mark.kwargs["reason"] == "no bun on PATH", (
        "the reason is the whole value of a skip here: '36 skipped' with no "
        "reason is indistinguishable from 'not applicable', which is #557")


def test_an_absent_prerequisite_somebody_promised_raises() -> None:
    """The whole point. A promised toolchain that is missing goes red.

    Without this, a job whose install step half-fails skips its tests and
    reports the same green as a job that ran them — #557 reproduced inside the
    fix for #557.
    """
    with pytest.raises(ToolchainPromiseBroken) as excinfo:
        require_or_skip(False, "no bun on PATH", promised=True)
    assert "no bun on PATH" in str(excinfo.value)
    assert "would report a pass for tests that did not run" in str(excinfo.value)


def test_the_js_promise_is_off_unless_explicitly_set(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_REQUIRE_JS", raising=False)
    assert js_promised() is False
    monkeypatch.setenv("SUPERTOOL_REQUIRE_JS", "1")
    assert js_promised() is True
    monkeypatch.setenv("SUPERTOOL_REQUIRE_JS", "true")
    assert js_promised() is False, (
        "only the exact value the workflow sets counts; a near-miss spelling "
        "must not silently arm a promise nothing installed for")


def test_the_promise_is_not_inferred_from_ci(monkeypatch) -> None:
    """Eleven of the twelve pytest legs are on CI and install no JS runtime.

    Inferring the promise from `CI` would turn the whole board red to fix a
    reporting defect.
    """
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("SUPERTOOL_REQUIRE_JS", raising=False)
    assert js_promised() is False


def test_the_channel_test_discovery_is_not_empty() -> None:
    """A parametrize over an empty set collects nothing and renders green.

    Same failure as `test_the_shell_population_is_not_empty` guards against,
    one directory over: if `git ls-files` cannot answer, every channel guard
    below silently checks no file at all and the board says it checked five.
    """
    assert channel_test_files(), (
        "no channel test files discovered — git could not answer `ls-files`, "
        "so the gate and workflow guards below are now checking nothing and "
        "reporting a pass")


@pytest.mark.parametrize("name", sorted(channel_test_files()))
def test_the_channel_tests_route_their_skips_through_the_gate(name: str) -> None:
    """Pins the decision, not just its current effect.

    A bare `pytest.mark.skipif(shutil.which("bun") is None, ...)` here is what
    #557 is about: correct locally, and unable to fail in the job that installs
    bun.

    Over every channel file rather than the two this named until #731. The
    other three were added to the workflow and never to this list, so a raw
    `skipif` in #605, #609 or #612 would have skipped silently in the one job
    that can run them and left the leg green.
    """
    source = (REPO / name).read_text(encoding="utf-8")
    assert "require_or_skip(" in source, name
    assert "promised=js_promised()" in source, name
    assert "pytest.mark.skipif(" not in source, (
        f"{name} has a raw skipif again — it will skip silently in the "
        "notifiers job and the leg will still be green")


# --- the workflow, pinned structurally ------------------------------------
#
# #731. Every assertion in this section used to be a substring match against
# the whole of `tests.yml`. Roughly two thirds of that file is comments
# recording why each decision was made, so the needle and the prose describing
# the needle are the same match — and prose survives the change it describes:
#
#   assert "oven-sh/setup-bun" in text, "nothing installs bun any more"
#
# passed for months on the strength of the comment saying bun is *not*
# installed that way any more (#730). `assert "--no-cov" in notifiers` had the
# identical shape and nobody had noticed: the comment "--no-cov because
# pyproject's addopts carry a whole-suite 86% gate" sits eleven lines above the
# flag and would have kept that guard green with the flag deleted.
#
# So these read the job's *steps* — `uses:`, `env:`, `run:` — via
# `tests/_workflow_parse.py`. A comment can then say anything at all and no
# assertion moves, and the question "what would have to be true for this to
# fail?" has a product-shaped answer: delete the step, and it goes red.


_BUN_INSTALL_RE = re.compile(
    r"npm\s+(?:i|install)\s+(?:-g|--global)\s+bun\b"
    r"|curl\b[^\n]*\bbun\.sh/install")
_TSC_RE = re.compile(r"\btsc\b[^\n]*--noEmit")


def _notifiers_block() -> str:
    block = job_blocks().get("notifiers")
    assert block, (
        "there is no `notifiers` job in tests.yml. It is the only job that "
        "installs bun, so without it the channel tests skip on all fourteen "
        "legs and the board is green over TypeScript nothing has executed")
    return block


def _notifiers_steps() -> list:
    steps = job_steps(_notifiers_block())
    assert steps, (
        "no steps parsed out of the notifiers job — either the workflow's "
        "shape moved or the indentation parser broke. Either way every "
        "assertion below is now checking nothing and reporting a pass")
    return steps


def _channel_pytest_steps() -> list:
    steps = [s for s in _notifiers_steps()
             if "pytest" in s.run and _CHANNEL_ARG_RE.search(s.run)]
    assert steps, (
        "no step in the notifiers job runs any channel test file. The job "
        "installs a toolchain and then runs nothing with it — a green leg "
        "that proves less than no leg at all")
    return steps


def test_a_step_of_the_notifiers_job_actually_installs_bun() -> None:
    """Read out of `uses:` and `run:`, never out of the file's prose.

    The predecessor asserted `"oven-sh/setup-bun" in text`. That action was
    dropped for `npm i -g bun@1.3.14` and the string survived only inside the
    comment explaining the switch, so the assertion had been reporting on a
    comment since the day the switch landed. Both installation routes are
    accepted here because either is a real answer to "is bun on PATH for the
    steps below"; what is not accepted is a file that merely mentions one.
    """
    steps = _notifiers_steps()
    by_action = [s for s in steps if "setup-bun" in s.uses]
    by_run = [s for s in steps if _BUN_INSTALL_RE.search(s.run)]
    assert by_action or by_run, (
        "no step of the notifiers job installs bun — neither a `uses:` naming "
        "a setup-bun action nor a `run:` installing it from npm or bun.sh. "
        "Every step after it needs bun on PATH, and the channel tests would "
        "raise ToolchainPromiseBroken rather than skip, so this should be "
        f"loud. Steps found: {[s.name or s.uses for s in steps]}")


def test_the_job_runs_every_channel_test_file_the_repo_has() -> None:
    """The set the job passes to pytest must equal the set that exists.

    Both directions matter and both have already happened. #605 and #609
    landed without being added to the job, so their tests had never executed
    in CI — caught by hand while wiring #612 in. And until #731 this guard
    named only the 550 and 554 files while the step ran five, so three could
    have been dropped from the step without anything going red.

    Comparing sets rather than listing names means neither can recur: a new
    channel file fails this test until the workflow runs it, and a removed
    argument fails it until the file goes too.
    """
    tracked = channel_test_files()
    assert tracked, "no channel test files discovered — git could not answer"
    named: set[str] = set()
    for run in run_blocks(_notifiers_steps()):
        if "pytest" in run:
            named.update(_CHANNEL_ARG_RE.findall(run))
    assert named == tracked, (
        "the channel test files the notifiers job runs are not the channel "
        "test files this repo has. The 12-leg matrix installs no bun, so this "
        "job is the only place any of them executes — a file missing from it "
        "has never run in CI and its green means nothing:\n"
        f"  in the repo, not run by CI: {sorted(tracked - named)}\n"
        f"  run by CI, not in the repo: {sorted(named - tracked)}")


def test_the_channel_job_arms_the_promise() -> None:
    """Without this the job's tests skip and it passes having run nothing.

    Asserted on the step's parsed `env:` mapping. The whole-file version could
    have been satisfied by a comment quoting the variable — the neighbouring
    comment quotes it as `SUPERTOOL_REQUIRE_JS=1` and missed only by spelling.
    """
    for step in _channel_pytest_steps():
        assert step.env.get("SUPERTOOL_REQUIRE_JS") == "1", (
            f"step {step.name!r} runs channel tests without arming "
            "SUPERTOOL_REQUIRE_JS, so a failed bun install would skip them "
            f"and leave the job green. Its env is {step.env}")


def test_the_channel_job_type_checks_the_typescript() -> None:
    """`bunx tsc --noEmit` is the cheap half: it catches what the integration
    tests cannot, which is a type regression on a path they do not exercise."""
    steps = _notifiers_steps()
    assert any(_TSC_RE.search(s.run) for s in steps), (
        "no step of the notifiers job type-checks channel.ts. The integration "
        "tests only cover the paths they walk; the type check is what covers "
        f"the rest. Steps found: {[s.name or s.uses for s in steps]}")


def test_the_channel_job_states_why_it_skips_windows() -> None:
    """A Windows leg here would be theatre and the file has to say so.

    `channel.ts` binds an AF_UNIX socket. Installing bun on `windows-latest`
    would add a leg that skips by platform and reports green — the exact shape
    of the defect being fixed, wearing the fix's clothes.

    The first half is deliberately an assertion about prose, and that is not
    #731's defect: the property being pinned *is* that the file explains its
    platform scope, so a comment satisfying it is the correct answer rather
    than an accidental one. The second half is not about prose, and used to
    read `"windows" not in matrix` on a text slice — it now reads the parsed
    `matrix.os` list, so the comment a few lines above it that contains the
    word "windows-latest" can never be what answers.
    """
    block = _notifiers_block()
    header = block.split("steps:")[0]
    assert "AF_UNIX" in header, (
        "the notifiers job no longer explains its platform scope; the next "
        "reader will file 'why is windows missing' or, worse, add it")
    platforms = matrix_os(block)
    assert platforms, (
        "the notifiers job declares no matrix.os, so this guard cannot tell "
        "which platforms it runs on and is checking nothing")
    assert not any("windows" in name for name in platforms), (
        "a windows leg here installs a toolchain for tests that skip by "
        "platform and then reports green — #557's shape wearing #557's fix. "
        f"matrix.os is {platforms}")


def test_the_channel_job_disables_the_whole_suite_coverage_gate() -> None:
    """pyproject's addopts carry `--cov-fail-under=86` for the whole suite.

    Two files cannot meet it, so without `--no-cov` this job fails for a reason
    that has nothing to do with `channel.ts`.

    On the step's `run:`, not on the job text. The job text contains the
    comment "--no-cov because pyproject's addopts carry a whole-suite 86%
    gate", which would have held this guard green with the flag deleted —
    a second live instance of #730's shape, found while fixing the first.
    """
    for step in _channel_pytest_steps():
        assert "--no-cov" in step.run, (
            f"step {step.name!r} runs the channel tests without --no-cov, so "
            "the whole-suite 86% coverage gate applies to two files that "
            "cannot meet it and the job fails for an unrelated reason")


# --- the step parser, so a discovery bug cannot read as a clean sheet ------


def test_the_step_parser_finds_the_steps_that_exist() -> None:
    names = [s.name or s.uses for s in _notifiers_steps()]
    assert "Install bun" in names or any("setup-bun" in n for n in names), (
        f"the notifiers job's steps no longer include a bun install: {names}")


def test_the_step_parser_does_not_read_a_comment_as_a_step() -> None:
    """#731 in one fixture: the prose says everything, the step does nothing.

    If this parser ever let a comment reach `run` or `env`, every guard above
    would degrade straight back into the whole-file substring match they
    replaced — and it would do so silently, which is the defect itself.
    """
    fixture = "\n".join([
        "    steps:",
        "      - uses: actions/checkout@v7",
        "      # Bun used to be installed via `oven-sh/setup-bun`, and this",
        "      # step used to pass `--no-cov` and set SUPERTOOL_REQUIRE_JS.",
        "      - name: Run something else entirely",
        "        # tsc --noEmit and npm i -g bun both appear in this comment",
        "        run: echo hello",
    ])
    steps = job_steps(fixture)
    assert [s.name or s.uses for s in steps] == [
        "actions/checkout@v7", "Run something else entirely"]
    assert steps[1].run == "echo hello"
    assert steps[1].env == {}
    assert not _BUN_INSTALL_RE.search(steps[1].run)
    assert not _TSC_RE.search(steps[1].run)
    assert "--no-cov" not in steps[1].run
    assert "setup-bun" not in steps[1].uses


def test_the_step_parser_reads_block_scalars_and_env() -> None:
    """A folded `run: >-` block and its sibling `env:` are the two shapes the
    real workflow uses for the step this file cares about most."""
    fixture = "\n".join([
        "    steps:",
        "      - name: Run the channel integration tests for real",
        "        # SUPERTOOL_REQUIRE_JS is named in this comment and not set",
        "        env:",
        "          SUPERTOOL_REQUIRE_JS: \"1\"",
        "        run: >-",
        "          python -m pytest -n0 --no-cov",
        "          tests/test_notifiers_claude_channel_550.py",
    ])
    steps = job_steps(fixture)
    assert len(steps) == 1
    assert steps[0].env == {"SUPERTOOL_REQUIRE_JS": "1"}
    assert "--no-cov" in steps[0].run
    assert _CHANNEL_ARG_RE.findall(steps[0].run) == [
        "tests/test_notifiers_claude_channel_550.py"]


# --- what stays uncovered, said out loud ---------------------------------

#: Every tracked `.ts` file, and whether CI executes it. A new entry fails
#: `test_the_typescript_inventory_is_complete` until it is classified here, so
#: the next unexecuted TypeScript file cannot arrive silently — which is the
#: only durable half of #557's option (3).
TYPESCRIPT_INVENTORY = {
    # Type-checked by `bunx tsc --noEmit` and executed by the two channel test
    # files, on ubuntu and macOS.
    "notifiers/claude-channel/channel.ts": "executed",
    # NOT executed and NOT type-checked. It is a VS Code extension: compiling it
    # needs `npm install` of the `vscode` type packages, and exercising it needs
    # an editor host. It is outside the radar path that #550/#554 were about,
    # so the cost was judged higher than the risk and the gap is recorded rather
    # than closed. Worth its own issue, not this PR's scope.
    "notifiers/cursor-witness/extension/src/extension.ts": "uncovered",
    # A fixture for the `resolve` op's tests, not shipped code.
    "tests/fixtures/resolve/util.ts": "fixture",
}


def test_the_typescript_inventory_is_complete() -> None:
    tracked = {rel for rel in _tracked() if rel.endswith(".ts")}
    assert tracked, "no .ts files found — git could not answer"
    assert tracked == set(TYPESCRIPT_INVENTORY), (
        "the TypeScript inventory in this file no longer matches the repo. "
        "Classify the difference — 'executed', 'uncovered' or 'fixture' — so "
        "an unexecuted file cannot arrive under a green tick:\n"
        f"  only in repo:      {sorted(tracked - set(TYPESCRIPT_INVENTORY))}\n"
        f"  only in inventory: {sorted(set(TYPESCRIPT_INVENTORY) - tracked)}")


def test_the_uncovered_typescript_is_named_and_not_merely_absent() -> None:
    """The inventory must keep saying what it cannot answer for.

    A green board that has never run a file is #557. An inventory that quietly
    lost its 'uncovered' entries would be the same thing one layer up.
    """
    uncovered = [k for k, v in TYPESCRIPT_INVENTORY.items() if v == "uncovered"]
    assert uncovered == ["notifiers/cursor-witness/extension/src/extension.ts"], (
        "the set of knowingly-unexecuted TypeScript changed. If something was "
        "covered, say so here; if something new is uncovered, file it.")


def test_the_shell_coverage_is_syntax_only_and_says_so() -> None:
    """No installer is executed by CI, and that is the honest statement.

    `bash -n` parses; it does not run `install.sh`. Executing an installer in CI
    means writing into `~/.claude`, cloning an MCP SDK and starting a server —
    real cost for a script whose failure mode is loud and immediate on the one
    machine that runs it. Stated rather than implied.
    """
    docs = (REPO / "docs" / "contributing.md").read_text(encoding="utf-8")
    assert "bash -n" in docs, (
        "docs/contributing.md no longer describes the shell check, so a "
        "contributor cannot tell syntax-only coverage from real coverage")
