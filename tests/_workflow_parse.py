"""Structural reads of `.github/workflows/tests.yml`, for the guards over it.

Two test files pin CI policy — `test_ci_job_timeouts_722.py` and
`test_ci_non_python_coverage_557.py` — and both used to ask their questions of
the workflow's *text*. That is the defect #731 is about: `tests.yml` is 183
lines of which roughly two thirds are comments explaining the decisions, so any
`X in text` needle can be satisfied by the prose describing X rather than by X.
The verified case was `assert "oven-sh/setup-bun" in text`, kept green for
months by the comment recording that the action had been dropped.

The answer is to read the *structure*: which jobs exist, which steps they
declare, and what each step's `uses:`, `env:` and `run:` actually contain.
A comment can then say anything at all and change no assertion, because
comments are not steps and are not run blocks.

**PyYAML is deliberately not used**, and since #1213 the reason is no longer
that it is unavailable. It said CI installed "pytest, pytest-cov, pytest-xdist
and pytest-timeout and nothing else", so an import here would make every guard
built on it skip on all fourteen legs — silence in the files whose subject is
silence. `ruff` and `markdown-it-py` were already in the `dev` extra when that
was written, and `pyyaml` joined them in #1213, so both workflows install it and
the premise is now simply false.

The decision stands on its own footing instead. A guard over CI policy that
imports a third-party parser can be skipped by that parser going missing, and
this file's whole subject is a check that reports nothing and reads as a pass;
an indentation parser that ships with the repo cannot be uninstalled out from
under it. It is enough for one workflow whose shape is pinned by the tests
below it.

It is a parser, so it can be wrong in the one direction that matters: finding
nothing and reporting a clean sheet. Every function here is fixture-tested in
`test_ci_non_python_coverage_557.py` (steps) and `test_ci_job_timeouts_722.py`
(jobs), and both files assert their discovery is non-empty against the real
workflow before asserting anything about its contents.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"

#: A key at exactly two spaces of indent inside the top-level `jobs:` mapping.
_JOB_RE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$")

#: Four spaces: a *job's* budget. A step's would be at eight or more, under a
#: `- name:`, and must not be mistaken for its job's — a step ceiling leaves
#: the rest of the job unbounded.
_JOB_TIMEOUT_RE = re.compile(r"^    timeout-minutes:\s*(\d+)\s*$", re.M)

_STEPS_RE = re.compile(r"^    steps:\s*(?:#.*)?$")
_STEP_START_RE = re.compile(r"^      - (\S.*)$")
_KEY_RE = re.compile(r"^        ([A-Za-z_][A-Za-z0-9_-]*):(?: (.*))?$")
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?$")
_MATRIX_OS_RE = re.compile(r"^        os:\s*\[(.*)\]\s*$", re.M)


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def job_blocks(text: str | None = None) -> dict[str, str]:
    """Map each job name to the raw text of its block.

    Takes the text so the parser can be exercised against fixtures; a parser
    that silently found nothing would render its callers green while checking
    no job at all, which is the #557 shape.

    The block keeps its comments — a couple of guards are deliberately *about*
    the prose (that the notifiers job states why it skips Windows, for
    instance). Guards that are about behaviour go through `job_steps` instead.
    """
    lines = (workflow_text() if text is None else text).splitlines()
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    in_jobs = False
    for line in lines:
        if not line.startswith((" ", "\t")) and line.strip():
            in_jobs = line.split("#", 1)[0].strip() == "jobs:"
            current = None
            continue
        if not in_jobs:
            continue
        match = _JOB_RE.match(line)
        if match:
            current = match.group(1)
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    return {name: "\n".join(body) for name, body in blocks.items()}


def job_budget(block: str) -> int | None:
    """The job-level `timeout-minutes` of a job block, or None if it has none."""
    found = _JOB_TIMEOUT_RE.findall(block)
    return int(found[0]) if found else None


def matrix_os(block: str) -> list[str]:
    """The `matrix.os` list a job declares, as names — `[]` if it declares none.

    Read out of the mapping rather than by asking whether the word "windows"
    appears in the block, because each of these jobs carries a comment
    explaining which platforms it runs on and why.
    """
    match = _MATRIX_OS_RE.search(block)
    if not match:
        return []
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


@dataclass(frozen=True)
class Step:
    """One entry of a job's `steps:` list.

    `uses`, `env` and `run` are the executable surface — what CI will actually
    do. An assertion phrased against these cannot be satisfied by a comment,
    which is the entire reason this module exists.
    """

    name: str = ""
    uses: str = ""
    run: str = ""
    env: dict[str, str] | None = None
    with_: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.env is None:
            object.__setattr__(self, "env", {})
        if self.with_ is None:
            object.__setattr__(self, "with_", {})


def job_steps(block: str) -> list[Step]:
    """Parse a job block's `steps:` list.

    Comment-only lines are skipped rather than parsed: they match no key
    regex, so they contribute to no field. A `#` inside a `run:` block scalar
    is shell, not YAML, and is kept — it is part of what the step runs.
    """
    lines = block.splitlines()
    start = None
    for index, line in enumerate(lines):
        if _STEPS_RE.match(line):
            start = index + 1
            break
    if start is None:
        return []

    chunks: list[list[str]] = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("      "):
            break
        match = _STEP_START_RE.match(line)
        if match:
            chunks.append(["        " + match.group(1)])
        elif chunks:
            chunks[-1].append(line)
    return [_parse_step(chunk) for chunk in chunks]


def run_blocks(steps: list[Step]) -> list[str]:
    """Every non-empty `run:` body in a job, in declaration order."""
    return [step.run for step in steps if step.run]


def _parse_step(lines: list[str]) -> Step:
    fields: dict[str, object] = {}
    index = 0
    while index < len(lines):
        match = _KEY_RE.match(lines[index])
        if not match:
            index += 1
            continue
        key = match.group(1)
        inline = (match.group(2) or "").strip()
        body, index = _value_body(lines, index, inline)
        if key == "env":
            fields["env"] = _mapping(body)
        elif key == "with":
            fields["with_"] = _mapping(body)
        elif key in ("name", "uses", "run"):
            fields[key] = body if key == "run" else body.strip()
    return Step(**fields)


def _value_body(lines: list[str], index: int, inline: str) -> tuple[str, int]:
    """An inline scalar, or the indented block that follows a `|`/`>`/mapping."""
    if inline and not _BLOCK_SCALAR_RE.match(inline):
        return inline, index + 1
    body: list[str] = []
    cursor = index + 1
    while cursor < len(lines):
        line = lines[cursor]
        if line.strip() and len(line) - len(line.lstrip(" ")) <= 8:
            break
        body.append(line)
        cursor += 1
    return textwrap.dedent("\n".join(body)).strip("\n"), cursor


def _mapping(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out
