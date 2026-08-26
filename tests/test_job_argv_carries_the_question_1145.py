"""#1145 — `gh-job` / `gl-job` answered a question nobody asked.

Three separate ways the op's argv can stop meaning what the caller typed, all
of them ending in output that reads like a successful read of the job:

1. **An unrecognised mode token is dropped.** `sys.argv[2]` is compared against
   `raw` / `grep` / `errors` / `fail` and, when it matches none of them, the op
   falls through to its default render — metadata, then the log tail — and
   exits 0. The caller asked for a slice and got a tail, with nothing anywhere
   saying the mode was not applied. This is the shape reported on #1145: the
   op printed `PASS` and dumped the tail, and the reader concluded "nothing
   matched".

2. **A non-numeric job id is rendered as though it were the job.** GitHub's
   REST API coerces `actions/jobs/93211401185ep` to job 93211401185 and answers
   200, so a mangled id round-trips into `# Job #93211401185ep` over the real
   job's data. A corrupted identifier in the header is the tell that argv was
   mangled upstream; printing it as the job being read hides the tell.

3. **A colon inside a grep pattern truncates it silently.** Core splits an op
   on every `:`, so `grep:Error: not found` reaches the preset as two argv
   entries and only the first is used. The op then greps `/Error/` — a
   different question, answered confidently.

None of the three is about `|`. Alternation survives core's tokenizer intact
(`shlex.quote` per part), which the control below pins so a fix does not
"solve" it by refusing the character that always worked.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def _load(preset: str, name: str) -> Any:
    path = Path(__file__).parent.parent / "presets" / preset / "job.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh = _load("github", "github_job_1145")
gl = _load("gitlab", "gitlab_job_1145")

LF = chr(10)

TRACE = LF.join([
    "Running tests",
    "Error: not found while loading the fixture",
    "Error: unrelated",
    "1 passed",
]) + LF


def _gh_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
    meta = json.dumps({
        "name": "test-job", "status": "completed", "conclusion": "failure",
        "run_id": 42, "run_url": "https://github.com/x/y/actions/runs/42",
    })
    # First non-flag positional after `api` — the log call now inserts
    # --allow-escape-sequences before the url (#1957).
    url = next((a for a in args[2:] if not a.startswith("--")), "")
    if url.endswith("/logs"):
        return subprocess.CompletedProcess(args, 0, TRACE, "")
    return subprocess.CompletedProcess(args, 0, meta, "")


def _gl_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
    meta = json.dumps({
        "name": "test-job", "status": "failed", "stage": "test",
        "duration": 12.0, "web_url": "https://gitlab.example/job/1",
        "ref": "feature/x", "pipeline": {"id": 999},
    })
    url = args[2] if len(args) > 2 else ""
    return subprocess.CompletedProcess(
        args, 0, TRACE if url.endswith("/trace") else meta, "")


PRESETS = [
    pytest.param(gh, _gh_run, "gh-job", id="gh-job"),
    pytest.param(gl, _gl_run, "gl-job", id="gl-job"),
]


def _render(monkeypatch, capsys, mod, runner, argv: list[str]) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(mod.subprocess, "run", runner)
    rc = mod.main()
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------------------
# 1. an unrecognised mode is refused, not dropped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod,runner,op", PRESETS)
def test_an_unrecognised_mode_is_refused(monkeypatch, capsys, mod, runner, op) -> None:
    """A mode the op cannot serve must not fall through to the default render.

    `grepp` is what `:grep:` becomes when a character is lost upstream. The old
    behaviour answered the default question and exited 0, so the log tail read
    as "the grep found nothing".
    """
    rc, out = _render(monkeypatch, capsys, mod, runner,
                      ["job.py", "123", "grepp", "passed"])
    assert rc == 1, out
    assert "grepp" in out, out
    assert "grep" in out and "raw" in out and "fail" in out, out
    assert "# Job #" not in out, out


@pytest.mark.parametrize("mod,runner,op", PRESETS)
def test_the_four_real_modes_are_still_served(monkeypatch, capsys, mod, runner, op) -> None:
    """The control for the refusal above."""
    for mode in ("fail", "errors", "raw"):
        rc, out = _render(monkeypatch, capsys, mod, runner, ["job.py", "123", mode])
        assert rc == 0, (mode, out)
        assert "# Job #123" in out, (mode, out)
    rc, out = _render(monkeypatch, capsys, mod, runner,
                      ["job.py", "123", "grep", "passed"])
    assert rc == 0, out
    assert "## grep /passed/" in out, out


# ---------------------------------------------------------------------------
# 2. a job id that is not a job id never reaches the header
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod,runner,op", PRESETS)
def test_a_non_numeric_job_id_is_refused(monkeypatch, capsys, mod, runner, op) -> None:
    """`93211401185ep` is not an id. GitHub answers for it anyway.

    So the op cannot rely on the API to reject one: it has to look. Rendering
    `# Job #123ep` over job 123's real metadata publishes a corrupted
    identifier as the thing being read.
    """
    rc, out = _render(monkeypatch, capsys, mod, runner, ["job.py", "123ep"])
    assert rc == 1, out
    assert "# Job #123ep" not in out, out
    assert "123ep" in out, out


@pytest.mark.parametrize("mod,runner,op", PRESETS)
def test_a_numeric_job_id_is_still_read(monkeypatch, capsys, mod, runner, op) -> None:
    """The control."""
    rc, out = _render(monkeypatch, capsys, mod, runner, ["job.py", "123"])
    assert rc == 0, out
    assert "# Job #123" in out, out


# ---------------------------------------------------------------------------
# 3. a colon in the pattern is carried, not cut
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod,runner,op", PRESETS)
def test_a_colon_in_the_grep_pattern_survives(monkeypatch, capsys, mod, runner, op) -> None:
    """Core split `grep:Error: not found` into two argv entries.

    Rejoining them is the only reading that answers the question asked — the
    op takes no argument after the pattern, so everything to the right of
    `grep:` is the pattern. Reading only the first token greps `/Error/`,
    which matches a line the caller did not ask about.
    """
    rc, out = _render(monkeypatch, capsys, mod, runner,
                      ["job.py", "123", "grep", "Error", " not found"])
    assert rc == 0, out
    # The count is the discriminator, not the body: `Error: unrelated` is one
    # line away from the hit and rides in as context either way. Truncated to
    # /Error/ the header read `2 matching lines`.
    assert "## grep /Error: not found/ — 1 matching lines" in out, out


@pytest.mark.parametrize("mod,runner,op", PRESETS)
def test_the_rejoin_is_disclosed(monkeypatch, capsys, mod, runner, op) -> None:
    """Rejoining is a reading of an ambiguous CLI, so it is stated.

    Same resolution core's own `grep` reached (`_colon_split_hint`): carry the
    pattern, echo how it was read, name the payload route. A silent rejoin
    would be the mirror of the silent truncation.
    """
    _, out = _render(monkeypatch, capsys, mod, runner,
                     ["job.py", "123", "grep", "Error", " not found"])
    assert "Error: not found" in out, out
    assert "':'" in out or "colon" in out.lower(), out


@pytest.mark.parametrize("mod,runner,op", PRESETS)
def test_a_pattern_without_a_colon_says_nothing_about_colons(
        monkeypatch, capsys, mod, runner, op) -> None:
    """Crying wolf on every grep would make the disclosure worthless."""
    _, out = _render(monkeypatch, capsys, mod, runner,
                     ["job.py", "123", "grep", "passed"])
    assert "colon" not in out.lower(), out


# ---------------------------------------------------------------------------
# the control that keeps the fix off the character that always worked
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod,runner,op", PRESETS)
def test_alternation_is_not_the_bug(monkeypatch, capsys, mod, runner, op) -> None:
    """`|` reaches the preset intact and must keep doing so.

    Core shell-quotes each part before substituting `{args}`, so a pipe is
    never a shell operator here. #1145 leaned toward refusing the character;
    it was never the one that broke.
    """
    rc, out = _render(monkeypatch, capsys, mod, runner,
                      ["job.py", "123", "grep", "passed|missing"])
    assert rc == 0, out
    assert "## grep /passed|missing/" in out, out
    assert "1 passed" in out, out
