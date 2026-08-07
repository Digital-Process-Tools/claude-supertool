"""radar's GitHub tier: the PR board is a population radar could not watch (#859).

`presets/watch/tiers/` held exactly one tier and it spoke GitLab, so the board
this repository is actually merged from was the one thing radar could not see.
These cases drive `gh_prs.radar_report()` — the layer radar renders — with the
real API boundaries faked (`gh pr list --json` on stdout, and `branch.py`'s
three fetchers), so `_build_list_cmd`, the annotate pass and the snapshot
keying genuinely run. A test that handed the tier a pre-annotated list would
pass on code that never called GitHub at all, which is the trap #425 documents.

Every case here is a shape of the house defect: an absence produced by the tool
rendered as an absence in the world.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
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


radar = _module("watch_radar_859", WATCH_DIR / "radar.py")
tier = _module("watch_radar_gh_prs_859", WATCH_DIR / "tiers" / "gh_prs.py")


# ---------------------------------------------------------------------------
# fixtures — the two real boundaries
# ---------------------------------------------------------------------------

def _pr(number: int, rollup, sha: str = "a" * 40, **kw) -> dict:
    """One row as `gh pr list --json` returns it."""
    row = {
        "number": number,
        "title": f"pr {number}",
        "state": "OPEN",
        "author": {"login": "me"},
        "headRefName": f"fix/{number}",
        "baseRefName": "master",
        "headRefOid": sha,
        "labels": [],
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "statusCheckRollup": rollup,
        "additions": 1,
        "deletions": 1,
        "changedFiles": 1,
        "updatedAt": "2026-08-07T10:00:00Z",
        "createdAt": "2026-08-07T09:00:00Z",
        "assignees": [],
        "url": f"https://github.com/o/r/pull/{number}",
    }
    row.update(kw)
    return row


GREEN_LEG = {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS",
             "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9"}
RED_LEG = {"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE",
           "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9"}


class _Result:
    def __init__(self, out: str = "", err: str = "", code: int = 0):
        self.stdout, self.stderr, self.returncode = out, err, code


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Snapshots and pid files land in a scratch dir, never /tmp."""
    monkeypatch.setattr(tier.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tier.snapshot.transport, "STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch):
    """Nothing in this file may spawn a poller; a tier that does fails loudly."""
    monkeypatch.setattr(tier.dispatcher, "start_poller",
                        lambda *a, **k: pytest.fail("tier spawned a poller"))


@pytest.fixture(autouse=True)
def quiet_reconcile(monkeypatch):
    """Green-leg reconciliation is a network round per green PR.

    Off by default here so the cases about *other* things do not depend on it,
    and re-armed explicitly by the case that is about it.
    """
    monkeypatch.setattr(tier, "_reconcile_one", lambda p: ("", []))


def _fake_gh(monkeypatch, prs, code=0, err=""):
    """Fake `gh pr list`, and record the argv the tier actually built."""
    seen: list[list[str]] = []

    def run(cmd, *a, **k):
        seen.append(list(cmd))
        return _Result(json.dumps(prs), err, code)

    monkeypatch.setattr(tier.subprocess, "run", run)
    return seen


def _no_default_branch(monkeypatch):
    """Turn the default-branch member off for cases that are about PRs."""
    monkeypatch.setattr(tier, "default_branch_report",
                        lambda *a, **k: ([], True))


# ---------------------------------------------------------------------------
# 1. a filter the tool could not honour must never render as a filter that
#    matched everything (#486, and the live gh-prs behaviour this must not
#    inherit)
# ---------------------------------------------------------------------------

def test_unknown_filter_token_is_refused_not_silently_widened(state_dir, monkeypatch):
    seen = _fake_gh(monkeypatch, [_pr(1, [GREEN_LEG])])
    _no_default_branch(monkeypatch)

    with pytest.raises(tier.RadarError) as exc:
        tier.radar_report({"_arg": "milestone=v19", "_watch": lambda *a, **k: "alive"})

    assert "milestone" in str(exc.value)
    assert seen == [], "a refused filter must not reach GitHub at all"


def test_known_filters_still_pass_through(state_dir, monkeypatch):
    seen = _fake_gh(monkeypatch, [_pr(1, [GREEN_LEG])])
    _no_default_branch(monkeypatch)
    tier.radar_report({"_arg": "author=modular.system,state=open",
                       "_watch": lambda *a, **k: "alive"})
    assert any("--author" in cmd and "modular.system" in cmd for cmd in seen)


# ---------------------------------------------------------------------------
# 2. an empty rollup is *unchecked*, never green
# ---------------------------------------------------------------------------

def test_pr_with_no_checks_is_unchecked_not_green(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1, [GREEN_LEG]), _pr(2, [])])
    _no_default_branch(monkeypatch)

    lines, healthy = tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})
    text = "\n".join(lines)

    assert "1 unchecked" in text
    assert "2 green" not in text
    assert not healthy, "a board with an unread PR cannot claim it could tell the truth"


