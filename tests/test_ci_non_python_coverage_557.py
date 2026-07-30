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

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"

_SHEBANG = re.compile(rb"^#!.*\b(?:ba|da|z|k)?sh\b")


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

    `.githooks/` is the reason this does not reuse `_repo_walk.is_machine_state`:
    that rule treats every dot-prefixed path component as machine state, which
    is right for `.venv` and `.pytest_cache` and wrong for the two hooks this
    repository ships and asks contributors to install.
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


def test_the_channel_tests_route_their_skips_through_the_gate() -> None:
    """Pins the decision, not just its current effect.

    A bare `pytest.mark.skipif(shutil.which("bun") is None, ...)` here is what
    #557 is about: correct locally, and unable to fail in the job that installs
    bun.
    """
    for name in ("tests/test_notifiers_claude_channel_550.py",
                 "tests/test_notifiers_claude_channel_554.py"):
        source = (REPO / name).read_text(encoding="utf-8")
        assert "require_or_skip(" in source, name
        assert "promised=js_promised()" in source, name
        assert "pytest.mark.skipif(" not in source, (
            f"{name} has a raw skipif again — it will skip silently in the "
            "notifiers job and the leg will still be green")


# --- the workflow, pinned -------------------------------------------------


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_a_job_installs_bun_and_runs_the_channel_tests() -> None:
    text = _workflow()
    assert "notifiers:" in text, "the notifiers job is gone"
    assert "oven-sh/setup-bun" in text, "nothing installs bun any more"
    for name in ("tests/test_notifiers_claude_channel_550.py",
                 "tests/test_notifiers_claude_channel_554.py"):
        assert name in text, f"{name} is no longer executed by any job"


def test_the_channel_job_arms_the_promise() -> None:
    """Without this the job's tests skip and it passes having run nothing."""
    assert "SUPERTOOL_REQUIRE_JS: \"1\"" in _workflow(), (
        "the notifiers job no longer arms SUPERTOOL_REQUIRE_JS, so a failed "
        "bun install would skip the tests and leave the job green")


def test_the_channel_job_type_checks_the_typescript() -> None:
    """`bunx tsc --noEmit` is the cheap half: it catches what the integration
    tests cannot, which is a type regression on a path they do not exercise."""
    assert "tsc --noEmit" in _workflow()


def test_the_channel_job_states_why_it_skips_windows() -> None:
    """A Windows leg here would be theatre and the file has to say so.

    `channel.ts` binds an AF_UNIX socket. Installing bun on `windows-latest`
    would add a leg that skips by platform and reports green — the exact shape
    of the defect being fixed, wearing the fix's clothes.
    """
    notifiers = _workflow().split("notifiers:", 1)[1]
    header = notifiers.split("steps:")[0]
    assert "AF_UNIX" in header, (
        "the notifiers job no longer explains its platform scope; the next "
        "reader will file 'why is windows missing' or, worse, add it")
    matrix = header.split("matrix:", 1)[1]
    assert "windows" not in matrix, (
        "a windows leg here installs a toolchain for tests that skip by "
        "platform and then reports green — #557's shape wearing #557's fix")


def test_the_channel_job_disables_the_whole_suite_coverage_gate() -> None:
    """pyproject's addopts carry `--cov-fail-under=86` for the whole suite.

    Two files cannot meet it, so without `--no-cov` this job fails for a reason
    that has nothing to do with `channel.ts`.
    """
    notifiers = _workflow().split("notifiers:", 1)[1]
    assert "--no-cov" in notifiers


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
