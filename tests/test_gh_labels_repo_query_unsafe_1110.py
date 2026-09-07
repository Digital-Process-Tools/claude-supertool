"""#1110 finding 3: `gh-labels:tally`'s repo target is not query-escaped.

Core's shape check on a `repo:` op (`presets/_repo_target.py::owner_repo`) is
"exactly one `/`, both halves non-empty" -- no character-set check. Every
other value interpolated into `search_query`'s search term goes through
`_QUERY_UNSAFE` first: the label prefix at `parse_args` and each label name at
`tally_main`. The `repo:{repo}` term was the one place that check was
missing, so `SUPERTOOL_REPO='owner/name is:public'` (a real shape that passes
`owner_repo`'s check -- one `/`, two non-empty halves) walks straight into the
query as extra, attacker-shaped search syntax.

Operator-supplied rather than remote -- `repo:` in this call is typed by
whoever runs supertool, not by the label author -- so this is a consistency
gap against the check already applied to the other two inputs, not a trust
boundary crossing. Fixed the same way #1105's neighbours were: refused, not
escaped, matching `_QUERY_UNSAFE`'s own reasoning that GitHub's search grammar
has no documented escape for a quote inside a term.

Positive control: a normal repo target must still tally normally -- the
refusal is not the whole file learning to fail.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


labels = _load("presets/github/labels.py", "github_labels_1110")

LABELS = [
    {"name": "cohort-1", "description": ""},
    {"name": "cohort-2", "description": ""},
]


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _refusing_gh(argv, *a: Any, **kw: Any):
    raise AssertionError(
        f"tally_main called gh with an unescaped repo target instead of "
        f"refusing first: {argv!r}")


def _clean_gh(argv, *a: Any, **kw: Any):
    joined = " ".join(argv)
    if "search/issues" in joined:
        return _Completed("0")
    if "issue list" in joined:
        return _Completed("[]")
    return _Completed("", returncode=1, stderr="unexpected")


def test_a_query_unsafe_repo_target_is_refused_before_any_search(
    monkeypatch,
) -> None:
    monkeypatch.setattr(labels.subprocess, "run", _refusing_gh)
    rc = labels.tally_main(
        "cohort-", LABELS,
        'Digital-Process-Tools/claude-supertool" is:public')

    assert rc == 1, "an unsafe repo target must not tally as if nothing was wrong"


def test_the_issues_own_repro_needs_no_quote_character_at_all(
    monkeypatch,
) -> None:
    """The auditor's finding on this same fix: `repo:{repo}` is interpolated
    UNQUOTED in `search_query` (unlike the prefix and every label name, which
    sit inside `label:"{n}"`), so a bare SPACE is enough to inject extra
    search syntax -- no `"` or newline required. `_QUERY_UNSAFE` alone
    (`["\r\n]`) does not see this at all: this is the issue's own repro
    verbatim, `SUPERTOOL_REPO='owner/name is:public'`, and it must be
    refused on shape, not on a quote/newline check that was never going to
    fire for this input."""
    monkeypatch.setattr(labels.subprocess, "run", _refusing_gh)
    rc = labels.tally_main(
        "cohort-", LABELS,
        "Digital-Process-Tools/claude-supertool is:public")

    assert rc == 1, (
        "the exact shape from the issue's own repro -- no quote, no "
        "newline, just a space -- was tallied as if the repo target were "
        "clean, because it was interpolated unquoted into the query"
    )


def test_a_clean_repo_target_is_not_refused(monkeypatch, capsys) -> None:
    """Positive control: the check must not swallow the ordinary case."""
    monkeypatch.setattr(labels.subprocess, "run", _clean_gh)
    rc = labels.tally_main(
        "cohort-", LABELS, "Digital-Process-Tools/claude-supertool")
    out = capsys.readouterr().out

    assert rc == 0
    assert "refusing" not in out.lower()
