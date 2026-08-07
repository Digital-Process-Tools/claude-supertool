"""A runner description or a job ref cannot become a line `gl-runners` wrote (#970).

Same mechanism as #965 — an API-supplied string reaches `print()` without
`_untrusted.flat`, and `str.splitlines()` breaks on ten separators that
`git check-ref-format` and GitLab's runner-description field both accept — but
a different surface, and the surface changes what the payload can forge.

`gl-runners` renders three hand-padded, column-aligned blocks:

* the fleet table (`_print_fleet`), `{description[:22]:<22}` and `{tags[:34]}`,
* the queue view (`_print_queue`), `{name:<44} {ref:<24} on {runner}`,
* the STARVED / UNKNOWN diagnosis (`_print_diagnosis` via `_owner_names`), where
  a runner description is interpolated into the sentence naming which hosts a
  reader should go and check.

None of these goes through `_board.render_row`, which flattens every cell it is
handed — they pad with f-string field widths, so the guarantee that a board row
cannot become two rows does not reach here. The issue that filed this assumed
`_board` was in the path; it is not, which is why the defect exists at all.

What the payload gets is therefore stronger than one broken row. The text after
the separator is unconstrained, so it can be spaced to the same column widths as
the block it lands in and read as a row the tool emitted: a runner that is not in
the fleet, a `3 live runner(s)` verdict against a tag that has none, or the
`all have a responsive runner` all-clear under a queue that is starved. The
padding is computed on the pre-split string, so the surviving first half is also
short by the length of the tail — the one row that visibly misaligns is the one
carrying the forgery, and it is the least of what happened.

Nothing here feeds a merge decision, which is why #968 left it out. It feeds an
operational one: this is the view a person reads when CI is slow to decide
whether a runner is starved or wedged, and this tracker already has the worked
case where a misleading process view got two live watchers killed, one of them
the one that was needed.

The bar, unchanged from #965:

* **the forged text may not be its own rendered line**, asserted against
  `splitlines()`, which is what a reader and every consumer counts with;
* **the block may not gain a line at all** — a hostile payload and the same
  payload with the separator replaced by a space must render the same number of
  lines, which is the assertion a `flat()` that ran but printed the raw value
  beside it would still fail;
* **the fleet table's cells stay inside their columns** — asserted there and
  only there, because that block slices to the column width and this fix orders
  the slice after the flatten. The queue view has no slice and is not given one;
  the reasoning is on `test_queue_running_block_is_one_row_per_job`;
* **and nothing is censored** — every word survives, flattened, never dropped.

Every field here is display-only. Nothing in this op interpolates a description,
a tag or a ref into a command, and the two places that do consume tags —
`_can_serve` and `classify_queue` — compare sets and never render. So `flat()`
is the whole answer and #924's refusal has nothing to attach to: refusing to
print the description of the runner a reader is being sent to check would
withhold the fact the decision is made on.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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


runners_mod = _load("presets/gitlab/runners.py", "gitlab_runners_970")

#: Zl, not a control character — GitLab accepts it in a runner description and
#: in a ref, and `str.splitlines()` breaks on it (#886).
SEP = " "

# Each of these is padded to the widths of the block it is aimed at, so it does
# not merely add a line, it adds a *plausible* one.
FORGED_FLEET_ROW = (
    "9999  ghost-runner           shared   online    idle    1s      "
    "0    9         0     untagged-ok"
)
FORGED_QUEUE_VERDICT = "  [prod] 12 job(s) -> 3 live runner(s)"
FORGED_ALL_CLEAR = (
    "Queue: 1 pending, all have a responsive runner. "
    "Waiting on capacity, not routing."
)
FORGED_OWNER_LINE = "  - 1 job(s) tagged [prod] -> only builder-99 (seen 2s ago)"

FORGED_LINES = (
    FORGED_FLEET_ROW,
    FORGED_QUEUE_VERDICT,
    FORGED_ALL_CLEAR,
    FORGED_OWNER_LINE,
)


def _payload(sep: str) -> dict[str, Any]:
    """The same fleet twice: once with a separator, once with a space."""
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    old = (now - timedelta(seconds=3600)).isoformat().replace("+00:00", "Z")
    queued = (now - timedelta(seconds=900)).isoformat().replace("+00:00", "Z")

    # `dead` is offline by GitLab's own reckoning, so the pending job tagged for
    # it lands in `stuck` and `_print_diagnosis` renders the STARVED block that
    # interpolates this description.
    dead = {
        "id": 11,
        "description": f"builder-01{sep}{FORGED_FLEET_ROW}",
        "runner_type": "project_type",
        "status": "offline",
        "paused": False,
        "active": True,
        "job_execution_status": "idle",
        "contacted_at": old,
        "tag_list": [f"prod{sep}{FORGED_QUEUE_VERDICT}"],
        "run_untagged": False,
        "locked": False,
        "maximum_timeout": 3600,
        "version": f"16.0.0{sep}{FORGED_ALL_CLEAR}",
        "platform": "linux",
        "architecture": "amd64",
    }
    live = {
        "id": 22,
        "description": "healthy-02",
        "runner_type": "instance_type",
        "status": "online",
        "paused": False,
        "active": True,
        "job_execution_status": "active",
        "contacted_at": fresh,
        "tag_list": ["docker"],
        "run_untagged": True,
        "locked": False,
        "maximum_timeout": None,
        "version": "16.0.0",
        "platform": "linux",
        "architecture": "amd64",
    }
    return {
        "runners": [dead, live],
        "details": {11: dead, 22: live},
        "pending": [{
            "id": 901,
            "name": f"deploy{sep}{FORGED_OWNER_LINE}",
            "ref": f"release/1.0{sep}{FORGED_ALL_CLEAR}",
            "created_at": queued,
            "tag_list": [f"prod{sep}{FORGED_QUEUE_VERDICT}"],
        }],
        "running": [{
            "id": 902,
            "name": f"build{sep}{FORGED_QUEUE_VERDICT}",
            "ref": f"feat/x{sep}{FORGED_ALL_CLEAR}",
            "created_at": fresh,
            "tag_list": ["docker"],
            "runner": {"id": 22, "description": f"healthy-02{sep}{FORGED_FLEET_ROW}"},
        }],
    }


def _install_api(monkeypatch: Any, data: dict[str, Any]) -> None:
    def fake_api(endpoint: str, paginate: bool = False, timeout: int = 20) -> Any:
        if endpoint.startswith("projects/:id/runners"):
            return data["runners"], None
        if endpoint.startswith("runners/"):
            return data["details"][int(endpoint.split("/")[1])], None
        if "scope[]=pending" in endpoint:
            return data["pending"], None
        if "scope[]=running" in endpoint:
            return data["running"], None
        if endpoint.startswith("projects/:id/jobs?per_page"):
            return [], None
        raise AssertionError(f"unstubbed endpoint {endpoint!r}")

    monkeypatch.setattr(runners_mod, "_api", fake_api)


def _render(monkeypatch: Any, capsys: Any, mode: str, sep: str) -> str:
    _install_api(monkeypatch, _payload(sep))
    argv = ["runners.py"] + ([mode] if mode else [])
    monkeypatch.setattr(sys, "argv", argv)
    assert runners_mod.main() == 0
    return capsys.readouterr().out


MODES = ["", "queue", "full"]


# ---------------------------------------------------------------------------
# The forged text may not be its own line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_no_forged_line_in_any_mode(monkeypatch: Any, capsys: Any,
                                    mode: str) -> None:
    out = _render(monkeypatch, capsys, mode, SEP)
    rendered = out.splitlines()
    for forged in FORGED_LINES:
        assert forged not in rendered, (
            f"{forged!r} rendered as its own line in mode {mode or 'fleet'!r}:\n"
            + "\n".join(f"  {i:>3} | {line}" for i, line in enumerate(rendered, 1))
        )


# ---------------------------------------------------------------------------
# The block may not gain a line at all
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_a_separator_adds_no_line(monkeypatch: Any, capsys: Any,
                                  mode: str) -> None:
    """Same fleet, separator vs space. The render must be the same height.

    Stronger than the assertion above and the one a partial adoption fails: a
    site that flattens one field and prints its neighbour raw still produces a
    line nobody wrote, whatever that line happens to say.
    """
    hostile = _render(monkeypatch, capsys, mode, SEP).splitlines()
    benign = _render(monkeypatch, capsys, mode, " ").splitlines()
    assert len(hostile) == len(benign), (
        f"mode {mode or 'fleet'!r} grew from {len(benign)} to {len(hostile)} "
        f"lines because of a separator:\n"
        + "\n".join(f"  {i:>3} | {line}" for i, line in enumerate(hostile, 1))
    )


# ---------------------------------------------------------------------------
# The columns may not shift
# ---------------------------------------------------------------------------

def test_queue_running_block_is_one_row_per_job(monkeypatch: Any,
                                                capsys: Any) -> None:
    """`  {name:<44} {ref:<24} on {runner}` — one job, one row, one ` on `.

    The column offset itself is deliberately **not** asserted here, and the
    reason is the judgment this issue turns on. `flat()` spells U+2028
    `[U+2028]` — eight characters for one, because there is no Control Picture
    for a code point outside C0 (#863) — so a flattened cell can be wider than
    the cell that arrived. In `_print_fleet` that is contained, because the row
    slices to the column width and the fix orders the slice *after* the flatten.
    The queue view has no such slice, and giving it one is the trade this repo
    refuses: a job name cut to 44 characters to protect an alignment is a
    different job's name to the reader deciding which job is stuck, which
    converts a visibly ragged row into a quietly wrong one. A long job name
    already pushes this column on master with no hostile input involved.

    What a separator may not do is what the fleet table and this block both
    guarantee: make a row, or make a row disappear. `:<44` shifting on the one
    row that carries the payload is cosmetic and self-announcing; the rows above
    and below are independent `print()` calls and are untouched — which is also
    why the filing issue's "destroys the alignment for every row after it" does
    not hold.
    """
    payload = _payload(SEP)
    out = _render(monkeypatch, capsys, "queue", SEP)
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## Running"))
    end = next(i for i, line in enumerate(lines) if line.startswith("## Pending"))
    rows = [line for line in lines[start + 1:end] if line.strip()]
    assert len(rows) == len(payload["running"]), (
        f"{len(payload['running'])} running job(s) rendered as {len(rows)} rows:\n"
        + "\n".join(f"  {row!r}" for row in rows)
    )
    # `on {runner}` is the row's last field, so an intact row ends with the last
    # word of the description that was handed to it: the cell was flattened onto
    # this line rather than split off it or cut short of it.
    assert rows[0].endswith("untagged-ok"), (
        f"the runner cell did not survive intact on its row:\n  {rows[0]!r}")


def test_fleet_table_cells_stay_inside_their_columns(monkeypatch: Any,
                                                     capsys: Any) -> None:
    """DESCRIPTION is a 22-wide column; TYPE begins where the header says.

    `flat()` can widen a field — a stream that cannot carry `␨` spells the same
    separator `[U+2028]`, eight characters for one (#863) — so flattening a cell
    *after* slicing it to the column width trades a split row for an overflowing
    one. The order has to be flatten, then truncate.
    """
    out = _render(monkeypatch, capsys, "", SEP)
    lines = out.splitlines()
    header = next(line for line in lines if line.startswith("ID   "))
    type_at = header.index("TYPE")
    start = lines.index(header) + 2  # skip the rule
    rows = [line for line in lines[start:] if line[:1].isdigit()]
    assert rows, "no fleet rows rendered"
    for row in rows:
        assert row[type_at - 1] == " ", (
            f"DESCRIPTION overflowed its column into TYPE at offset {type_at}:\n"
            f"  {row!r}"
        )


# ---------------------------------------------------------------------------
# And nothing is censored
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_the_name_survives_in_full(monkeypatch: Any, capsys: Any,
                                   mode: str) -> None:
    """Flattened, never dropped — `_untrusted`'s disclosed-not-stripped rule.

    Checked on the untruncated renders: the STARVED block names the hosts to go
    and check, and `:full` is the detail view. Deleting the field would satisfy
    every assertion above and is the trade this repo refuses.
    """
    out = _render(monkeypatch, capsys, mode, SEP)
    # `ghost-runner` is the tail of a description in every mode: the fleet views
    # render the runner's own, the queue view renders the one on the job.
    assert "ghost-runner" in out, "the payload's own text was stripped, not flattened"
    if mode != "queue":
        assert "builder-01" in out


def test_starved_block_still_names_the_hostile_runner(monkeypatch: Any,
                                                      capsys: Any) -> None:
    """The finding the forgery was aimed at displacing still renders."""
    out = _render(monkeypatch, capsys, "", SEP)
    assert "## STARVED" in out
    assert "builder-01" in out
