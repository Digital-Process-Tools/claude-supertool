"""A CI job name cannot become a line `gl-mr` wrote (#982).

#982 says the site is `presets/gitlab/mrs.py:1081`. It is not, and `gl-mrs` is
not affected at all: every cell of that board goes through
`_board.render_row`, which flattens what it is handed, and the board prints
`_untrusted.flat_note("MR titles")` above itself. The issue's own evidence table
names the real file — `mr.py:1081 name (via jname)` — and the prose beneath it
misattributed that to `mrs.py`. Re-derived on master at 88a22ba.

The file is right and the count is wrong. `presets/gitlab/mr.py` renders a job
name from the pipeline API in **two** places, not one, and they are on different
renders:

* `_named_gl_jobs` — `  {status}: {name} (job #{id}), ...`, the block the
  **`:status`** render prints. That is the poll-loop render: read most often and
  looked at least closely, which is the argument #982 makes for the board and
  which applies here with more force.
* `_failed_job_lines` — `  #{id} | {name} | {stage}`, the failed-jobs block on
  the full render. This is the `jname` the issue points at.

A job name comes out of `.gitlab-ci.yml` on the source branch, so anyone who can
open a merge request writes it, and `str.splitlines()` breaks on separators
GitLab accepts there. `_untrusted.flat` is the whole answer — the name is
display-only, nothing interpolates it into a command, and `gl-pipeline` already
flattens the same field from the same endpoint (`presets/gitlab/pipeline.py`),
so this is one render adopting a rule the neighbouring one already keeps.

The bar is #970's:

* the forged text may not be its own rendered line;
* the block may not gain a line at all, asserted by rendering the same jobs
  twice — once with a separator, once with a space — and comparing heights;
* nothing is censored: the job name is what the reader acts on.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mr_mod = _load("presets/gitlab/mr.py", "gitlab_mr_982")

#: Zl. GitLab accepts it in a job name and `str.splitlines()` breaks on it.
SEP = " "

#: Written to the shape of the `:status` block it lands in.
FORGED_STATUS_LINE = "  success: build (job #4021), test (job #4022)"

#: Written to the shape of the failed-jobs block on the full render.
FORGED_FAILED_ROW = "  #4021 | deploy-to-production | deploy"


def _jobs(sep: str) -> list[dict]:
    return [
        {"id": 101, "name": f"phpstan{sep}{FORGED_STATUS_LINE}",
         "stage": f"lint{sep}{FORGED_FAILED_ROW}", "status": "failed"},
    ]


def _named(monkeypatch: Any, sep: str) -> list[str]:
    monkeypatch.setattr(mr_mod, "_fetch_array",
                        lambda endpoint, noun, timeout=10: (_jobs(sep), None))
    return mr_mod._named_gl_jobs(77)


def _failed(monkeypatch: Any, sep: str) -> list[str]:
    return mr_mod._failed_job_lines(_jobs(sep))


#: name -> (renderer, forged line)
BLOCKS = {
    "status": (_named, FORGED_STATUS_LINE),
    "failed-jobs": (_failed, FORGED_FAILED_ROW),
}


def _rendered(lines: list[str]) -> list[str]:
    """What a reader counts: the lines, not the list elements.

    Both blocks return a list the caller prints one element per `print()`. A
    list of two elements is not a block of two lines if one element carries a
    separator, and that gap is the whole defect.
    """
    return "\n".join(lines).splitlines()


@pytest.mark.parametrize("block", sorted(BLOCKS))
def test_no_forged_line(monkeypatch: Any, block: str) -> None:
    render, forged = BLOCKS[block]
    rendered = _rendered(render(monkeypatch, SEP))
    assert forged not in rendered, (
        f"{forged!r} rendered as its own line in the {block} block:\n"
        + "\n".join(f"  {i:>3} | {line}" for i, line in enumerate(rendered, 1))
    )


@pytest.mark.parametrize("block", sorted(BLOCKS))
def test_a_separator_adds_no_line(monkeypatch: Any, block: str) -> None:
    render, _ = BLOCKS[block]
    hostile = _rendered(render(monkeypatch, SEP))
    benign = _rendered(render(monkeypatch, " "))
    assert len(hostile) == len(benign), (
        f"the {block} block grew from {len(benign)} to {len(hostile)} lines "
        f"because of a separator:\n" + "\n".join(hostile)
    )


@pytest.mark.parametrize("block", sorted(BLOCKS))
def test_every_word_survives(monkeypatch: Any, block: str) -> None:
    """The job name is the answer to "what broke". None of it is dropped."""
    render, forged = BLOCKS[block]
    out = "\n".join(render(monkeypatch, SEP))
    for word in forged.split():
        assert word in out, f"the {block} block dropped {word!r}:\n{out}"
