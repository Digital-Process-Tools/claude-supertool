"""The claude-log ops must advertise `:raw` in their syntax, not only in prose (#778).

`:raw` is the opt-out from #760's redact-by-default — a breaking change to what
these three ops print. `docs/presets/claude-log.md` shows it in the syntax
column of its own table; `presets/claude-log.json` shipped a syntax string
without it. Two surfaces, two usages, for the same op, on the one flag a reader
most needs to find after that change.

Deliberately narrow. The tempting general rule — "every flag the description
names must appear in the syntax" — is **wrong here**, and checking that before
writing this file is what kept it out: `git-status` documents `:porcelain` as an
alias of `:full`, and `gh-job`/`gl-job` document `:errors` as an alias of
`:fail`. In all three the syntax deliberately shows the canonical spelling while
the description names the alias, which is a convention rather than a defect.
`:raw` is not an alias. It is the only spelling of a real flag, and it was
missing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "claude-log.json"
DOC = ROOT / "docs" / "presets" / "claude-log.md"

OPS = ("claude-log-list", "claude-log-tail", "claude-log-summary")


def _shipped() -> dict[str, str]:
    data = json.loads(PRESET.read_text(encoding="utf-8"))
    return {op: (spec.get("syntax") or "")
            for op, spec in data["ops"].items()}


def _documented() -> dict[str, str]:
    """The syntax cell each op advertises in its own docs table."""
    rows: dict[str, str] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|", line)
        if m:
            rows.setdefault(m.group(1), m.group(2))
    return rows


@pytest.mark.parametrize("op", OPS)
def test_shipped_syntax_advertises_raw(op: str) -> None:
    """`:raw` is reachable from `ops` output, not only from the description."""
    syntax = _shipped()[op]
    assert ":raw" in syntax, (
        f"{op} ships syntax {syntax!r}, which does not mention `:raw` — the "
        "opt-out from #760's redact-by-default. A reader who copies the usage "
        "pattern is shown the one string that omits the escape hatch."
    )


@pytest.mark.parametrize("op", OPS)
def test_shipped_syntax_matches_the_docs_table(op: str) -> None:
    """The two surfaces must state the same usage for the same op."""
    documented = _documented()
    assert op in documented, f"{op} has no syntax row in {DOC.name}"
    assert _shipped()[op] == documented[op], (
        f"{op}: preset ships {_shipped()[op]!r}, docs advertise "
        f"{documented[op]!r}. Whichever is right, a reader hitting the other "
        "one is being told something untrue about how to call this op."
    )


def test_the_docs_table_was_actually_parsed() -> None:
    """Guard the fixture's own premise.

    If the table's formatting changes, the regex above silently returns {} and
    both tests would fail for the wrong reason — or, worse, a laxer assertion
    would pass over nothing. Pin that the parse found all three rows.
    """
    documented = _documented()
    missing = [op for op in OPS if op not in documented]
    assert not missing, (
        f"parsed {len(documented)} syntax rows from {DOC.name} and none of them "
        f"were {missing} — the table format changed and this file is no longer "
        "reading it."
    )
