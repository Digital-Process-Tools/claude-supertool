"""`gh-pr:N:diff` — the merge gate's one read, given a shape (#875).

No op returned a PR's diff, so every merge review fell back to `gh pr diff N`
piped into a hand-written filter. The raw command is the wrong shape three
ways, and this module is where the right one is pinned:

* **Whole-diff or nothing.** An 80-file mechanical sweep has four files of
  judgment in it. `:diff` is the file list with per-file stat; `:diff:PATH` is
  one file's hunks. Same walk `gh-job` already models with `:fail` / `:raw:-N`
  / `:grep:PATTERN`.
* **No sense of what is mechanical.** A file whose every hunk is the same edit
  repeated is a *note*, never a filter — it says where not to spend attention
  and it never removes a file from the list.
* **Every absence has to name itself.** A diff that could not be fetched must
  not render like an empty one, a path that is not in the diff must not render
  like a file with no changes, and a cap must disclose exactly what it
  withheld. Rendering any of those as silence puts a wrong all-clear inside
  the merge gate — `docs/validators.md` §"Declining instead of guessing".

Every assertion below is on behaviour that does not exist yet; the module
itself does not exist, so the file is red at import.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "_pr_diff.py"
_spec = importlib.util.spec_from_file_location("github_pr_diff_875", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
pr_diff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_diff)


TWO_FILES = """\
diff --git a/src/alpha.py b/src/alpha.py
index 1111111..2222222 100644
--- a/src/alpha.py
+++ b/src/alpha.py
@@ -1,4 +1,4 @@
 import os
-OLD = 1
+NEW = 2
 import sys
@@ -20,3 +20,4 @@ def go():
     pass
+    extra()
diff --git a/tests/test_beta.py b/tests/test_beta.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_beta.py
@@ -0,0 +1,2 @@
+def test_beta():
+    assert True
"""

# Two hunks, byte-identical edit in each — the "mechanical" shape.
REPEATED = """\
diff --git a/a/one.py b/a/one.py
index 1111111..2222222 100644
--- a/a/one.py
+++ b/a/one.py
@@ -3,3 +3,3 @@
 ctx
-from old import thing
+from new import thing
@@ -40,3 +40,3 @@
 ctx