def test_a_green_pr_whose_legs_do_not_reconcile_is_not_green(state_dir, monkeypatch):
    """The tier consumes #724/#804/#837's arithmetic; it never re-sums."""
    _fake_gh(monkeypatch, [_pr(1, [GREEN_LEG])])
    _no_default_branch(monkeypatch)
    monkeypatch.setattr(tier, "_reconcile_one",
                        lambda p: ("UNVERIFIED", ["  4 read, 18 declared"]))

    lines, healthy = tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})
    text = "\n".join(lines)

    assert "1 green" not in text
    assert "1 unchecked" in text
    assert "UNVERIFIED" in text
    assert not healthy


def test_greens_past_the_reconcile_cap_are_disclosed(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(n, [GREEN_LEG]) for n in range(1, 5)])
    _no_default_branch(monkeypatch)

    lines, healthy = tier.radar_report(
        {"_arg": "", "reconcile_cap": 2, "_watch": lambda *a, **k: "alive"})
    text = "\n".join(lines)

    assert "2 of 4 green PRs" in text
    assert not healthy


# ---------------------------------------------------------------------------
# 3. snapshot keying — GitHub identity is (number, head sha), not number alone
# ---------------------------------------------------------------------------

def test_new_head_sha_with_the_same_rollup_is_a_change(state_dir, monkeypatch):
    # A *green* PR, deliberately: a red row is a standing problem and gets
    # re-printed regardless, which would let a snapshot key with no SHA in it
    # pass this test for the wrong reason.
    _no_default_branch(monkeypatch)
    _fake_gh(monkeypatch, [_pr(1, [GREEN_LEG], sha="a" * 40)])
    tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})

    _fake_gh(monkeypatch, [_pr(1, [GREEN_LEG], sha="b" * 40)])
    lines, _ = tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})

    assert not any(line.startswith("radar: no change") for line in lines), (
        "a push that landed a new head commit is a change even when the "
        "rollup state word is identical"
    )


def test_the_live_query_actually_asks_for_the_head_sha(state_dir, monkeypatch):
    """The keying above is fixture-deep unless `gh pr list` returns the field.

    Without `headRefOid` in the requested fields every real PR carries
    `head_sha: ""`, the delta collapses back to the rollup word, and the whole
    of `snap_entry`'s reasoning is true of the tests and false of the board.
    """
    seen = _fake_gh(monkeypatch, [])
    _no_default_branch(monkeypatch)
    tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})

    listing = [c for c in seen if c[:3] == ["gh", "pr", "list"]]
    assert listing, f"the tier never listed PRs: {seen}"
    fields = [c[i + 1] for c in listing for i, tok in enumerate(c) if tok == "--json"]
    assert any("headRefOid" in f for f in fields), fields


def test_an_unmoved_board_says_no_change(state_dir, monkeypatch):
    _no_default_branch(monkeypatch)
    _fake_gh(monkeypatch, [_pr(1, [GREEN_LEG])])
    tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})
    lines, _ = tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})
    assert any(line.startswith("radar: no change") for line in lines)


def test_two_filters_do_not_share_one_snapshot(state_dir, monkeypatch):
    _no_default_branch(monkeypatch)
    _fake_gh(monkeypatch, [_pr(1, [GREEN_LEG])])
    tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})
    lines, _ = tier.radar_report({"_arg": "label=bug",
                                  "_watch": lambda *a, **k: "alive"})
    assert any("cold start" in line for line in lines)


# ---------------------------------------------------------------------------
# 4. the one-filter invariant does not survive intact: watch state is keyed by
#    PR number with no repo (#673), so under a repo target coverage is UNKNOWN
# ---------------------------------------------------------------------------

def test_repo_target_makes_coverage_unknown_and_heals_nothing(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1, [RED_LEG])])
    _no_default_branch(monkeypatch)
    monkeypatch.setattr(tier._repo_target, "target", lambda: "other/repo")

    lines, healthy = tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})
    text = "\n".join(lines)

    assert "UNKNOWN" in text and "watch" in text
    assert "0 watched" not in text, (
        "an unknowable coverage rendered as zero watched is a number the "
        "reader would act on"
    )
    assert not healthy
    # `no_spawn` already fails the test if anything spawned.


# ---------------------------------------------------------------------------
# 5. a board GitHub refused to answer is not an empty board
# ---------------------------------------------------------------------------

def test_auth_failure_raises_and_writes_no_snapshot(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [], code=1, err="error: not logged in to any GitHub hosts")
    _no_default_branch(monkeypatch)

    with pytest.raises(tier.RadarError) as exc:
        tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})

    assert "gh auth login" in str(exc.value)
    assert list(state_dir.glob("*snapshot.json")) == [], (
        "nothing may be snapshotted from a population we could not read"
    )


