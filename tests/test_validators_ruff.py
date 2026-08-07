"""The ruff validator adapter, and the ruleset it is pointed at (#666).

Two halves, and the split is deliberate.

**The adapter half** is the usual contract: one JSON object per
`validators/SCHEMA.md`, `skipped` when ruff is absent, an `adapter` error when
ruff itself falls over. Nothing here is specific to which rules are on.

**The ruleset half is the part worth writing.** A lint PR's real deliverable is
a *decision* — these rules, not those — and the obvious test for it is
"`ruff check .` reports nothing", which is worthless: it holds today, it stops
holding the first time anyone writes code, and when it breaks it says nothing
about whether the decision was right. It would also hand every unrelated PR a
red for a finding it did not cause, which is exactly the cost this repo has
been paying for over-eager gates.

So what is pinned is the configuration, exercised through ruff rather than
asserted about ruff. The repo's own `pyproject.toml` is copied verbatim into a
scratch directory, files carrying known violations are dropped beside it, and
the adapter is run on them. That asserts three things a `select = [...]`
string comparison cannot:

- every selected category actually fires on a violation of it;
- every deliberately ignored rule actually stays silent;
- the `per-file-ignores` entry for `supertool.py` is scoped to that name and
  no other — the same source under a different filename is still reported.

Copying the file rather than parsing it is also why this runs on 3.9: there is
no `tomllib` before 3.11 and no reason to grow a TOML dependency to read four
lines back out of a file ruff is about to read anyway.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_verdict import assert_declined, assert_ok, verdict
from _winenv import empty_path_env

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "ruff" / "ruff.py"
PYPROJECT = REPO / "pyproject.toml"

needs_ruff = pytest.mark.skipif(
    not shutil.which("ruff"),
    reason="ruff not on PATH — `pip install -e .[dev]` provides it",
)


def _spawn(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ADAPTER), *args],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
    )


def _run(file_path: Path) -> dict:
    return verdict(_spawn(str(file_path)), adapter=ADAPTER.name)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A scratch directory carrying this repo's real ruff configuration.

    `pyproject.toml` is copied byte for byte, so the rules under test are the
    rules that ship. Its `[project]` and `[tool.pytest.ini_options]` sections
    are inert to ruff.
    """
    shutil.copy(PYPROJECT, tmp_path / "pyproject.toml")
    return tmp_path


def _write(project: Path, name: str, body: str) -> Path:
    p = project / name
    p.write_text(body, encoding="utf-8")
    return p


def _codes(out: dict) -> list:
    return [e.get("code") for e in out.get("errors", [])]


# ---------------------------------------------------------------------------
# The ruleset: every selected category fires
# ---------------------------------------------------------------------------

#: One violation per selected category, chosen to be the least interesting
#: possible example of it — the point is that the category is switched on, not
#: that ruff can lint. `E9` is exercised separately below because a file that
#: does not parse cannot also demonstrate anything else.
SELECTED = [
    ("F", "F821", "def f():\n    return undefined_thing\n"),
    ("B", "B006", "def f(a=[]):\n    return a\n"),
    ("PLE", "PLE0704", "def f():\n    raise\n"),
]


@needs_ruff
@pytest.mark.parametrize(("category", "code", "body"), SELECTED,
                         ids=[c for c, _, _ in SELECTED])
def test_selected_category_is_reported(project: Path, category: str,
                                       code: str, body: str) -> None:
    out = _run(_write(project, f"v_{code.lower()}.py", body))
    assert_declined(out, context=f"{category} violation ({code})")
    assert code in _codes(out), (
        f"{category} is in select but {code} was not reported: {out}"
    )


@needs_ruff
def test_syntax_error_is_reported_as_an_error(project: Path) -> None:
    """E9 / the null-code path: a file that does not parse.

    Ruff gives a syntax error no rule code — it is not selectable — so the
    adapter has to make that legible on its own rather than emitting a
    `code: null` a reader cannot act on.
    """
    out = _run(_write(project, "broken.py", "def f(:\n"))
    assert_declined(out, context="a file that does not parse")
    err = out["errors"][0]
    assert err["severity"] == "error"
    assert "syntax error" in err["msg"]
    assert isinstance(err["line"], int)


# ---------------------------------------------------------------------------
# The ruleset: every ignored rule stays silent
# ---------------------------------------------------------------------------

@needs_ruff
def test_ignored_rules_stay_silent(project: Path) -> None:
    """F401, F841 and F541 are off, and off on purpose.

    All three are in the selected `F` category and all three are switched off
    in `ignore` because bringing 263 pre-existing occurrences to zero is its
    own PR. If someone re-enables them without doing that cleanup, this fails
    and points at the reason rather than at 263 findings.
    """
    body = (
        "import os\n"          # F401 — unused import
        "\n\n"
        "def f():\n"
        "    x = 1\n"          # F841 — unused local
        '    return f""\n'     # F541 — f-string with no placeholder
    )
    out = _run(_write(project, "ignored.py", body))
    assert_ok(out, context="a file whose only findings are deliberately ignored")
    assert out["count"] == 0


