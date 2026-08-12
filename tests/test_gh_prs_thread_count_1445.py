"""#1445, the same defect one op over — `gh-prs` counted a failed fetch as zero.

`_enrich` asked `_fetch_review_threads` for each PR's threads, and that helper
returned `[]` for every failure. `sum(1 for t in [] ...)` is `0`, `_flags`
tested `if p.get("_unresolved", 0):`, and a PR whose GraphQL call was rate-
limited rendered **without** the `threads` flag — indistinguishable from a PR
that has no threads at all.

It is worse here than on `gh-pr`. The board is the read that decides which PR
to open, so an unread thread count does not merely omit a line: it removes the
one marker that would have made the reader look.

The fetcher is now the three-state one `gh-pr:N:threads` already used, and a
decline is its own flag. `threads?` and no flag are different claims, which is
the whole point.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "presets"))
sys.path.insert(0, str(ROOT / "presets" / "github"))
_spec = importlib.util.spec_from_file_location(
    "github_prs_1445", ROOT / "presets" / "github" / "prs.py")
assert _spec is not None and _spec.loader is not None
prs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prs)


def test_a_declined_thread_fetch_is_not_a_zero_count(monkeypatch) -> None:
    monkeypatch.setattr(prs, "_fetch_review_threads_detailed",
                        lambda url, n: (None, "HTTP 403: rate limit"),
                        raising=False)
    pr_list = [{"number": 1, "url": "u"}]
    prs._enrich(pr_list)
    assert pr_list[0]["_unresolved"] is None


def test_a_declined_thread_fetch_flags_itself_rather_than_looking_clean() -> None:
    out = prs._flags({"isDraft": False, "mergeable": "MERGEABLE",
                      "_unresolved": None})
    assert "threads?" in out


def test_a_real_zero_still_carries_no_thread_flag() -> None:
    assert prs._flags({"isDraft": False, "mergeable": "MERGEABLE",
                       "_unresolved": 0}) == ""


def test_a_real_count_still_carries_the_plain_flag() -> None:
    out = prs._flags({"isDraft": False, "mergeable": "MERGEABLE",
                      "_unresolved": 2})
    assert "threads" in out and "threads?" not in out
