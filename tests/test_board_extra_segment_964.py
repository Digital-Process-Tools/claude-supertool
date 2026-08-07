"""#964 — a second `:` segment on a board op is discarded, and the board is printed anyway.

What the issue says, and what is actually true
----------------------------------------------
The issue is filed as "`gh-issues:label=lane:tracker-ops` returns 0 because the
label's own colon is tokenized", and its premise is that this repo's labels are
namespaced with a colon (`lane:tracker-ops`, `priority:medium`).

**They are not.** Verified live on 2026-08-07 against
`Digital-Process-Tools/claude-supertool` with an authenticated `gh`:

    $ gh label list --json name -q '.[].name'
    ... lane-ci-cost lane-containment lane-git-ops lane-release
        lane-tracker-ops lane-validators lane-watch
        priority-high priority-low priority-medium ...

Every lane label is spelled with a **hyphen**, and the hyphen form has always
worked — `gh-issues:label=lane-tracker-ops,state=open` returns the board. So the
headline claim ("every lane query is a silently empty board", "there were 50
issues I could not see") is false: no lane query anyone could actually run was
ever empty, because no label with a colon in it has ever existed here.

The defect underneath it is real, and is worse-shaped than the one filed
-----------------------------------------------------------------------
`{args}` hands every `:`-segment to the preset as a separate argv entry, and
`main()` in each board op reads `sys.argv[1]` and nothing else. So the trailing
segments are dropped in silence — and this is not confined to a value that
happens to contain a colon:

    $ supertool 'gh-issues:state=open:COMPLETEGARBAGE'
    PASS (4.53s)
    ... the entire open board ...

That is #864's defect verbatim — an unrecognised token discarded and the
unfiltered board printed as the answer — resurrected one layer up, where
`_filter_tokens`' refusal cannot see it. #864 fixed the tokenizer; nothing
guards the argv the tokenizer is handed. The refusal machinery is not bypassed
by a mangled token, it is bypassed by never being shown the token at all.

The colon-label case is one instance of that: `label=lane:tracker-ops` reaches
the op as `['label=lane', 'tracker-ops']`, the first half is a *valid*
`key=value`, so nothing refuses, and `--label lane` is queried instead.

Why refusal and not an escape
-----------------------------
There is no escape today (`label=lane\\:x` splits identically — verified), and
#806 declined to promote `\\:` into a supported contract. Refusing costs one
error, invents no grammar, and leaves a payload route open to be designed later
if a colon-bearing value ever needs querying (GitLab's `scope::value` is the
real candidate, not GitHub's). What must not survive is the case the issue is
right about: a filter the op could not apply in full, answered with a board.

`gh-job` is deliberately NOT covered: its grammar is genuinely positional
(`gh-job:ID:raw:START:END`), so argv[2:] is meaningful there.

The bar: every test below fails on the code as it stands.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PRESETS = Path(__file__).parent.parent / "presets"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_filter_tokens = _load("_filter_tokens.py", "filter_tokens_964")
issues = _load("github/issues.py", "github_issues_964")
prs = _load("github/prs.py", "github_prs_964")
mrs = _load("gitlab/mrs.py", "gitlab_mrs_964")


# ---------------------------------------------------------------------------
# the shared helper
# ---------------------------------------------------------------------------

def test_extra_segments_are_reported_as_not_applied() -> None:
    """The helper names every segment that never reached the filter parser."""
    err = _filter_tokens.extra_segments_error(
        ["issues.py", "label=lane", "tracker-ops"], "gh-issues"
    )
    assert err is not None
    assert "tracker-ops" in err
    assert "gh-issues" in err


def test_extra_segments_error_names_the_colon_as_the_cause() -> None:
    """An error that says what is wrong but not why is its own filing."""
    err = _filter_tokens.extra_segments_error(
        ["issues.py", "label=lane", "tracker-ops"], "gh-issues"
    )
    assert err is not None
    assert ":" in err
    assert "comma" in err.lower()


def test_a_single_segment_is_not_refused() -> None:
    """The ordinary call must stay ordinary."""
    assert _filter_tokens.extra_segments_error(
        ["issues.py", "label=lane-tracker-ops,state=open"], "gh-issues"
    ) is None
    assert _filter_tokens.extra_segments_error(["issues.py"], "gh-issues") is None


# ---------------------------------------------------------------------------
# the three board ops
# ---------------------------------------------------------------------------

_ROWS = [
    {"number": 1, "title": "t", "labels": [], "state": "OPEN",
     "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
     "author": {"login": "a"}, "comments": [], "url": "https://github.com/o/r/issues/1"}
]


def _never_called(*_a: Any, **_kw: Any):
    raise AssertionError(
        "the backend was queried — the op built a request from a filter it "
        "could not apply in full"
    )


@pytest.mark.parametrize(
    "mod, op, argv",
    [
        (issues, "gh-issues", ["issues.py", "label=lane", "tracker-ops"]),
        (issues, "gh-issues", ["issues.py", "state=open", "COMPLETEGARBAGE"]),
        (prs, "gh-prs", ["prs.py", "state=open", "COMPLETEGARBAGE"]),
        (mrs, "gl-mrs", ["mrs.py", "state=opened", "COMPLETEGARBAGE"]),
    ],
)
def test_a_dropped_segment_refuses_instead_of_printing_the_board(
    mod: Any, op: str, argv: list[str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A board built from a partly-applied filter is not the answer to the question."""
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(mod.subprocess, "run", _never_called)
    rc = mod.main()
    out = capsys.readouterr()
    assert rc == 1, f"{op} answered rc=0 with {argv[2]!r} silently dropped"
    assert argv[2] in out.err
    assert op in out.err


def test_the_refusal_beats_the_board_that_would_otherwise_have_rendered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Pin the harm: with the same stub, a well-formed call DOES render a board.

    Without this the refusal test could pass on an op that is simply broken.
    """
    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, json.dumps(_ROWS), "")

    monkeypatch.setattr(issues.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["issues.py", "state=open,nopipe"])
    assert issues.main() == 0
    assert "1 issue" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["issues.py", "state=open,nopipe", "GARBAGE"])
    rc = issues.main()
    out = capsys.readouterr()
    assert rc == 1
    assert "1 issue" not in out.out
