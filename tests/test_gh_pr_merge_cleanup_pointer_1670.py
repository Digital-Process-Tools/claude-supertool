"""The no-cleanup section: what it claims about `|cleanup`, and the slug it prints (#1670).

Two properties of the arm `main()` renders when `cleanup` was **not** asked for,
and neither is pinned on the wording:

* the header advertises a token, and that token is one `parse_argv` really turns
  into `cleanup=True`. A test on the header string alone would stay green if the
  route were deleted tomorrow; this one reddens. The complaint that opened #1670
  was a reader stopping at `## Cleanup - not run by this op` and concluding the
  op cannot do it, 69 lines above the pointer saying it can.
* the printed `gh api -X DELETE` names the repository this same receipt already
  names twice - in its own header and in its `URL:` line - while the *executed*
  path keeps going through `_repo_target.api_path` unchanged. A printed command
  and an argv are two consumers of one function, and #1670 is the third issue in
  this file's history where the two got conflated. `api_path` replaces gh's
  `{owner}`/`{repo}` rather than accompanying them (#1281), and filling a slug in
  for the printed one must not weaken that.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path


def _load_sibling(filename: str, name: str):
    """Import another test module by path (same reason as #950's own copy):
    `tests/` is not on `sys.path` under every invocation."""
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sib = _load_sibling("test_gh_pr_merge_main_950.py", "pr_merge_main_for_1670")
m = sib.m
HEAD = "fix/924"
HEAD_OID = "c" * 40


def _run(monkeypatch, capsys, *, over=None, target=None, identity=None) -> str:
    h = sib._Harness(sib._pr(**(over or {})))
    sib._install(monkeypatch, h, argv=["944|force"])
    if target is None:
        monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    else:
        monkeypatch.setenv("SUPERTOOL_REPO", target)
    if identity is not None:
        monkeypatch.setattr(m, "_repo_identity", lambda: identity)
    m.main()
    return capsys.readouterr().out


def _section(out: str) -> list:
    lines = out.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("## Cleanup")]
    assert len(starts) == 1, f"expected one Cleanup header, got {starts}"
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ") or lines[i].startswith("[result]")),
               len(lines))
    return lines[start:end]


def _delete_line(out: str) -> str:
    hits = [line for line in _section(out) if "-X DELETE" in line]
    assert len(hits) == 1, f"expected one printed DELETE, got {hits}"
    return hits[0]


# ---------------------------------------------------------------------------
# the header advertises a route that exists
# ---------------------------------------------------------------------------

def test_the_no_cleanup_header_names_a_token_that_really_runs_cleanup(
        monkeypatch, capsys) -> None:
    """The property, not the phrasing: whatever the header offers in backticks
    has to be a token `parse_argv` accepts and turns into `cleanup=True`. Delete
    the route and this reddens; reword the header and it does not."""
    header = _section(_run(monkeypatch, capsys))[0]
    offered = [tok.strip("|") for tok in re.findall(r"`([^`]+)`", header)]
    assert offered, f"the header offers no route at all: {header!r}"
    runs = [tok for tok in offered
            if m.parse_argv(["944", tok])[4] == ""
            and m.parse_argv(["944", tok])[3] is True]
    assert runs, (f"header {header!r} offers {offered}, none of which parse_argv "
                  f"turns into cleanup=True")


def test_an_arm_that_refuses_the_delete_says_the_offered_route_refuses_too(
        monkeypatch, capsys) -> None:
    """A cross-repo head is refused by `run_cleanup` on every item, so the
    section must not leave the header's pointer standing as the way through.
    The refusal is read off `run_cleanup` itself rather than asserted, so the
    prose and the behaviour cannot drift apart."""
    out = _run(monkeypatch, capsys, over={"isCrossRepository": True})
    section = " ".join(_section(out))
    assert "No delete command is printed" in section, section
    rows = m.run_cleanup(HEAD, merged=True, cross_repo=True,
                         default_branch="master", head_oid=HEAD_OID)
    assert all(state != m.CLEAN_DONE for _i, state, _d in rows), rows
    assert re.search(r"cleanup`[^.]*refus", section), section


# ---------------------------------------------------------------------------
# the printed path names the repository, the executed one does not change
# ---------------------------------------------------------------------------

def test_the_printed_delete_names_the_repo_the_receipt_already_named(
        monkeypatch, capsys) -> None:
    out = _run(monkeypatch, capsys)
    assert f"repos/{sib.REPO}/git/refs/heads/{HEAD}" in _delete_line(out)
    assert "{owner}" not in out and "{repo}" not in out, out


def test_the_executed_path_still_carries_ghs_own_placeholders(
        monkeypatch) -> None:
    """The half that must not move. `gh api` expands these from the cwd's
    remote and a repo target has to override them, so argv keeps the
    placeholders however the printed line is rendered."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    assert (m._repo_target.api_path("git/refs/heads/" + HEAD)
            == "repos/{owner}/{repo}/git/refs/heads/" + HEAD)


def test_a_repo_target_is_still_replaced_rather_than_accompanied(
        monkeypatch) -> None:
    """A target names the repository the call is about; the cwd's slug is not
    it, and must not appear beside it (#1281). Under a target the printed path
    and the executed one are the same string, which is the cheapest statement
    that the two consumers have not been split apart.

    A unit rather than a `main()` render: #950's harness dispatches `gh_json`
    on `args[-1]`, and `gh_args()` appends `--repo OWNER/NAME` after the field
    list, so a targeted run through that harness never reaches the PR read."""
    monkeypatch.setenv("SUPERTOOL_REPO", "other/thing")
    suffix = "git/refs/heads/" + HEAD
    printed = m._repo_target.api_path_for_display(suffix, sib.REPO)
    assert printed == "repos/other/thing/" + suffix, printed
    assert sib.REPO not in printed, printed
    assert printed == m._repo_target.api_path(suffix), printed


def test_an_unreadable_slug_falls_back_to_the_placeholders(
        monkeypatch, capsys) -> None:
    """Three states, not two: a slug that did not come back is not a slug to
    build a path out of, and the placeholders are still a correct command."""
    out = _run(monkeypatch, capsys, identity=("", "master", ""))
    line = _delete_line(out)
    assert f"repos/{{owner}}/{{repo}}/git/refs/heads/{HEAD}" in line, line