-from old import thing
+from new import thing
"""

DELETED = """\
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 4444444..0000000
--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-line one
-line two
"""

BINARY = """\
diff --git a/logo.png b/logo.png
index 5555555..6666666 100644
Binary files a/logo.png and b/logo.png differ
"""


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def test_parse_splits_the_patch_per_file_with_its_own_stat() -> None:
    files = pr_diff.parse(TWO_FILES)
    assert [f["path"] for f in files] == ["src/alpha.py", "tests/test_beta.py"]
    alpha = files[0]
    assert (alpha["added"], alpha["removed"]) == (2, 1), alpha
    assert len(alpha["hunks"]) == 2, alpha["hunks"]
    beta = files[1]
    assert (beta["added"], beta["removed"]) == (2, 0), beta
    assert beta["status"] == "A", beta


def test_parse_names_a_deletion_as_a_deletion() -> None:
    """`D` with `-2`, not an empty file — a removed file is the change most
    worth not misreading in a review."""
    (gone,) = pr_diff.parse(DELETED)
    assert gone["status"] == "D"
    assert (gone["added"], gone["removed"]) == (0, 2)


def test_parse_marks_a_binary_file_rather_than_counting_it_as_zero_lines() -> None:
    """`0 / 0` on a binary file reads as "nothing changed here"."""
    (png,) = pr_diff.parse(BINARY)
    assert png["path"] == "logo.png"
    assert png["binary"] is True
    assert png["hunks"] == []


# ---------------------------------------------------------------------------
# the summary walk
# ---------------------------------------------------------------------------

def test_summary_lists_every_file_with_its_own_plus_minus() -> None:
    text, code = pr_diff.render(pr_diff.parse(TWO_FILES), header=["# PR #1"])
    assert code == 0
    assert "src/alpha.py" in text and "tests/test_beta.py" in text
    assert "+2" in text and "-1" in text
    assert "2 files" in text, text


def test_summary_names_the_route_to_one_files_hunks() -> None:
    """A summary that does not say how to get the rest is a summary the reader
    leaves for `gh pr diff`, which is the behaviour this op exists to end."""
    text, _ = pr_diff.render(pr_diff.parse(TWO_FILES), header=["# PR #1"],
                             number="1")
    assert "gh-pr:1:diff:" in text, text


def test_summary_carries_no_hunk_bodies() -> None:
    """The whole point: the file list must not be the diff."""
    text, _ = pr_diff.render(pr_diff.parse(TWO_FILES), header=["# PR #1"])
    assert "+NEW = 2" not in text, text


def test_one_file_route_returns_that_files_hunks_and_no_others() -> None:
    text, code = pr_diff.render(pr_diff.parse(TWO_FILES), header=["# PR #1"],
                                path="src/alpha.py")
    assert code == 0
    assert "+NEW = 2" in text
    assert "test_beta" not in text, text


def test_a_path_not_in_the_diff_declines_and_names_what_is(
) -> None:
    """"That file is not in this PR" and "that file has no changes" are the
    same render if the miss prints nothing — and the first is usually a typo or
    a wrong PR number, which the reader must be told rather than concluding the
    file is clean."""
    text, code = pr_diff.render(pr_diff.parse(TWO_FILES), header=["# PR #1"],
                                path="src/typo.py")
    assert code == 1, text
    assert "src/typo.py" in text
    assert "src/alpha.py" in text, (
        f"the miss must name the paths that do exist, or the reader has no "
        f"next move:\n{text}"
    )


# ---------------------------------------------------------------------------
# the mechanical note — a note, never a filter
# ---------------------------------------------------------------------------

def test_a_file_whose_hunks_repeat_one_edit_is_noted() -> None:
    (one,) = pr_diff.parse(REPEATED)
    note = pr_diff.mechanical_note(one)
    assert note, "two byte-identical hunks are the repeated-edit shape"
    assert "2" in note, note


def test_a_file_with_distinct_hunks_gets_no_note() -> None:
    """Under-flagging is the safe direction: a wrong 'mechanical' note is an
    invitation to skim a file that needed reading."""
    alpha = pr_diff.parse(TWO_FILES)[0]
    assert pr_diff.mechanical_note(alpha) is None


def test_the_note_never_removes_the_file_from_the_summary() -> None:
    text, _ = pr_diff.render(pr_diff.parse(REPEATED), header=["# PR #1"])
    assert "a/one.py" in text, text
    assert "1 file" in text, text


# ---------------------------------------------------------------------------
# three states
# ---------------------------------------------------------------------------

def test_an_unfetchable_diff_says_so_instead_of_rendering_empty() -> None:
    text, code = pr_diff.render(None, header=["# PR #1"],
                                reason="gh pr diff exited 1: no such PR")
    assert code == 1
    low = text.lower()
    assert "could not" in low or "unavailable" in low, text
    assert "no such PR" in text, text
    assert "0 files" not in text, (
        f"a diff nobody read must never be tallied as zero files:\n{text}"
    )


def test_a_genuinely_empty_diff_is_a_different_sentence() -> None:
    """A PR with no changes is a real, reportable state and must not borrow the
    could-not-read wording."""
    text, code = pr_diff.render([], header=["# PR #1"])
    assert code == 0, text
    assert "could not" not in text.lower(), text
    assert "no file" in text.lower() or "0 files" in text.lower(), text


def test_a_capped_summary_discloses_the_files_it_dropped() -> None:
    patch = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/f{i}.py\n+++ b/f{i}.py\n"
        f"@@ -1 +1 @@\n-a\n+b\n"
        for i in range(10)
    )
    text, _ = pr_diff.render(pr_diff.parse(patch), header=["# PR #1"],
                             max_files=4)
    assert "f0.py" in text
    assert "6 more" in text, (
        f"a cap must name how many files it withheld — a silently narrowed "
        f"file list rendering as a complete one is the defect this op is "
        f"built inside:\n{text}"
    )
    assert "10 files" in text, (
        f"the header count must be the real one, not the shown one:\n{text}"
    )


def test_a_capped_file_view_discloses_the_bytes_it_dropped() -> None:
    big = ("diff --git a/big.py b/big.py\n"
           "index 1111111..2222222 100644\n"
           "--- a/big.py\n+++ b/big.py\n"
           "@@ -1,500 +1,500 @@\n"
           + "".join(f"+line {i}\n" for i in range(500)))
    text, _ = pr_diff.render(pr_diff.parse(big), header=["# PR #1"],
                             path="big.py", max_bytes=500)
    assert "line 0" in text
    assert "bytes" in text.lower(), (
        f"the cap must say it cut and in what unit:\n{text[-400:]}"
    )
    assert "line 499" not in text