@needs_ruff
def test_b023_is_scoped_to_the_bulk_module_and_nothing_else(project: Path) -> None:
    """The `per-file-ignores` entry is a filename, and it is meant to be narrow.

    The same source is written three times under three names. `_supertool.py`
    is exempt — its 34 occurrences are closures invoked inside the iteration
    that defines them, which B023 cannot see. Nothing else is, and that is what
    stops the exemption from quietly widening into the whole repo.

    `supertool.py` is in that "nothing else" on purpose since #931 split it: it
    is a ~80-line entry point with no loop in it, so it has no claim on an
    exemption written for 34 call sites that all live in the other file. Left
    keyed to the old name, the exemption would have been protecting the shim
    and nothing at all.
    """
    body = (
        "def outer(items):\n"
        "    out = []\n"
        "    for i in items:\n"
        "        out.append(lambda: i)\n"
        "    return out\n"
    )
    exempt = _run(_write(project, "_supertool.py", body))
    assert_ok(exempt, context="B023 inside _supertool.py")

    shim = _run(_write(project, "supertool.py", body))
    assert_declined(shim, context="B023 inside the supertool.py entry-point shim")
    assert "B023" in _codes(shim)

    ordinary = _run(_write(project, "other_module.py", body))
    assert_declined(ordinary, context="B023 outside _supertool.py")
    assert "B023" in _codes(ordinary)


@needs_ruff
def test_a_clean_file_is_clean(project: Path) -> None:
    out = _run(_write(project, "fine.py", "def f(a):\n    return a + 1\n"))
    assert_ok(out)
    assert out["count"] == 0
    assert out["errors"] == []


# ---------------------------------------------------------------------------
# A cheap guard that runs even where ruff is not installed
# ---------------------------------------------------------------------------

def test_pyproject_carries_the_ruleset_and_its_reasons() -> None:
    """The config exists, is pinned to a target version, and is annotated.

    Deliberately thin — the tests above are the real pin. This one only has to
    fail loudly if the whole `[tool.ruff]` block is deleted on a runner where
    ruff is absent and every other test in this file skips.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "[tool.ruff]" in text
    assert "[tool.ruff.lint]" in text
    # py39 is not cosmetic: it is what keeps B905 (`zip(strict=)`, 3.10+) from
    # reporting nine findings the project cannot act on while it supports 3.9.
    assert 'target-version = "py39"' in text
    assert 'select = ["E9", "F", "B", "PLE"]' in text


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------

def test_missing_ruff_is_the_third_state(tmp_path: Path) -> None:
    """Absent tool → `skipped`, with `ok`/`count`/`errors` omitted entirely.

    Not `ok: true`. A linter nobody installed has produced no information
    about the file, and a receipt carrying `ok` reads as a pass to everything
    downstream — the delta arithmetic, the rollback decision, the cache.
    """
    f = tmp_path / "anything.py"
    f.write_text("def f():\n    return undefined_thing\n", encoding="utf-8")
    out = verdict(_spawn(str(f), env=empty_path_env()), adapter=ADAPTER.name)
    assert "skipped" in out, out
    assert "ruff" in out["skipped"]
    for key in ("ok", "count", "errors"):
        assert key not in out, f"a skip must not carry {key!r}: {out}"
    assert out["tool"] == "ruff"
    assert isinstance(out["duration_ms"], int)


def test_no_file_arg_is_an_adapter_error() -> None:
    out = verdict(_spawn(), adapter=ADAPTER.name)
    assert_declined(out)
    assert out["errors"][0]["code"] == "adapter"


@needs_ruff
def test_output_carries_every_required_field(project: Path) -> None:
    out = _run(_write(project, "fields.py", "def f(a=[]):\n    return a\n"))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out
    assert isinstance(out["duration_ms"], int)
    err = out["errors"][0]
    for key in ("line", "col", "severity", "code", "msg"):
        assert key in err
    assert err["severity"] in ("error", "warning", "info")
    assert err["source_context"], "a located finding carries its source lines"


@needs_ruff
def test_the_adapter_emits_exactly_one_json_object(project: Path) -> None:
    """Nothing but the verdict on stdout, whatever ruff writes to stderr.

    `--quiet` is doing the work here: without it ruff appends a human summary
    line ("Found 1 error.") after the JSON array and every consumer's
    `json.loads` fails on the trailing text.
    """
    r = _spawn(str(_write(project, "one.py", "def f(a=[]):\n    return a\n")))
    payload = json.loads(r.stdout.strip())
    assert isinstance(payload, dict)
    assert payload["tool"] == "ruff"
