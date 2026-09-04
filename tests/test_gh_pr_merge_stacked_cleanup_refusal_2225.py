"""`gh-pr-merge|cleanup` deleted the head branch even when an open pull
request was stacked on it, closing that pull request as a side effect (#2225).

`stacked_followups()` already knows the answer before cleanup runs -- main()
computes it for the `## Stacked follow-up` section first -- but `run_cleanup`
never saw it, so the remote-branch delete went ahead regardless. This pins the
safer-default fix the issue itself named as matching how the op treats every
other irreversible-ish step: the remote branch item refuses rather than
deletes when a stacked pull request is known, or not known to be absent.

The local worktree and local branch items are unaffected -- only the remote
delete is what GitHub reacts to by closing a PR based on it -- so both stay
`CLEAN_DONE` in every case here; only "remote branch" changes.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_merge.py"
_spec = importlib.util.spec_from_file_location(
    "gh_pr_merge_stacked_cleanup_2225", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _sibling(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_containment = _sibling(
    "test_gh_pr_merge_cleanup_containment_1280_1281_1282.py",
    "gh_pr_merge_containment_for_2225")
_main = _sibling("test_gh_pr_merge_main_950.py", "gh_pr_merge_main_for_2225")


def _install(monkeypatch, s) -> None:
    """`_containment.install`, but against **this file's own** `m` — the
    containment file loads `pr_merge.py` a second time under its own module
    name, and patching that copy would leave this file's `m.run_cleanup`
    talking to the real `_git`/`_gh` rather than the shim."""
    monkeypatch.setattr(m, "_git", s.git)
    monkeypatch.setattr(m, "_gh", s.gh)
    monkeypatch.setattr(m, "_worktrees_for_branch", s.worktrees_for)
    monkeypatch.setattr(m, "_worktree_state", s.worktree_state)
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)


def _clean(shim, head, **kw):
    kw.setdefault("merged", True)
    kw.setdefault("cross_repo", False)
    kw.setdefault("default_branch", "master")
    kw.setdefault("head_oid", _containment.OID)
    return m.run_cleanup(head, **kw)


def _states(rows):
    return {item: state for item, state, _ in rows}


def test_a_known_stacked_pull_request_refuses_the_remote_delete(
        monkeypatch) -> None:
    s = _containment.Shim()
    _install(monkeypatch, s)
    rows = _clean(s, "fix/958", stack_state=m.STACK_FOUND)
    st = _states(rows)
    assert st["remote branch"] == m.CLEAN_REFUSED
    assert s.deletes() == [], "the delete must never reach the API"
    detail = [d for i, _s, d in rows if i == "remote branch"][0]
    assert "Stacked follow-up" in detail


def test_an_unread_stack_search_also_refuses_the_remote_delete(
        monkeypatch) -> None:
    """UNKNOWN is not established-absent, so it is treated the same as found --
    the same 'unestablished refuses' posture `run_cleanup` already applies to
    `cross_repo` and `default_branch` (#1281/#1292)."""
    s = _containment.Shim()
    _install(monkeypatch, s)
    rows = _clean(s, "fix/958", stack_state=m.UNKNOWN)
    st = _states(rows)
    assert st["remote branch"] == m.CLEAN_REFUSED
    assert s.deletes() == []


def test_no_stacked_pull_request_still_deletes_the_remote_branch(
        monkeypatch) -> None:
    """The paired must-fire case: nothing here changes ordinary cleanup."""
    s = _containment.Shim()
    _install(monkeypatch, s)
    rows = _clean(s, "fix/958", stack_state=m.STACK_NONE)
    st = _states(rows)
    assert st["remote branch"] == m.CLEAN_DONE
    assert len(s.deletes()) == 1


def test_a_refused_remote_branch_does_not_block_the_local_items(
        monkeypatch) -> None:
    """Only the remote delete is what GitHub reacts to -- local cleanup is
    unaffected by an open pull request the reader has not seen yet."""
    s = _containment.Shim(worktrees=["/w/fix"], branch_exists=True)
    _install(monkeypatch, s)
    rows = _clean(s, "fix/958", stack_state=m.STACK_FOUND)
    st = _states(rows)
    assert st["remote branch"] == m.CLEAN_REFUSED
    assert st["local worktree"] == m.CLEAN_DONE
    assert st["local branch"] == m.CLEAN_DONE


def test_omitting_stack_state_refuses_rather_than_silently_deleting(
        monkeypatch) -> None:
    """A caller who forgets to pass it gets the same unestablished refusal
    as every other omitted keyword here -- never a delete it cannot undo."""
    s = _containment.Shim()
    _install(monkeypatch, s)
    rows = _clean(s, "fix/958")
    assert _states(rows)["remote branch"] == m.CLEAN_REFUSED
    assert s.deletes() == []


def test_a_changelog_fragment_exists() -> None:
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(2225)


# ---------------------------------------------------------------------------
# the printed (non-`|cleanup`) arm of main() -- the same hazard, unguarded
# ---------------------------------------------------------------------------
#
# `run_cleanup` is not the only place that hands over a remote delete: without
# `|cleanup`, main() prints a ready-to-paste `gh api -X DELETE` command for the
# reader instead of running it, and #1281/#1292 already made that arm mirror
# every other `run_cleanup` gate (`cross_repo`, `default_branch`) so the two
# could never disagree about when a delete is safe. `stack_state` was not
# mirrored into it, so the printed arm kept handing over a command that closes
# a stacked pull request exactly as irreversibly as the gated `|cleanup` path
# used to -- the reader has seen the `## Stacked follow-up` warning immediately
# above it, but a warning read is not a refusal, and every sibling gate here
# earns its own explicit refusal rather than relying on the reader noticing
# a paragraph above.


def _install_main(monkeypatch, h, *, argv):
    """`_main._install`, but against **this file's own** `m` -- exactly the
    reason `_install` above exists for `_containment`: the sibling module
    loads `pr_merge.py` a second time under its own module name, and patching
    that copy leaves this file's `m.main` talking to the real `gh`."""
    monkeypatch.setattr(m, "_gh", h.gh)
    monkeypatch.setattr(m, "_gh_json", h.gh_json)
    monkeypatch.setattr(m, "_load_pr_module", h.pr_module)
    monkeypatch.setattr(m.subprocess, "run", h.subprocess_run)
    monkeypatch.setattr(_main.sys, "argv", ["pr_merge.py"] + argv)
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_CONFIRM", "1")


def test_a_stacked_pull_request_gets_no_printed_delete_command_either(
        monkeypatch, capsys) -> None:
    h = _main._Harness(_main._pr(), stack_prs=[
        {"number": 961, "title": "follow-up", "url": "https://x/961"}])
    _install_main(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    assert "gh api -X DELETE" not in out
    assert "Stacked follow-up" in out
    assert "stacked" in out.lower() or "stack" in out.lower()


def test_an_unread_stack_search_also_gets_no_printed_delete_command(
        monkeypatch, capsys) -> None:
    h = _main._Harness(_main._pr(), fail_json={"stack"})
    _install_main(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    assert "gh api -X DELETE" not in out


def test_no_stacked_pull_request_still_prints_the_delete_command(
        monkeypatch, capsys) -> None:
    """The paired must-fire case: an empty board leaves the printed arm
    exactly as it was -- this is the same behaviour
    `test_an_ordinary_same_repo_head_still_gets_its_commands` in
    `test_gh_pr_merge_main_950.py` already pins; restated here so the pair
    lives beside the two refusal cases above rather than only in another
    file."""
    h = _main._Harness(_main._pr())
    _install_main(monkeypatch, h, argv=["944"])
    m.main()
    out = capsys.readouterr().out
    assert "gh api -X DELETE" in out
