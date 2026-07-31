"""`gl-mrs:failed` must not answer "nothing is failing" from MRs it never checked (#652).

Driven end to end through `main()` over the two boundaries the op actually
reads — `glab mr list -F json` on stdout, and the per-MR detail/approvals API —
so a test cannot pass by pinning an internal predicate's return value. Every
case asserts the fixture really exercised the cap (`len(fetched) == cap`)
before asserting anything about the output, because a fixture that quietly
enriches everything would make this whole file green while testing nothing.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "mrs.py"
_spec = importlib.util.spec_from_file_location("gitlab_mrs_652", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
mrs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mrs)

CAP = mrs.ENRICH_CAP


def _row(iid: int) -> dict:
    """One row as `glab mr list -F json` returns it — never any pipeline field."""
    return {
        "iid": iid,
        "title": f"mr {iid}",
        "source_branch": f"b{iid}",
        "target_branch": "master",
        "updated_at": "2026-07-27T10:00:00Z",
        "blocking_discussions_resolved": True,
    }


def _drive(monkeypatch, capsys, rows, arg_str, failing=()) -> tuple[str, str, list[str]]:
    """Run gl-mrs over stubbed API payloads. Returns (stdout, stderr, iids fetched).

    `failing` is the set of iids whose *pipeline* is red. Whether the op ever
    learns that is the thing under test: the detail endpoint is the only place
    the status lives, and the enrichment cap decides who gets asked.
    """
    failing = set(failing)
    monkeypatch.setattr(sys, "argv", ["mrs.py", arg_str])
    monkeypatch.setattr(
        mrs, "_run",
        lambda cmd, timeout=25: subprocess.CompletedProcess(cmd, 0, json.dumps(rows), ""),
    )
    fetched: list[str] = []

    def _api(endpoint, timeout=10):
        if endpoint.endswith("/approvals"):
            return {"approved": True, "approved_by": []}
        if "/pipelines/" in endpoint:
            return [{"name": "phpstan2"}]
        iid = endpoint.rsplit("/", 1)[-1]
        fetched.append(iid)
        status = "failed" if int(iid) in failing else "success"
        return {"head_pipeline": {"status": status, "id": iid}, "changes_count": "3"}

    monkeypatch.setattr(mrs, "_api_json", _api)
    monkeypatch.setattr(mrs, "_watched_iids", lambda *a, **k: set())
    assert mrs.main() == 0
    cap = capsys.readouterr()
    return cap.out, cap.err, fetched


# ---------------------------------------------------------------------------
# The load-bearing one: an MR past the cap is unknown, never "not failing".
# ---------------------------------------------------------------------------

def test_a_failing_mr_past_the_enrichment_cap_appears_or_its_absence_is_disclosed(
    monkeypatch, capsys,
) -> None:
    """The whole contract in one assertion, stated as the reader would state it.

    An MR the enricher never reached is in the third state — unknown — and
    `:failed` currently folds it into "not failing" and drops it. Either it is
    on the board, or the board says it could not see it. Silence is the defect.
    """
    rows = [_row(i) for i in range(1, CAP + 6)]
    beyond = CAP + 3  # sits past the cap, so its pipeline is never fetched
    out, err, fetched = _drive(monkeypatch, capsys, rows, "failed", failing={beyond})

    assert len(fetched) == CAP, "fixture must actually hit the enrichment cap"
    assert str(beyond) not in fetched, "fixture is wrong: the MR was checked after all"

    combined = out + err
    assert f"!{beyond}" in combined or "not checked" in combined, (
        "a failing MR past the enrichment cap was dropped from :failed with "
        "nothing in the output saying anything was left unchecked"
    )


def test_the_failed_board_names_how_many_mrs_it_could_not_check(monkeypatch, capsys) -> None:
    """The disclosure has to carry the count and the escape, not just a hedge."""
    rows = [_row(i) for i in range(1, CAP + 6)]
    out, _err, fetched = _drive(monkeypatch, capsys, rows, "failed", failing={2})

    assert len(fetched) == CAP
    assert "!2" in out, "fixture must render the in-cap failing MR"
    assert "5 of 45 MRs not checked" in out
    assert str(CAP) in out
    assert "SUPERTOOL_ENRICH_CAP" in out


# ---------------------------------------------------------------------------
# Defect 2: the notice was computed from the post-filter list, so it could not
# fire in the one mode where the truncation causes harm.
# ---------------------------------------------------------------------------

def test_the_cap_notice_survives_the_failed_filter_shrinking_the_list(
    monkeypatch, capsys,
) -> None:
    """One failing MR out of 45 leaves a list of one — far under the cap. The
    disclosure is about what was *checked*, not about what survived."""
    rows = [_row(i) for i in range(1, CAP + 6)]
    out, _err, fetched = _drive(monkeypatch, capsys, rows, "failed", failing={2})

    assert len(fetched) == CAP
    assert out.count("!") >= 1
    assert "not checked" in out


def test_the_disclosure_counts_what_enrichment_actually_reached(monkeypatch, capsys) -> None:
    """Not re-derived from the configured cap — read off the MRs themselves.

    A count recomputed as `total - cfg[cap]` goes stale the moment `_enrich`
    changes which MRs it reaches, and goes stale silently.
    """
    rows = [_row(i) for i in range(1, 11)]
    real_enrich = mrs._enrich
    monkeypatch.setattr(mrs, "_enrich", lambda mr_list, *a, **k: real_enrich(mr_list, 2, 1))
    out, _err, _fetched = _drive(monkeypatch, capsys, rows, "failed", failing={1})

    assert "!1" in out
    assert "8 of 10 MRs not checked" in out


# ---------------------------------------------------------------------------
# Silence is a positive claim, and it is the half that is easy to break later.
# ---------------------------------------------------------------------------

def test_a_fully_checked_failed_board_prints_no_cap_marker_at_all(monkeypatch, capsys) -> None:
    """Under the cap, every MR was checked, so the absence of a failing MR is a
    real claim of health. Nothing extra may be printed, on either stream."""
    rows = [_row(i) for i in range(1, 6)]
    out, err, fetched = _drive(monkeypatch, capsys, rows, "failed", failing={2})

    assert len(fetched) == 5, "fixture must enrich every MR"
    assert "!2" in out, "fixture must render the failing MR"
    lowered = out.lower()
    for token in ("not checked", "cap", "supertool_enrich_cap", "unchecked"):
        assert token not in lowered, f"{token!r} leaked onto an uncut board"
    assert err == ""


def test_an_empty_fully_checked_failed_board_is_an_unqualified_all_clear(
    monkeypatch, capsys,
) -> None:
    """The output a human reads before walking away. Nothing failing, nothing
    unchecked — it must not hedge, and it must not be silent about being empty."""
    rows = [_row(i) for i in range(1, 6)]
    out, err, fetched = _drive(monkeypatch, capsys, rows, "failed")

    assert len(fetched) == 5
    assert "No MRs match." in out
    assert "not checked" not in out.lower()
    assert err == ""


def test_nopipe_prints_no_cap_marker(monkeypatch, capsys) -> None:
    """`nopipe` enriches nothing and claims nothing about pipelines — the
    pipeline column is a literal dash. A cap notice there would be noise."""
    rows = [_row(i) for i in range(1, CAP + 6)]
    out, err, fetched = _drive(monkeypatch, capsys, rows, "nopipe")

    assert fetched == [], "nopipe must not fetch any detail"
    assert "!1" in out
    assert "not checked" not in out.lower()
    assert err == ""


# ---------------------------------------------------------------------------
# The cap is not the only way an MR ends up unchecked. A detail fetch that
# fails returns {} — inside the cap — and that is the same third state.
# ---------------------------------------------------------------------------

def _drive_with_failures(monkeypatch, capsys, rows, arg_str, failing=(), broken=()):
    """As `_drive`, but the detail endpoint returns None for `broken` iids —
    what `_api_json` does on a timeout, a 5xx, or an unparseable body."""
    failing, broken = set(failing), set(broken)
    monkeypatch.setattr(sys, "argv", ["mrs.py", arg_str])
    monkeypatch.setattr(
        mrs, "_run",
        lambda cmd, timeout=25: subprocess.CompletedProcess(cmd, 0, json.dumps(rows), ""),
    )

    def _api(endpoint, timeout=10):
        if endpoint.endswith("/approvals"):
            return {"approved": True, "approved_by": []}
        if "/pipelines/" in endpoint:
            return [{"name": "phpstan2"}]
        iid = endpoint.rsplit("/", 1)[-1]
        if int(iid) in broken:
            return None
        status = "failed" if int(iid) in failing else "success"
        return {"head_pipeline": {"status": status, "id": iid}, "changes_count": "3"}

    monkeypatch.setattr(mrs, "_api_json", _api)
    monkeypatch.setattr(mrs, "_watched_iids", lambda *a, **k: set())
    assert mrs.main() == 0
    cap = capsys.readouterr()
    return cap.out, cap.err


def test_an_mr_whose_detail_fetch_failed_is_not_reported_as_healthy(
    monkeypatch, capsys,
) -> None:
    """The transient-failure version of the same defect, well inside the cap.

    `_fetch_mr_detail` returns {} on any failure, so `_pipeline` is "" and
    `_is_failing` is False — the MR is dropped and the board renders an
    unqualified all-clear over an MR whose state it never learned.
    """
    rows = [_row(i) for i in range(1, 6)]
    out, _err = _drive_with_failures(monkeypatch, capsys, rows, "failed", broken={4})

    assert "not checked" in out, (
        "an MR whose pipeline lookup failed was silently treated as green"
    )
    assert "1 of 5 MRs not checked" in out


def test_a_failed_lookup_does_not_blame_the_cap_it_did_not_hit(monkeypatch, capsys) -> None:
    """Five MRs, cap forty. Naming the cap as the cause would be a confidently
    wrong reason, which this repo rates worse than silence."""
    rows = [_row(i) for i in range(1, 6)]
    out, _err = _drive_with_failures(monkeypatch, capsys, rows, "failed", broken={4})

    assert "1 of 5 MRs not checked" in out
    assert "Enrichment cap" not in out, (
        "the cap never bound at 5 MRs — naming it states a cause that was not "
        "measured, and offers an escape that cannot work"
    )
    assert "SUPERTOOL_ENRICH_CAP" not in out


# ---------------------------------------------------------------------------
# The machine-readable feed. `failed,iids` is what pipes into the watch
# supervisor, so it is the silent-omission channel with the longest reach.
# ---------------------------------------------------------------------------

def test_failed_iids_keeps_stdout_a_bare_feed_and_discloses_on_stderr(
    monkeypatch, capsys,
) -> None:
    rows = [_row(i) for i in range(1, CAP + 6)]
    out, err, fetched = _drive(
        monkeypatch, capsys, rows, "failed,iids", failing={2, CAP + 3},
    )

    assert len(fetched) == CAP
    assert out.split() == ["2"], "the id feed must stay parseable — ids and nothing else"
    assert "not checked" in err, "the omission must reach a human on some stream"


def test_a_fully_checked_iids_feed_writes_nothing_to_stderr(monkeypatch, capsys) -> None:
    rows = [_row(i) for i in range(1, 6)]
    out, err, fetched = _drive(monkeypatch, capsys, rows, "failed,iids", failing={2})

    assert len(fetched) == 5
    assert out.split() == ["2"]
    assert err == ""


# ---------------------------------------------------------------------------
# The default board's existing disclosure must not regress.
# ---------------------------------------------------------------------------

def test_the_default_board_still_discloses_the_cap(monkeypatch, capsys) -> None:
    rows = [_row(i) for i in range(1, CAP + 6)]
    out, _err, fetched = _drive(monkeypatch, capsys, rows, "")

    assert len(fetched) == CAP
    assert "5 of 45 MRs not checked" in out
    assert "Enrichment cap is 40" in out
    assert "SUPERTOOL_ENRICH_CAP" in out


def test_the_cap_notice_is_printed_above_the_table(monkeypatch, capsys) -> None:
    """Header position, not footer-only — a footer is lost by exactly the
    consumer that truncates the output (#635)."""
    rows = [_row(i) for i in range(1, CAP + 6)]
    out, _err, _fetched = _drive(monkeypatch, capsys, rows, "")

    lines = out.splitlines()
    notice = next(i for i, ln in enumerate(lines) if "not checked" in ln)
    first_row = next(i for i, ln in enumerate(lines) if "!1 " in ln)
    assert notice < first_row
