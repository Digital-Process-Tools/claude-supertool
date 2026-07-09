"""Regression test for issue #354 — real hermetic git repos, no mocks.

Scenario: a branch exists on origin, is checked out in a fresh repo that has
NO remote-tracking refspec (worktree / odd clone), a local commit is added,
and the remote has since moved ahead. The first `git-push` non-fast-forwards
and drops into `_recover_by_rebase`.

Bug: `_recover_by_rebase` fetched `origin <branch>` (which — without a
configured fetch refspec — populates ONLY FETCH_HEAD, not the remote-tracking
ref `refs/remotes/origin/<branch>`) and then rebased onto `origin/<branch>`,
which does not exist → `fatal: invalid upstream 'origin/<branch>'`.

Fix: rebase onto FETCH_HEAD, which the preceding fetch always populates.

Everything is self-contained (bare "remote" + two working repos in a tmp dir)
and self-cleaning. No network, never touches the real supertool repo.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


# Environment that keeps every git call hermetic: no user/global/system config,
# fixed identity, no signing, no hooks inheriting from the host.
_HERMETIC_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
        capture_output=True, text=True, timeout=60,
    )


def _commit(cwd: str, fname: str, msg: str) -> None:
    Path(cwd, fname).write_text(msg)
    assert _run(["add", fname], cwd).returncode == 0
    assert _run(["commit", "-m", msg], cwd).returncode == 0


class FirstUpstreamRebaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="st354_")
        self.remote = os.path.join(self._tmp, "remote.git")
        self.c1 = os.path.join(self._tmp, "c1")   # normal collaborator
        self.c2 = os.path.join(self._tmp, "c2")   # the fresh, refspec-less repo

        assert _run(["init", "--bare", "remote.git"], self._tmp).returncode == 0

        # Collaborator clone: create + push `feature`.
        assert _run(["clone", self.remote, "c1"], self._tmp).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.c1).returncode == 0
        _commit(self.c1, "a.txt", "base on remote")
        assert _run(["push", "-u", "origin", "feature"], self.c1).returncode == 0

        # Fresh repo WITHOUT a remote-tracking fetch refspec — the trigger.
        os.makedirs(self.c2)
        assert _run(["init"], self.c2).returncode == 0
        assert _run(["remote", "add", "origin", self.remote], self.c2).returncode == 0
        _run(["config", "--unset-all", "remote.origin.fetch"], self.c2)
        assert _run(["fetch", "origin", "feature"], self.c2).returncode == 0
        assert _run(["checkout", "-b", "feature", "FETCH_HEAD"], self.c2).returncode == 0
        _run(["branch", "--unset-upstream"], self.c2)  # ensure Tracking: (none)
        _commit(self.c2, "local.txt", "my local work")

        # Remote moves ahead (a teammate pushes) → first push will non-ff.
        _commit(self.c1, "more.txt", "remote advanced")
        assert _run(["push", "origin", "feature"], self.c1).returncode == 0

    def tearDown(self) -> None:
        # Never let a failing test leak tmp dirs.
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _local_subjects(self) -> list[str]:
        r = _run(["log", "--format=%s"], self.c2)
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    def _sanity_no_remote_tracking_ref(self) -> None:
        # Precondition of the bug: origin/feature is NOT resolvable here, even
        # after a one-shot fetch — only FETCH_HEAD is.
        assert _run(["fetch", "origin", "feature"], self.c2).returncode == 0
        self.assertNotEqual(
            _run(["rev-parse", "origin/feature"], self.c2).returncode, 0,
            "expected NO remote-tracking ref for the repro precondition")
        self.assertEqual(
            _run(["rev-parse", "FETCH_HEAD"], self.c2).returncode, 0,
            "FETCH_HEAD must always be populated by the fetch")

    def _drive_push(self) -> tuple[int, str]:
        """Run push.main() inside c2, capturing stdout. Restores cwd + env."""
        import io
        from contextlib import redirect_stdout

        prev_cwd = os.getcwd()
        prev_env = {k: os.environ.get(k) for k in _HERMETIC_ENV}
        os.chdir(self.c2)
        os.environ.update({k: v for k, v in _HERMETIC_ENV.items() if v is not None})
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = push.main()
        finally:
            os.chdir(prev_cwd)
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, buf.getvalue()

    def test_precondition_holds(self) -> None:
        self._sanity_no_remote_tracking_ref()

    def test_first_push_recovers_rebases_and_keeps_local_commit(self) -> None:
        self._sanity_no_remote_tracking_ref()

        rc, out = self._drive_push()

        # Before the fix this fails with the reported error.
        self.assertNotIn("invalid upstream", out,
                         f"regression #354: invalid upstream still surfaced:\n{out}")
        self.assertNotIn("could not start", out,
                         f"rebase should start cleanly now:\n{out}")
        self.assertEqual(rc, 0, f"push should succeed after rebase:\n{out}")
        self.assertIn("pushed", out.lower(), f"expected a pushed receipt:\n{out}")

        # No data loss: the local commit survived the rebase and is on the
        # remote, replayed on top of the remote-advanced commit.
        subjects = self._local_subjects()
        self.assertIn("my local work", subjects,
                      f"local commit was dropped by the rebase:\n{subjects}")
        self.assertIn("remote advanced", subjects,
                      f"remote commit missing after rebase:\n{subjects}")

        remote_head = _run(
            ["log", "-1", "--format=%s", "feature"], self.remote).stdout.strip()
        self.assertEqual(remote_head, "my local work",
                         "remote tip should be the replayed local commit")


if __name__ == "__main__":
    unittest.main()