def test_a_filter_that_matched_nothing_names_its_scope(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [])
    _no_default_branch(monkeypatch)
    lines, _ = tier.radar_report({"_arg": "label=bug",
                                  "_watch": lambda *a, **k: "alive"})
    text = "\n".join(lines)
    assert "label=bug" in text
    assert "No open PRs." not in text, (
        "'no PRs' and 'this filter matched nothing' are different facts"
    )


# ---------------------------------------------------------------------------
# 6. the default branch is a board member — the red-master case
# ---------------------------------------------------------------------------

def test_default_branch_with_no_run_is_never_green(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [])
    monkeypatch.setattr(tier.branch, "_head_commit",
                        lambda ref: ("c" * 40, 99999, ""))
    monkeypatch.setattr(tier.branch, "_run_list", lambda ref: ([], ""))
    monkeypatch.setattr(tier.branch, "_jobs_for", lambda rid: None)

    lines, healthy = tier.radar_report(
        {"_arg": "", "default_branch": "master", "_watch": lambda *a, **k: "alive"})
    text = "\n".join(lines)

    assert "master" in text
    assert tier.branch.NO_RUN in text
    assert not healthy


def test_default_branch_red_is_reported(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [])
    sha = "c" * 40
    monkeypatch.setattr(tier.branch, "_head_commit", lambda ref: (sha, 9999, ""))
    monkeypatch.setattr(tier.branch, "_run_list", lambda ref: ([{
        "workflowName": "tests", "headSha": sha, "databaseId": 5,
        "status": "completed", "conclusion": "failure", "event": "push",
        "createdAt": "2026-08-07T10:00:00Z", "attempt": 1,
    }], ""))
    monkeypatch.setattr(tier.branch, "_jobs_for",
                        lambda rid: [{"name": "tests", "status": "completed",
                                      "conclusion": "failure"}])
    monkeypatch.setattr(tier.branch, "_reconcile", lambda *a, **k: ("", []))

    lines, healthy = tier.radar_report(
        {"_arg": "", "default_branch": "master", "_watch": lambda *a, **k: "alive"})
    text = "\n".join(lines)

    assert tier.branch.NOT_GREEN in text
    assert "master" in text


def test_default_branch_unreadable_is_stated_not_dropped(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [])
    monkeypatch.setattr(tier.branch, "_head_commit",
                        lambda ref: ("", None, "ERROR: gh timed out"))

    lines, healthy = tier.radar_report(
        {"_arg": "", "default_branch": "master", "_watch": lambda *a, **k: "alive"})

    assert any("master" in line and "UNKNOWN" in line for line in lines)
    assert not healthy


# ---------------------------------------------------------------------------
# 7. inspection without action — the `watches` guarantee, for a tier
# ---------------------------------------------------------------------------

def test_radar_state_reads_and_spawns_nothing(state_dir, monkeypatch):
    def boom(*a, **k):
        pytest.fail("radar_state reached the network")

    monkeypatch.setattr(tier.subprocess, "run", boom)

    lines = tier.radar_state({"_arg": "label=bug", "default_branch": "master"})
    text = "\n".join(lines)

    assert "label=bug" in text
    assert "snapshot" in text
    # `no_spawn` fails the test if a poller was started.


def test_radar_state_op_does_not_run_the_tiers(monkeypatch, capsys):
    called: list[str] = []
    monkeypatch.setenv(radar.TIERS_ENV, json.dumps({"gh-prs": {}}))
    monkeypatch.setattr(radar.dispatcher, "reap_duplicate_pollers",
                        lambda *a, **k: called.append("reap") or [])
    monkeypatch.setattr(tier, "radar_report",
                        lambda *a, **k: called.append("report") or ([], True))

    rc = radar.main(["radar.py", "--state"])
    out = capsys.readouterr().out

    assert rc == 0
    assert called == [], "the read-only view must neither reap nor report"
    assert "gh-prs" in out


def test_radar_state_names_a_tier_it_cannot_resolve(monkeypatch, capsys):
    monkeypatch.setenv(radar.TIERS_ENV, json.dumps({"no-such-tier": {}}))
    rc = radar.main(["radar.py", "--state"])
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "no-such-tier" in out
    assert rc != 0


# ---------------------------------------------------------------------------
# 8. live — the fixtures cannot reach the shapes real GitHub produces
# ---------------------------------------------------------------------------

def _gh_ready() -> bool:
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True,
                           text=True, timeout=20, encoding="utf-8",
                           errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def test_live_board_over_this_repo(state_dir):
    # Probed inside the test, not at collection: a module-level `skipif` runs
    # `gh auth status` on every collection of the whole suite.
    if not _gh_ready():
        pytest.skip("gh not authenticated")
    lines, _healthy = tier.radar_report(
        {"_arg": "author=", "default_branch": "", "_watch": lambda *a, **k: "alive"})
    text = "\n".join(lines)
    assert "scope" in text
    assert "open" in text
