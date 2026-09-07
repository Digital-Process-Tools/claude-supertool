"""`worktree._common.read_manifest` must not read "git could not answer" as
"no manifest was ever written" (#2371).

`read_manifest` resolves the manifest path with `git rev-parse --git-path`
before it ever looks at the filesystem. That resolution step does not
depend on whether the manifest file exists -- it only computes where it
would be -- so a failure THERE (a `_run_git` timeout, an `OSError` from the
subprocess call, or any other nonzero return) is never a real "setup never
ran". Before #2371 it was collapsed into the exact same
`ConfigResult({"linked": [], "copied": [], "excluded": []})` as a genuine
absence, which `teardown_op.py` then reported as a clean, empty teardown --
even though a live symlink into the primary checkout could still be sitting
there, untouched, because teardown thought there was nothing to remove.

Every "must report unknown" case here is paired with a "must still report
a genuine, real absence as empty" positive control in the same fixture
(this repo's CLAUDE.md: "a negative assertion needs a positive control") --
without it, a `read_manifest` that always returned `.error` would pass the
first half of this file just as well as a correct one.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).parent.parent


class ReadManifestTransientGitFailureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tests"))
        from _preset_loader import load_preset_module  # noqa: PLC0415
        cls._common = load_preset_module(
            "worktree", "_common", prefix="wt2371_common_")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="st2371_")
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name)

    # -- must report unknown, never empty ---------------------------------

    def test_run_git_timeout_is_reported_as_unknown_not_empty(self):
        """A `_run_git` timeout surfaces as `_run_git`'s own returncode -1
        contract (see `_common._run_git`'s docstring) -- this must become
        `ConfigResult(None, error=...)`, never the same empty manifest a
        genuine "setup never ran" produces.
        """
        timed_out = subprocess.CompletedProcess(
            args=["git", "-C", str(self.target), "rev-parse", "--git-path", "x"],
            returncode=-1, stdout="", stderr="timed out after 15s",
        )
        with unittest.mock.patch.object(self._common, "_run_git", return_value=timed_out):
            result = self._common.read_manifest(self.target)
        self.assertIsNone(result.config)
        self.assertIsNotNone(result.error)
        self.assertIn("timed out", result.error)

    def test_run_git_oserror_is_reported_as_unknown_not_empty(self):
        """Same contract, for the `OSError` branch of `_run_git` (e.g. git
        not on PATH, or the subprocess could not even be spawned)."""
        failed_to_spawn = subprocess.CompletedProcess(
            args=["git", "-C", str(self.target), "rev-parse", "--git-path", "x"],
            returncode=-1, stdout="", stderr="FileNotFoundError: [Errno 2] No such file or directory: 'git'",
        )
        with unittest.mock.patch.object(self._common, "_run_git", return_value=failed_to_spawn):
            result = self._common.read_manifest(self.target)
        self.assertIsNone(result.config)
        self.assertIsNotNone(result.error)
        self.assertIn("FileNotFoundError", result.error)

    def test_other_nonzero_git_failure_is_also_reported_as_unknown(self):
        """`target` was already confirmed a real worktree by `resolve_target`
        before any caller reaches `read_manifest` -- so ANY nonzero return
        from this resolution step is an anomaly, not a legitimate "no
        manifest", and must go through `.error` the same way."""
        odd_failure = subprocess.CompletedProcess(
            args=["git", "-C", str(self.target), "rev-parse", "--git-path", "x"],
            returncode=128, stdout="", stderr="fatal: not a git repository (disappeared mid-run)",
        )
        with unittest.mock.patch.object(self._common, "_run_git", return_value=odd_failure):
            result = self._common.read_manifest(self.target)
        self.assertIsNone(result.config)
        self.assertIsNotNone(result.error)
        self.assertIn("not a git repository", result.error)

    # -- positive control: a genuine absence must still read as empty -----

    def test_genuine_absence_is_still_reported_as_a_clean_empty_manifest(self):
        """The positive control for all three cases above: when git DOES
        resolve the path successfully and the file simply is not there
        (setup genuinely never ran), `read_manifest` must still return the
        plain empty manifest, not an error -- this must not regress into
        every call looking like a failure."""
        # No mocking at all: real git, against a real (non-worktree-y but
        # otherwise inert) temp dir would not even resolve --git-path, so
        # drive this the same way the rest of the suite does -- a real repo.
        import shutil  # noqa: PLC0415
        import os  # noqa: PLC0415
        env = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_TERMINAL_PROMPT": "0",
        }
        full_env = {**os.environ, **env}
        subprocess.run(["git", "init", "-q", str(self.target)], env=full_env, check=True, timeout=30)
        subprocess.run(["git", "-C", str(self.target), "config", "user.email", "t@t"], env=full_env, check=True, timeout=30)
        subprocess.run(["git", "-C", str(self.target), "config", "user.name", "T"], env=full_env, check=True, timeout=30)
        result = self._common.read_manifest(self.target)
        self.assertIsNone(result.error)
        self.assertEqual(result.config, {"linked": [], "copied": [], "excluded": []})
        self.addCleanup(shutil.rmtree, str(self.target), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
