"""main()'s `repo:` pre-pass restores SUPERTOOL_REPO after the call (#1962).

`_supertool.py`'s `repo:` pre-pass writes `os.environ["SUPERTOOL_REPO"]`
directly, with nothing to restore it -- a process-global mutation
`monkeypatch` never performed and therefore cannot undo. A test that drives
`main()` in-process leaves the variable live for every test that runs after
it in the same pytest worker: `presets/watch/transport.repo_slug()` and any
other `_repo_target` consumer then answers about whatever repository leaked
in, silently, in a file that never mentioned `repo:` at all. Reported and
diagnosed by the lane implementing #1953/#1952/#1951 (PR #1961); this file is
the fix's own direct regression test, in-process rather than the
subprocess-driven cross-test probe `tests/test_gl_repo_target_676.py`
already carries for the same mechanism (kept there, per #1986's brief, at
its `tmp_path` location rather than moved).

#1986 layers a second, call-scoped marker (`SUPERTOOL_REPO_FROM_OP`) on top
of the same pre-pass, so this file also asserts it does not outlive the
call -- restoring one and not the other would still leak a bit of state a
later write route could misread.
"""
from __future__ import annotations

import os

import pytest

import supertool


@pytest.fixture(autouse=True)
def clean_repo_env(monkeypatch):
    """Belt-and-suspenders: a failure in the fix under test must not itself
    leak into whatever test runs after this file."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.delenv("SUPERTOOL_REPO_FROM_OP", raising=False)
    yield
    os.environ.pop("SUPERTOOL_REPO", None)
    os.environ.pop("SUPERTOOL_REPO_FROM_OP", None)


def test_main_exports_the_target_during_the_call_and_restores_it_after(monkeypatch) -> None:
    """Paired must-fire / must-not-fire in one fixture (per the brief): a
    must-not-fire assertion alone passes just as well against a broken
    harness that never calls dispatch at all, so the same test also checks
    that the call really did export the value while it ran.
    """
    seen: dict[str, str | None] = {}

    def fake_dispatch(arg: str) -> str:
        # must-fire control: during the call, dispatch sees both the target
        # and the from-op marker main()'s pre-pass is supposed to export.
        seen["repo_during_call"] = os.environ.get("SUPERTOOL_REPO")
        seen["marker_during_call"] = os.environ.get("SUPERTOOL_REPO_FROM_OP")
        return ""

    monkeypatch.setattr(supertool, "dispatch", fake_dispatch)
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    assert "SUPERTOOL_REPO" not in os.environ  # sanity precondition

    rc = supertool.main(["repo:owner/name", "gh-issue:1"])

    assert rc == 0
    assert seen.get("repo_during_call") == "owner/name"
    assert seen.get("marker_during_call") == "1"

    # must-not-fire: neither variable outlives the call that set it.
    assert "SUPERTOOL_REPO" not in os.environ
    assert "SUPERTOOL_REPO_FROM_OP" not in os.environ


def test_main_restores_a_prior_ambient_value_rather_than_deleting_it(monkeypatch) -> None:
    """If the process already had SUPERTOOL_REPO set before this call --
    from an outer shell export, say -- main() must put that value BACK, not
    just clear the variable outright. Popping unconditionally would turn a
    caller's own ambient export into a one-shot value silently consumed."""
    monkeypatch.setenv("SUPERTOOL_REPO", "outer/ambient")
    monkeypatch.setattr(supertool, "dispatch", lambda arg: "")
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    rc = supertool.main(["repo:owner/name", "gh-issue:1"])

    assert rc == 0
    assert os.environ.get("SUPERTOOL_REPO") == "outer/ambient"
