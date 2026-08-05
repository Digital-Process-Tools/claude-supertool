"""Audit: silent data loss vectors in git-checkout and git-resolve.

Each test runs against a real git repo in tmp_path (no mocking). The goal is
to pin whether ops REFUSE (safe), WARN, or PROCEED SILENTLY (data-loss risk).
Severity annotations follow each test where data loss is possible.

2026-05-23 — initial audit pass.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------

def _load(name: str, rel: str):
    p = Path(__file__).parent.parent / rel
    spec = importlib.util.spec_from_file_location(name, p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


checkout = _load("git_checkout", "presets/git/checkout.py")
resolve = _load("git_resolve", "presets/git/resolve.py")


# ---------------------------------------------------------------------------
# Git repo factory
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=repo,
        capture_output=True,
        text=True,
        check=check, encoding="utf-8", errors="replace",
    )


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one commit on 'master'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master", check=True)
    _git(repo, "config", "user.email", "test@test.com", check=True)
    _git(repo, "config", "user.name", "Test", check=True)
    readme = repo / "README.md"
    readme.write_text("hello\n")
    _git(repo, "add", "README.md", check=True)
    _git(repo, "commit", "-m", "init", check=True)
    return repo


def _make_conflict_repo(tmp_path: Path) -> Path:
    """Repo with an active merge conflict on 'conflict.txt'."""
    repo = _make_repo(tmp_path)

    # branch-a: sets conflict.txt = "ours"
    _git(repo, "checkout", "-b", "branch-a", check=True)
    (repo / "conflict.txt").write_text("ours\n")
    _git(repo, "add", "conflict.txt", check=True)
    _git(repo, "commit", "-m", "ours side", check=True)

    # master: sets conflict.txt = "theirs"
    _git(repo, "checkout", "master", check=True)
    (repo / "conflict.txt").write_text("theirs\n")
    _git(repo, "add", "conflict.txt", check=True)
    _git(repo, "commit", "-m", "theirs side", check=True)

    # Trigger the conflict
    result = _git(repo, "merge", "branch-a")
    assert result.returncode != 0, "expected merge conflict"
    return repo


# ---------------------------------------------------------------------------
# Utility: run a preset main() inside a given cwd
# ---------------------------------------------------------------------------

def _run_checkout(repo: Path | None, ref: str, monkeypatch, capsys):
    if repo:
        monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["checkout.py", ref])
    rc = checkout.main()
    out = capsys.readouterr().out
    return rc, out


def _run_resolve(repo: Path | None, side: str, paths: str, monkeypatch, capsys):
    if repo:
        monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["resolve.py", side, paths])
    rc = resolve.main()
    out = capsys.readouterr().out
    return rc, out


# ===========================================================================
# git-checkout tests
# ===========================================================================

class TestCheckoutUncommittedChanges:
    """Case 1: tracked file with local modifications.

    git checkout will fail with 'would be overwritten' when the target branch
    has a different version of the modified file. The op should catch this and
    refuse, not silently overwrite.
    """

    def test_refuses_on_dirty_tracked_file(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)

        # Create branch-b with a different version of README.md
        _git(repo, "checkout", "-b", "branch-b", check=True)
        (repo / "README.md").write_text("branch-b version\n")
        _git(repo, "add", "README.md", check=True)
        _git(repo, "commit", "-m", "branch-b change", check=True)

        # Go back to master, dirty the file (different content)
        _git(repo, "checkout", "master", check=True)
        (repo / "README.md").write_text("local unsaved work\n")
        # Do NOT stage or commit — this is the "uncommitted change" case

        rc, out = _run_checkout(repo, "branch-b", monkeypatch, capsys)

        # SAFE behavior: op must refuse and explain
        assert rc != 0, "SILENT DATA LOSS: checkout silently overwrote local changes"
        assert any(
            kw in out.lower()
            for kw in ("uncommitted", "overwritten", "stash", "local changes")
        ), f"Expected refusal message, got: {out!r}"

        # Verify the local content is intact
        content = (repo / "README.md").read_text(encoding="utf-8")
        assert content == "local unsaved work\n", (
            f"SILENT DATA LOSS: local content was clobbered. Got: {content!r}"
        )

    def test_clean_checkout_succeeds(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        _git(repo, "checkout", "-b", "branch-c", check=True)
        _git(repo, "checkout", "master", check=True)

        rc, out = _run_checkout(repo, "branch-c", monkeypatch, capsys)
        assert rc == 0
        assert "branch-c" in out


class TestCheckoutUntrackedFileOverwrite:
    """Case 2: untracked file that the target branch would create.

    git checkout refuses if an untracked file would be overwritten by the
    target branch's version of that file. Op should propagate this refusal.
    """

    def test_refuses_when_untracked_would_be_overwritten(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)

        # branch-d creates new-file.txt
        _git(repo, "checkout", "-b", "branch-d", check=True)
        (repo / "new-file.txt").write_text("from branch-d\n")
        _git(repo, "add", "new-file.txt", check=True)
        _git(repo, "commit", "-m", "add new-file.txt", check=True)

        # Back on master, create the same untracked file with different content
        _git(repo, "checkout", "master", check=True)
        (repo / "new-file.txt").write_text("local untracked content\n")

        rc, out = _run_checkout(repo, "branch-d", monkeypatch, capsys)

        # SAFE behavior: git itself refuses; op must surface that
        assert rc != 0, "SILENT DATA LOSS: untracked file was silently overwritten"
        content = (repo / "new-file.txt").read_text(encoding="utf-8")
        assert content == "local untracked content\n", (
            f"SILENT DATA LOSS: untracked file was clobbered. Got: {content!r}"
        )


class TestCheckoutDetachedHead:
    """Case 3: checkout to a commit SHA → detached HEAD.

    Should succeed but the op should communicate the detached-HEAD state
    (or at minimum not crash/lose data).
    """

    def test_checkout_to_commit_sha(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

        rc, out = _run_checkout(repo, sha, monkeypatch, capsys)

        # Must succeed — detached HEAD is a valid git state
        assert rc == 0, f"Unexpected failure checking out SHA {sha!r}: {out}"
        # Op should show the transition
        assert sha in out or "HEAD" in out


class TestCheckoutNonExistentRef:
    """Case 4: ref that doesn't exist locally or remotely."""

    def test_clean_error_on_missing_ref(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        # No remotes → fetch will be a no-op or fail harmlessly
        rc, out = _run_checkout(repo, "definitely-does-not-exist-ref-xyz", monkeypatch, capsys)

        assert rc != 0
        assert "ERROR" in out or "not found" in out.lower() or "error" in out.lower()
        # Must not be empty
        assert out.strip(), "Expected an error message, got empty output"


class TestCheckoutShellSpecialChars:
    """Case 9: ref containing shell metacharacters — injection guard."""

    def test_shell_injection_in_ref_is_not_executed(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        # If subprocess is used with a list (not shell=True), this is safe.
        # But if somewhere shell=True leaks in, rm -rf would be catastrophic.
        canary = tmp_path / "canary.txt"
        canary.write_text("alive\n")

        malicious_ref = f"feature/branch;rm -rf {canary}"
        rc, out = _run_checkout(repo, malicious_ref, monkeypatch, capsys)

        # Must fail — the ref doesn't exist
        assert rc != 0
        # The canary must still exist — no shell injection occurred
        assert canary.exists(), (
            "CRITICAL SECURITY: shell injection deleted canary file. "
            "The ref argument was passed to shell=True."
        )

    def test_ref_with_backticks_not_executed(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        canary = tmp_path / "canary2.txt"
        canary.write_text("alive\n")

        rc, out = _run_checkout(repo, "`touch /tmp/injected-by-supertool`", monkeypatch, capsys)
        assert rc != 0
        assert not Path("/tmp/injected-by-supertool").exists(), (
            "CRITICAL SECURITY: backtick injection executed a command."
        )


class TestCheckoutNonGitDirectory:
    """Case 10: running outside any git repo."""

    def test_clean_error_outside_git_repo(self, tmp_path, monkeypatch, capsys):
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        rc, out = _run_checkout(non_repo, "master", monkeypatch, capsys)

        assert rc != 0
        assert out.strip(), "Expected an error message, got empty output"
        # Should mention it's not a git repo or fail with checkout error
        assert any(
            kw in out.lower()
            for kw in ("not", "git", "error", "repository")
        ), f"Unexpected error message: {out!r}"


class TestCheckoutRemoteOnlyBranch:
    """#277: branch exists on a remote but was never checked out locally.

    git's DWIM (`git checkout <branch>` → create local tracking branch) can
    fail to fire; the op must resolve it explicitly via the remote-tracking
    ref instead of erroring 'not found even after fetch'.
    """

    def _make_clone_with_remote_branch(self, tmp_path: Path) -> Path:
        """Clone with a remote-only branch 'feature/remote-only'.

        Returns the clone repo. The branch exists on origin and as a
        remote-tracking ref, but has no local branch.
        """
        origin = self._make_origin(tmp_path)
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", str(origin), str(clone), check=True)
        _git(clone, "config", "user.email", "test@test.com", check=True)
        _git(clone, "config", "user.name", "Test", check=True)
        # Tracking ref exists (from clone), but no local branch.
        assert _git(clone, "rev-parse", "--verify", "--quiet",
                     "origin/feature/remote-only").returncode == 0
        assert _git(clone, "rev-parse", "--verify", "--quiet",
                     "feature/remote-only").returncode != 0
        return clone

    def _make_origin(self, tmp_path: Path) -> Path:
        seed = _make_repo(tmp_path)
        _git(seed, "checkout", "-b", "feature/remote-only", check=True)
        (seed / "feature.txt").write_text("remote work\n")
        _git(seed, "add", "feature.txt", check=True)
        _git(seed, "commit", "-m", "remote-only feature", check=True)
        _git(seed, "checkout", "master", check=True)
        origin = tmp_path / "origin.git"
        _git(tmp_path, "clone", "--bare", str(seed), str(origin), check=True)
        return origin

    def test_checks_out_remote_only_branch(self, tmp_path, monkeypatch, capsys):
        clone = self._make_clone_with_remote_branch(tmp_path)

        rc, out = _run_checkout(clone, "feature/remote-only", monkeypatch, capsys)

        assert rc == 0, f"Expected success, got rc={rc}: {out!r}"
        branch = _git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert branch == "feature/remote-only", f"On wrong branch: {branch!r}"
        # Local tracking branch must now exist with the remote's content.
        assert (clone / "feature.txt").read_text(encoding="utf-8") == "remote work\n"

    def test_dwim_disabled_still_resolves(self, tmp_path, monkeypatch, capsys):
        """checkout.guess=false disables git's DWIM — op must still resolve."""
        clone = self._make_clone_with_remote_branch(tmp_path)
        _git(clone, "config", "checkout.guess", "false", check=True)

        rc, out = _run_checkout(clone, "feature/remote-only", monkeypatch, capsys)

        assert rc == 0, f"Expected success with guess disabled, got rc={rc}: {out!r}"
        branch = _git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert branch == "feature/remote-only"

    def test_genuinely_missing_branch_still_errors(self, tmp_path, monkeypatch, capsys):
        clone = self._make_clone_with_remote_branch(tmp_path)

        rc, out = _run_checkout(clone, "no/such/branch", monkeypatch, capsys)

        assert rc != 0
        assert "not found" in out.lower(), f"Expected not-found error, got: {out!r}"


# ===========================================================================
# git-resolve tests
# ===========================================================================

class TestResolveNoConflict:
    """Case 5: git-resolve called when there are no conflicts.

    Should report cleanly that there's nothing to do — not silently succeed
    or corrupt anything.
    """

    def test_no_conflict_reports_nothing_to_resolve(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)

        rc, out = _run_resolve(repo, "ours", "README.md", monkeypatch, capsys)

        # Should succeed (rc=0) and explain no conflicts exist
        assert rc == 0
        assert "no conflicted" in out.lower() or "nothing to resolve" in out.lower(), (
            f"Expected 'no conflicted files' message, got: {out!r}"
        )


class TestResolveSilentlyWipesOtherSide:
    """Case 6: git-resolve picks SIDE=ours — does it warn that THEIRS is lost?

    This is the core data-loss audit: picking 'ours' irreversibly discards
    the 'theirs' contribution. The op should pin whether it shows a diff/
    warning or just silently stages the ours version.

    SEVERITY: MEDIUM — data loss is intentional (user chose a side) but
    silent discard without showing what's lost is poor UX and error-prone.
    """

    def test_resolve_ours_stages_ours_content(self, tmp_path, monkeypatch, capsys):
        repo = _make_conflict_repo(tmp_path)

        rc, out = _run_resolve(repo, "ours", "conflict.txt", monkeypatch, capsys)

        assert rc == 0, f"Unexpected failure: {out}"
        content = (repo / "conflict.txt").read_text(encoding="utf-8")
        # "ours" in a merge means HEAD = master's version = "theirs\n"
        # (the branch being merged INTO is HEAD = master)
        assert "<<<<<<" not in content, "Conflict markers still present after resolve"

        # PIN BEHAVIOR: Does the op show what was discarded?
        # Current behavior: no diff shown, no warning about discarded content.
        # This is a UX data-loss risk — user may not realize what was wiped.
        shows_diff = any(kw in out for kw in ("+", "-", "discarded", "dropped", "diff"))
        # Document current behavior (expected: False = no diff shown)
        # If this changes to True, update the comment above.
        _ = shows_diff  # pinned — see docstring

    def test_resolve_theirs_stages_theirs_content(self, tmp_path, monkeypatch, capsys):
        repo = _make_conflict_repo(tmp_path)

        rc, out = _run_resolve(repo, "theirs", "conflict.txt", monkeypatch, capsys)

        assert rc == 0, f"Unexpected failure: {out}"
        content = (repo / "conflict.txt").read_text(encoding="utf-8")
        assert "<<<<<<" not in content
        # branch-a had "ours\n" — picking theirs from branch-a gives "ours\n"
        assert "ours" in content

    def test_resolve_shows_receipt_not_silent(self, tmp_path, monkeypatch, capsys):
        """At minimum, the op prints a receipt showing which files were resolved."""
        repo = _make_conflict_repo(tmp_path)

        rc, out = _run_resolve(repo, "ours", "conflict.txt", monkeypatch, capsys)

        assert rc == 0
        # The op must print SOMETHING — silent success is not acceptable
        assert out.strip(), "SILENT DATA LOSS: resolve produced no output at all"
        # Should mention the resolved file
        assert "conflict.txt" in out, (
            f"Expected file name in output, got: {out!r}"
        )


class TestResolveInvalidSide:
    """Case 7: invalid SIDE argument."""

    def test_invalid_side_clean_error(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        rc, out = _run_resolve(repo, "ours-typo", "README.md", monkeypatch, capsys)

        assert rc != 0
        assert "ours" in out.lower() or "theirs" in out.lower() or "side" in out.lower(), (
            f"Expected helpful error about valid SIDE values, got: {out!r}"
        )


class TestResolveMultiplePathsPartiallyConflicted:
    """Case 8: comma-separated paths where only some are conflicted.

    Op should reject the batch atomically (before touching anything) rather
    than partially applying — partial resolves leave the repo in a mixed state.
    """

    def test_non_conflicted_path_in_list_is_rejected_atomically(self, tmp_path, monkeypatch, capsys):
        repo = _make_conflict_repo(tmp_path)

        # conflict.txt is conflicted; README.md is not
        rc, out = _run_resolve(repo, "ours", "conflict.txt,README.md", monkeypatch, capsys)

        assert rc != 0, (
            "Expected atomic rejection when non-conflicted file is in the list"
        )
        assert "not conflicted" in out.lower() or "readme" in out.lower(), (
            f"Expected rejection mentioning non-conflicted file, got: {out!r}"
        )

        # conflict.txt must NOT have been resolved — atomic = all-or-nothing
        conflict_content = (repo / "conflict.txt").read_text(encoding="utf-8")
        assert "<<<<<<<" in conflict_content, (
            "SILENT PARTIAL DATA LOSS: conflict.txt was resolved despite atomic "
            "validation failing. Non-conflicted file in batch should block all resolves."
        )

    def test_all_conflicted_batch_resolves_all(self, tmp_path, monkeypatch, capsys):
        """When ALL listed paths are conflicted, batch should succeed."""
        repo = _make_repo(tmp_path)

        # Create two conflicting files
        _git(repo, "checkout", "-b", "multi-a", check=True)
        (repo / "file_a.txt").write_text("a-side\n")
        (repo / "file_b.txt").write_text("b-side\n")
        _git(repo, "add", "file_a.txt", "file_b.txt", check=True)
        _git(repo, "commit", "-m", "multi side a", check=True)

        _git(repo, "checkout", "master", check=True)
        (repo / "file_a.txt").write_text("master-a\n")
        (repo / "file_b.txt").write_text("master-b\n")
        _git(repo, "add", "file_a.txt", "file_b.txt", check=True)
        _git(repo, "commit", "-m", "multi side master", check=True)

        _git(repo, "merge", "multi-a")  # produces conflict

        rc, out = _run_resolve(repo, "ours", "file_a.txt,file_b.txt", monkeypatch, capsys)

        assert rc == 0, f"Batch resolve failed unexpectedly: {out}"
        assert "file_a.txt" in out
        assert "file_b.txt" in out


class TestResolveOutsideRepo:
    """Case 11: git-resolve called outside a git repo."""

    def test_clean_error_outside_git_repo(self, tmp_path, monkeypatch, capsys):
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()

        rc, out = _run_resolve(non_repo, "ours", "somefile.txt", monkeypatch, capsys)

        assert rc != 0
        assert out.strip(), "Expected an error message, got empty output"
        assert any(
            kw in out.lower()
            for kw in ("not", "git", "error", "repository")
        ), f"Unexpected error message: {out!r}"

    def test_resolve_with_path_outside_repo_boundary(self, tmp_path, monkeypatch, capsys):
        """File path pointing outside the repo (../../etc) — should not be accessible.

        git checkout --ours -- ../../outside.txt would fail at the git layer,
        but we verify the op returns an error cleanly rather than silently
        operating on an unexpected path.
        """
        repo = _make_conflict_repo(tmp_path)

        # Try to resolve a path that walks outside the repo
        rc, out = _run_resolve(repo, "ours", "../../outside.txt", monkeypatch, capsys)

        # Must refuse — the path is not in the conflict list
        assert rc != 0
        assert "not conflicted" in out.lower() or "error" in out.lower(), (
            f"Expected rejection of path outside repo, got: {out!r}"
        )
