"""`gh-pr:N:diff:PATH` served one commit's hunks and stopped (#1068).

Two independent defects, one visible symptom:

* **The wrong fetch.** `_run_diff` asked `gh pr diff N --patch`, which is
  format-patch: one section per COMMIT, so a file touched twice appears twice.
  `gh pr diff N` with no flag is the net three-dot diff — the merge result,
  which is the thing a reviewer merges. The net diff is what GitHub already
  serves; nothing needed reassembling.
* **The silent truncation.** `_one_file` took `next(...)` — the FIRST record
  matching the path — and rendered it as the file's diff. A second record was
  dropped with no disclosure of any kind, so superseded code rendered as
  current and a fix landed in a later commit was invisible. That `next()` is
  the absence this repo keeps shipping: a partial read rendering as a complete
  one, inside the merge gate's own reading tool.

So the fetch is fixed AND the render is made total: whatever the source hands
it, every entry for a path is shown and a multi-entry path says so. The second
half is the belt — if any future source ever yields per-commit records again,
the op discloses instead of truncating.

Every assertion below is on behaviour that does not exist yet.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr_diff = _load("presets/github/_pr_diff.py", "github_pr_diff_1068")


# What `gh pr diff N --patch` produces for one file touched by two commits:
# the same path, twice, second superseding the first.
PER_COMMIT = """\
From aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa Mon Sep 17 00:00:00 2001
Subject: [PATCH 1/2] first pass

diff --git a/src/alpha.py b/src/alpha.py
index 1111111..2222222 100644
--- a/src/alpha.py
+++ b/src/alpha.py
@@ -1,3 +1,3 @@
 ctx
-OLD = 1
+MID = 2
From bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb Mon Sep 17 00:00:00 2001
Subject: [PATCH 2/2] second pass

diff --git a/src/alpha.py b/src/alpha.py
index 2222222..3333333 100644
--- a/src/alpha.py
+++ b/src/alpha.py
@@ -1,3 +1,3 @@
 ctx
-MID = 2
+FINAL = 3
"""

SINGLE = """\
diff --git a/src/beta.py b/src/beta.py
index 1111111..2222222 100644
--- a/src/beta.py
+++ b/src/beta.py
@@ -1,3 +1,3 @@
 ctx
-OLD = 1
+NEW = 2
"""


# ---------------------------------------------------------------------------
# the render is total: no entry is dropped, and a partial view discloses itself
# ---------------------------------------------------------------------------

def test_every_entry_for_a_path_is_shown_not_just_the_first() -> None:
    """The load-bearing one. `+FINAL = 3` is the code at HEAD; a reviewer who
    is shown `+MID = 2` alone reviews code that no longer exists."""
    text, code = pr_diff.render(pr_diff.parse(PER_COMMIT), header=["# PR #1"],
                                path="src/alpha.py")
    assert code == 0, text
    assert "+MID = 2" in text, text
    assert "+FINAL = 3" in text, (
        "the second entry's hunks are absent — this is #1068 exactly:\n" + text
    )


def test_a_multi_entry_path_discloses_how_many_entries_it_assembled() -> None:
    """Silence is the defect. Showing both and saying nothing about where the
    seam is would still leave a reader unable to tell superseded lines from
    current ones."""
    text, _ = pr_diff.render(pr_diff.parse(PER_COMMIT), header=["# PR #1"],
                             path="src/alpha.py")
    assert "2 entries" in text, text


def test_a_single_entry_path_carries_no_multi_entry_note() -> None:
    """A disclosure printed on every file is noise, and noise is how a real
    disclosure gets skimmed past."""
    text, _ = pr_diff.render(pr_diff.parse(SINGLE), header=["# PR #1"],
                             path="src/beta.py")
    assert "entries" not in text, text


def test_the_summary_lists_a_twice_touched_file_once_with_summed_stat() -> None:
    """The file list reading `alpha.py +1 -1` twice is not two files; a total
    of `2 files` for one file is the same misreport one level up."""
    text, _ = pr_diff.render(pr_diff.parse(PER_COMMIT), header=["# PR #1"])
    assert text.count("src/alpha.py") == 1, text
    assert "1 files" in text or "1 file," in text, text
    assert "+2 -2" in text, text


def test_a_file_added_then_modified_is_still_an_addition() -> None:
    """Merging entries merges their statuses, and a file created by this PR is
    an `A` however many times a later commit came back to it. `M` would send a
    reviewer looking for a base version that does not exist."""
    (one,) = pr_diff.coalesce(pr_diff.parse(ADDED_THEN_MODIFIED))
    assert one["status"] == "A", one
    assert one["entries"] == 2, one
    assert (one["added"], one["removed"]) == (3, 1), one

    text, _ = pr_diff.render(pr_diff.parse(ADDED_THEN_MODIFIED),
                             header=["# PR #1"])
    row = [l for l in text.splitlines() if "src/new.py" in l]
    assert row and row[0].strip().startswith("A "), text


def test_a_file_modified_then_deleted_is_a_deletion() -> None:
    """The last entry wins for removal: whatever the earlier commits did to it,
    the file is not there at head."""
    (one,) = pr_diff.coalesce(pr_diff.parse(MODIFIED_THEN_DELETED))
    assert one["status"] == "D", one


ADDED_THEN_MODIFIED = """\
diff --git a/src/new.py b/src/new.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,2 @@
+FIRST = 1
+SECOND = 2
diff --git a/src/new.py b/src/new.py
index 1111111..2222222 100644
--- a/src/new.py
+++ b/src/new.py
@@ -1,2 +1,2 @@
-SECOND = 2
+SECOND = 3
"""

MODIFIED_THEN_DELETED = """\
diff --git a/src/gone.py b/src/gone.py
index 1111111..2222222 100644
--- a/src/gone.py
+++ b/src/gone.py
@@ -1,2 +1,2 @@
-A = 1
+A = 2
diff --git a/src/gone.py b/src/gone.py
deleted file mode 100644
index 2222222..0000000
--- a/src/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-A = 2
"""


# ---------------------------------------------------------------------------
# the fetch: net diff, not a per-commit replay
# ---------------------------------------------------------------------------

_VIEW_JSON = json.dumps({
    "number": 1057, "title": "t", "headRefName": "h", "baseRefName": "master",
    "url": "https://example.invalid/pull/1057",
})


def _completed(stdout: str, code: int = 0):
    return subprocess.CompletedProcess(args=["gh"], returncode=code,
                                       stdout=stdout, stderr="")


def test_the_diff_route_asks_for_the_net_diff_not_a_per_commit_patch(
        monkeypatch) -> None:
    """`--patch` is format-patch: one section per commit. That is the source of
    the duplicate entries, and the net diff is one flag away."""
    pr = _load("presets/github/pr.py", "github_pr_1068")
    calls: list[list[str]] = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        if "diff" in argv:
            return _completed(SINGLE)
        return _completed(_VIEW_JSON)

    monkeypatch.setattr(pr.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["pr.py", "1057", "diff"])
    assert pr.main() == 0

    diffs = [c for c in calls if c[:3] == ["gh", "pr", "diff"]]
    assert diffs, f"no `gh pr diff` call in {calls}"
    assert "--patch" not in diffs[0], (
        f"--patch replays the file once per commit: {diffs[0]}"
    )
