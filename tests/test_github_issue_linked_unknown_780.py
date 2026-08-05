"""`gh-issue` must say when it could not ask about linked PRs (#780 item 1).

The lookup had two silent failure paths: a non-zero `gh pr list` printed
nothing at all, and `except (TimeoutExpired, JSONDecodeError): pass` swallowed
the rest. Either way the reader sees no `Linked PRs` line and concludes **there
are none** — when the truth may be *I could not ask*.

That reading has a cost attached. "No linked PR" is the signal that an issue is
unclaimed, so the action it invites is delegating work. Work has already been
re-delegated onto an already-merged fix once on this tracker because a list did
not say.

Same class as #414, #445/#454, #459, #477/#482, #487, #486: an absence produced
by the tool, read as an absence in the world. `docs/validators.md` §"Declining
instead of guessing" — three states, not two.

Scope: item 1 only. Item 2 (the lookup uses a full-text `--search`, so prose
matching the number counts as a link) is a separate defect on the same lines,
re-scoped on the issue after measurement — the fix there is
`closedByPullRequestsReferences`, not the timeline.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "github" / "issue.py"
_spec = importlib.util.spec_from_file_location("github_issue_780", PRESET)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)


def _linked_section(monkeypatch, gh_behaviour) -> str:
    """Render just the linked-PR section, with `gh pr list` stubbed."""
    monkeypatch.setattr(issue, "_gh", gh_behaviour)
    buf = io.StringIO()
    with redirect_stdout(buf):
        issue._print_linked_prs(42)
    return buf.getvalue()


def test_non_zero_exit_says_unknown_rather_than_nothing(monkeypatch) -> None:
    """The path that printed nothing at all."""
    out = _linked_section(
        monkeypatch,
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    assert "unknown" in out.lower(), (
        "a failed lookup printed nothing, so the reader sees no Linked PRs "
        f"line and concludes there are none. Got:\n{out!r}"
    )
    assert "none" not in out.lower(), (
        f"'none' is a claim the op cannot support here:\n{out!r}"
    )


@pytest.mark.parametrize(
    "boom",
    [
        subprocess.TimeoutExpired(cmd="gh", timeout=1),
        json.JSONDecodeError("bad", "", 0),
    ],
    ids=["timeout", "malformed-json"],
)
def test_swallowed_exceptions_say_unknown(monkeypatch, boom) -> None:
    """The `except ...: pass` path."""
    def _raise(*a, **k):
        raise boom

    out = _linked_section(monkeypatch, _raise)

    assert "unknown" in out.lower(), (
        f"{type(boom).__name__} was swallowed silently. Got:\n{out!r}"
    )
    assert "none" not in out.lower()


def test_a_genuine_empty_result_still_says_none(monkeypatch) -> None:
    """The control: 'none' must remain available for a real, answered zero.

    This passes against the broken code too — deliberately. It is here so that
    a fix cannot satisfy the tests above by simply never saying 'none' again,
    which would trade one wrong answer for another.
    """
    out = _linked_section(
        monkeypatch,
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )

    assert "none" in out.lower()
    assert "unknown" not in out.lower()


def test_a_populated_result_still_lists_the_prs(monkeypatch) -> None:
    """The other control: the happy path is unchanged."""
    payload = json.dumps([
        {"number": 99, "title": "a fix", "state": "OPEN", "headRefName": "b"},
    ])
    out = _linked_section(
        monkeypatch,
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    assert "#99" in out
    assert "unknown" not in out.lower()
