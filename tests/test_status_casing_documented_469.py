"""469 — gh-pr/gl-mr print 'Checks:'/'Mergeable:' in full mode and
'checks:'/'mergeable:' in :status mode. Both modes are already internally
consistent (Title Case throughout full, lowercase throughout :status) —
verified by reading presets/github/pr.py and presets/gitlab/mr.py, and
pinned by test_github_pr.py / test_gitlab_mr.py's existing slim-mode
assertions. Unifying the casing across modes would fight :status's
deliberate terse/byte-budget design and break those pinned tests for no
grep-portability gain, since the label vocabulary already differs between
modes anyway (`review` vs `Review decision`, `merge_commit` vs
`Merge commit`). The fix is documenting the convention so a consumer knows
which casing to grep for in which mode — these tests pin that the docs
say so.
"""
from __future__ import annotations

from pathlib import Path

DOCS_DIR = Path(__file__).parent.parent / "docs" / "presets"


def test_github_docs_state_the_casing_convention() -> None:
    text = (DOCS_DIR / "github.md").read_text(encoding="utf-8")
    assert "Casing:" in text
    assert "Title Case" in text
    assert "lowercase" in text


def test_gitlab_docs_state_the_casing_convention() -> None:
    text = (DOCS_DIR / "gitlab.md").read_text(encoding="utf-8")
    assert "Casing:" in text
    assert "Title Case" in text
    assert "lowercase" in text
