"""#571: argv[0] of a labelled poller is a label, not an interpreter choice.

`transport.poller_argv` opened with `sys.executable or "python3"`, which reads
as interpreter resolution and is not: the program actually executed is passed
to `os.execve` as its *first* argument, separately, and
`dispatcher._exec_labelled` returns early on `if not sys.executable` before it
gets there. So the branch the fallback exists for — `sys.executable` empty —
never reaches an exec at all, and the string in argv[0] is never run by
anything.

The other half of the shape: `transport._labelled`, the matcher every reader
goes through, scans for a token ending in `watch/dispatcher.py` and checks the
*next* token is `poll`. It never looks at tokens[0]. So argv[0]'s content is
also not part of the signature.

Both halves together are why the fallback rescued nothing while costing
something real: #564 read this line as a Windows blast radius and had to be
re-diagnosed. These tests pin the two facts that make argv[0] free — it is
never executed, and it is never matched on — so the next reader does not have
to re-derive them from two files.
"""
from __future__ import annotations

import sys
from pathlib import Path

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

import transport  # noqa: E402  (the same module object dispatcher imports)

TRANSPORT_SOURCE = (WATCH_DIR / "transport.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# argv[0] is the running interpreter, with no name-shaped fallback behind it
# ---------------------------------------------------------------------------

def test_argv0_is_the_running_interpreter() -> None:
    argv = transport.poller_argv("gitlab-mr", "33248", [])
    assert argv[0] == sys.executable


def test_the_interpreter_name_fallback_is_gone() -> None:
    """Not "the string changed": the *shape* that read as PATH resolution is
    what #564 misdiagnosed, so pin that the whole expression is gone rather
    than that some line differs."""
    assert "sys.executable or" not in TRANSPORT_SOURCE
    assert "\n        sys.executable,\n" in TRANSPORT_SOURCE


def test_poller_argv_documents_that_argv0_is_label_only() -> None:
    """The reason the fallback could go is a two-file argument. If it is not
    written down at the call site, the next reader re-adds the fallback."""
    doc = transport.poller_argv.__doc__ or ""
    low = doc.lower()
    assert "argv[0]" in low
    assert "never executed" in low or "not executed" in low


# ---------------------------------------------------------------------------
# ...and dropping it did not break the matcher, which is the actual risk
# ---------------------------------------------------------------------------

def test_the_argv_poller_argv_builds_still_matches() -> None:
    """Round trip, common case: a normal non-empty sys.executable."""
    argv = transport.poller_argv("gitlab-mr", "33248", [])
    assert transport._labelled(argv) == ("gitlab-mr", "33248")


def test_the_argv_still_matches_with_an_only_filter() -> None:
    argv = transport.poller_argv("gitlab-mr", "33248", ["pipeline_failed", "merged"])
    assert transport._labelled(argv) == ("gitlab-mr", "33248")


def test_the_feed_watchers_argv_still_matches() -> None:
    argv = transport.poller_argv("gitlab-mr-feed", "author=@me,state=opened", [])
    assert transport._labelled(argv) == ("gitlab-mr-feed", "author=@me,state=opened")


def test_matching_does_not_depend_on_argv0_at_all() -> None:
    """The invariant that makes argv[0] free to change. If matching ever
    started reading tokens[0], this fails and #571's premise is void."""
    argv = transport.poller_argv("gitlab-mr", "33248", [])
    for label in ("", "python3", "/opt/weird/bin/python3.14", "supertool-watcher"):
        assert transport._labelled([label] + argv[1:]) == ("gitlab-mr", "33248")


def test_an_empty_sys_executable_still_produces_a_matchable_argv(monkeypatch) -> None:
    """The branch the fallback existed for. It cannot reach an exec — the
    dispatcher guards on `if not sys.executable` — but `watches`/`unwatch`
    still build this argv to match against a process table, so it must not
    become unmatchable."""
    monkeypatch.setattr(sys, "executable", "")
    argv = transport.poller_argv("gitlab-mr", "33248", [])
    assert argv[0] == ""
    assert transport._labelled(argv) == ("gitlab-mr", "33248")
