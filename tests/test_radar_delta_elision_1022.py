"""A delta board must account for every row it did not print (#1022).

Observed live: `radar` printed three PR rows under a footer reading
`6 open | 2 failing | 4 running`, and `gh-prs` seconds later returned all six.
Nothing was lost in the fetch — the footer and the rows are computed from the
same population. `render` deliberately elides a row that is unchanged since the
previous snapshot and is not a standing problem, and `running` is not a standing
problem. The elision is defensible; its silence is not.

A partial board is strictly harder to notice than an empty one, because it looks
like a working board — a maintainer reads three rows and merges as though three
were all there was. So the invariant pinned here is arithmetic:

    rendered rows + rows the board says it elided == the population the footer
    counts

and the elided rows are named, because a number a reader cannot resolve back to
a PR is not a disclosure.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WATCH_DIR = ROOT / "presets" / "watch"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tier = _module("radar_gh_prs_1022", WATCH_DIR / "tiers" / "gh_prs.py")
gl_tier = _module("radar_gl_mrs_1022", WATCH_DIR / "tiers" / "gl_mrs.py")


# ---------------------------------------------------------------------------
# fixtures — the same two real boundaries the #859 file drives
# ---------------------------------------------------------------------------

RUN_LEG = {"name": "pytest", "status": "IN_PROGRESS", "conclusion": None,
           "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9"}
RED_LEG = {"name": "pytest (windows-latest, 3.12)", "status": "COMPLETED",
           "conclusion": "FAILURE",
           "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9"}


def _pr(number: int, rollup, sha: str = "a" * 40, **kw) -> dict:
    row = {
        "number": number, "title": f"pr {number}", "state": "OPEN",
        "author": {"login": "me"}, "headRefName": f"fix/{number}",
        "baseRefName": "master", "headRefOid": sha, "labels": [],
        "isDraft": False, "mergeable": "MERGEABLE", "reviewDecision": "",
        "statusCheckRollup": rollup, "additions": 1, "deletions": 1,
        "changedFiles": 1, "updatedAt": "2026-08-07T10:00:00Z",
        "createdAt": "2026-08-07T09:00:00Z", "assignees": [],
        "url": f"https://github.com/o/r/pull/{number}",
    }
    row.update(kw)
    return row


class _Result:
    def __init__(self, out: str = "", err: str = "", code: int = 0):
        self.stdout, self.stderr, self.returncode = out, err, code


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tier.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tier.snapshot.transport, "STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch):
    monkeypatch.setattr(tier.dispatcher, "start_poller",
                        lambda *a, **k: pytest.fail("tier spawned a poller"))


@pytest.fixture(autouse=True)
def quiet_reconcile(monkeypatch):
    monkeypatch.setattr(tier, "_reconcile_one", lambda p: ("", []))


def _fake_gh(monkeypatch, rows):
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda cmd, *a, **k: _Result(json.dumps(rows)))


def _board(monkeypatch, rows, watched):
    """One `radar_report` over `rows`, with everything but the board stubbed."""
    monkeypatch.setattr(tier, "default_branch_report", lambda *a, **k: ([], True))
    monkeypatch.setattr(tier, "repo_name",
                        lambda: "Digital-Process-Tools/claude-supertool")
    monkeypatch.setattr(tier, "watch_coverage", lambda: set(watched))
    _fake_gh(monkeypatch, rows)
    lines, _ = tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})
    return lines


# ---------------------------------------------------------------------------
# the accounting the board has to be able to survive
# ---------------------------------------------------------------------------

_FOOTER_OPEN = re.compile(r"(\d+) open\b")
_ELIDED = re.compile(r"(\d+) unchanged not shown\b")


def _footer_line(lines: list[str], word: str) -> str:
    hits = [ln for ln in lines if f" {word}" in ln and " | " in ln]
    assert hits, f"no footer line among {lines!r}"
    return hits[-1]


def _rendered_ids(lines: list[str], sigil: str) -> set[str]:
    """Identifiers of the rows actually printed, not of every line mentioning one.

    A row is a rendered board row; a WARNING or NOTE that names `#1004` is the
    disclosure, not the row, and counting it would make this test pass on the
    very output it exists to reject.
    """
    out: set[str] = set()
    for line in lines:
        if line.lstrip().startswith(("radar:", "[", "scope ")) or " | " in line:
            continue
        found = re.search(rf"{re.escape(sigil)}(\d+)\s", line)
        if found:
            out.add(found.group(1))
    return out


def test_gh_prs_delta_board_accounts_for_every_open_pr(state_dir, monkeypatch):
    """The live #1022 sequence: a full cold board, then two merges and a rebase.

    #1005/#979 are failing and #1013 picks up a conflict, so three rows are
    standing problems and print. #1004, #956 and #1018 are running and unmoved
    — the exact three that vanished under a footer still counting all six.
    """
    _board(monkeypatch,
           [_pr(1005, [RED_LEG]), _pr(979, [RED_LEG]), _pr(1004, [RUN_LEG]),
            _pr(956, [RUN_LEG]), _pr(1013, [RUN_LEG]), _pr(1018, [RUN_LEG]),
            _pr(1008, [RUN_LEG]), _pr(997, [RUN_LEG])],
           {"1005", "979", "1004", "956", "1013", "1018", "1008", "997"})

    lines = _board(
        monkeypatch,
        [_pr(1005, [RED_LEG]), _pr(979, [RED_LEG]), _pr(1004, [RUN_LEG]),
         _pr(956, [RUN_LEG]), _pr(1013, [RUN_LEG], mergeable="CONFLICTING"),
         _pr(1018, [RUN_LEG])],
        {"1005", "979", "1004", "956", "1013", "1018"})

    text = "\n".join(lines)
    footer = _footer_line(lines, "open")
    open_n = int(_FOOTER_OPEN.search(footer).group(1))
    assert open_n == 6, footer

    rows = _rendered_ids(lines, "#")
    assert rows == {"1005", "979", "1013"}, (
        "fixture drifted — this case is about the three that do NOT print")

    hidden = _ELIDED.search(footer)
    assert hidden, (
        f"the footer counts {open_n} open PRs and the board printed "
        f"{len(rows)} rows, and nothing on it says the other "
        f"{open_n - len(rows)} were elided. A board that silently prints a "
        f"subset is byte-identical to a board with less work on it.\n\n{text}")

    assert len(rows) + int(hidden.group(1)) == open_n, (
        f"rendered {len(rows)} + elided {hidden.group(1)} != {open_n} open"
        f"\n\n{text}")

    for number in ("1004", "956", "1018"):
        assert f"#{number}" in text, (
            f"#{number} was elided and never named; a count a reader cannot "
            f"resolve back to a PR is not a disclosure.\n\n{text}")


def test_gh_prs_full_board_claims_completeness_by_saying_nothing(
        state_dir, monkeypatch):
    """The absence of the elision line is the positive claim, so it must be
    absent when nothing was elided — otherwise the marker stops meaning
    anything and every board grows a line the reader learns to skip."""
    lines = _board(monkeypatch, [_pr(1, [RED_LEG]), _pr(2, [RUN_LEG])],
                   {"1", "2"})
    footer = _footer_line(lines, "open")
    assert not _ELIDED.search(footer), footer
    assert "unchanged not shown" not in "\n".join(lines)


def test_gh_prs_no_change_board_still_reconciles(state_dir, monkeypatch):
    """Every row elided is the case that already had a line — `radar: no
    change`. It must carry the same arithmetic, or the one board that is
    entirely elision is the one that does not say so."""
    rows = [_pr(1, [RUN_LEG]), _pr(2, [RUN_LEG]), _pr(3, [RUN_LEG])]
    _board(monkeypatch, rows, {"1", "2", "3"})
    lines = _board(monkeypatch, rows, {"1", "2", "3"})

    text = "\n".join(lines)
    assert any(ln.startswith("radar: no change") for ln in lines), text
    footer = _footer_line(lines, "open")
    hidden = _ELIDED.search(footer)
    assert hidden, text
    assert int(hidden.group(1)) == 3, footer
    assert len(_rendered_ids(lines, "#")) + 3 == 3, text


# ---------------------------------------------------------------------------
# the same defect in the sibling tier — the GitLab board elides identically
# ---------------------------------------------------------------------------

def _mr(iid: int, status: str, **kw) -> dict:
    """An *enriched* MR — `_enriched` is what says its status was actually read
    (#659). Without it every row here is unchecked, which is a different
    finding and would let this case pass for the wrong reason."""
    row = {
        "iid": iid, "title": f"mr {iid}", "source_branch": f"fix/{iid}",
        "target_branch": "master", "draft": False,
        "updated_at": "2026-08-07T10:00:00Z",
        "_pipeline": status, "_pipeline_id": str(100 + iid), "_pipeline_url": "",
        "_changes": 3, "_approved": True, "_approved_by": [],
        "_failed_jobs": [] if status != "failed" else ["test_unit"],
        "_enriched": True,
    }
    row.update(kw)
    return row


def test_gl_mrs_delta_board_accounts_for_every_open_mr(monkeypatch):
    """Same construct, same file shape, same silence — `gl_mrs.render` elides
    an unchanged non-problem row while `_footer` counts the population."""
    mrs = [_mr(1, "running"), _mr(2, "running"), _mr(3, "failed")]
    covered = {"1", "2", "3"}
    previous = {"mrs": {str(m["iid"]): gl_tier._snap_entry(m) for m in mrs}}

    lines = gl_tier.render(mrs, covered, [], {}, [], [], previous,
                           label="scope author=@me")
    text = "\n".join(lines)
    footer = _footer_line(lines, "open")
    open_n = int(_FOOTER_OPEN.search(footer).group(1))
    assert open_n == 3, footer

    rows = _rendered_ids(lines, "!")
    assert rows == {"3"}, f"fixture drifted: {text}"

    hidden = _ELIDED.search(footer)
    assert hidden, (
        f"the footer counts {open_n} open MRs, {len(rows)} row(s) printed, "
        f"and nothing says the rest were elided.\n\n{text}")
    assert len(rows) + int(hidden.group(1)) == open_n, text
    for iid in ("1", "2"):
        assert f"!{iid}" in text, text
