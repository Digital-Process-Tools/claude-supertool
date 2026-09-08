"""`worktree:teardown` must not silently disable the repo-wide
`extensions.worktreeConfig` flag `worktree:setup` turned on (#2419).

That flag lives in the SHARED `.git/config`, not a per-worktree file --
`git config extensions.worktreeConfig true` (setup_op.py, no `--worktree`
flag) writes it once for the whole repository. Disabling it again would stop
EVERY worktree, not just the one being torn down, from having its own
`--worktree`-scoped config (e.g. a sibling's `core.excludesFile`) honoured
at all -- teardown of one worktree must never be able to break a sibling
that is still relying on it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISPATCHER = os.path.join(ROOT, "presets", "worktree", "dispatcher.py")

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


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )


def _run_op(mode, cwd, path_arg=None):
    argv = [sys.executable, str(DISPATCHER), mode]
    if path_arg is not None:
        argv.append(path_arg)
    return subprocess.run(
        argv, cwd=cwd, env=_HERMETIC_ENV,
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )


class WorktreeConfigExtensionTeardownTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="st2419_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.primary = os.path.join(self._tmp, "primary")
        os.makedirs(self.primary)
        _git(["init", "-q", "."], self.primary)
        _git(["config", "user.email", "t@t"], self.primary)
        _git(["config", "user.name", "T"], self.primary)

    def _write_config(self, cfg: dict):
        with open(os.path.join(self.primary, ".supertool.json"), "w", encoding="utf-8") as fh:
            json.dump({"ops": {"worktree": {"setup": cfg}}}, fh)

    def _commit_all(self, message="init"):
        _git(["add", "-A"], self.primary)
        r = _git(["commit", "-q", "-m", message], self.primary)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _add_worktree(self, name, branch):
        wt = os.path.join(self._tmp, name)
        r = _git(["worktree", "add", "-q", "-b", branch, wt], self.primary)
        self.assertEqual(r.returncode, 0, r.stderr)
        return wt

    def test_teardown_leaves_worktreeconfig_enabled_and_says_so(self):
        """Must fire: a sibling worktree (B) still relying on the shared
        extension must keep working after A's teardown, AND A's own
        exclude entries must actually be gone (the manifest-driven cleanup
        still fires) -- pairing the "must not break a sibling" case with a
        "the cleanup itself still happens" case in the same fixture.
        """
        self._write_config({"exclude": ["vendor/libs"]})
        self._commit_all()
        wt_a = self._add_worktree("a", "feature-a")
        wt_b = self._add_worktree("b", "feature-b")

        self.assertEqual(_run_op("setup", wt_a).returncode, 0)
        self.assertEqual(_run_op("setup", wt_b).returncode, 0)

        # Sanity: the flag is shared, not per-worktree -- visible from the
        # primary checkout too.
        self.assertEqual(
            _git(["config", "--get", "extensions.worktreeConfig"], self.primary).stdout.strip(),
            "true",
        )

        teardown_result = _run_op("teardown", wt_a)
        self.assertEqual(teardown_result.returncode, 0, teardown_result.stdout + teardown_result.stderr)

        # extensions.worktreeConfig is a SHARED flag -- unsetting it while
        # sibling B still has a --worktree-scoped core.excludesFile would
        # silently stop B's git from reading it at all.
        self.assertEqual(
            _git(["config", "--get", "extensions.worktreeConfig"], self.primary).stdout.strip(),
            "true",
            "teardown of one worktree must not disable the shared worktreeConfig extension "
            "while a sibling worktree still depends on it",
        )
        # B's own --worktree-scoped core.excludesFile must still resolve.
        b_excludes = _git(["config", "--get", "core.excludesFile"], wt_b).stdout.strip()
        self.assertTrue(b_excludes, "sibling worktree B's --worktree core.excludesFile must still resolve")

        # The receipt must explicitly disclose that the shared flag was
        # left enabled, rather than saying nothing about it at all.
        self.assertIn("extensions.worktreeConfig", teardown_result.stdout)
        self.assertIn("left enabled", teardown_result.stdout.lower())

        # A's own exclude entries are still actually cleaned up.
        a_exclude_file = _git(["rev-parse", "--git-path", "worktree-setup/exclude"], wt_a).stdout.strip()
        with open(a_exclude_file, encoding="utf-8") as fh:
            self.assertNotIn("vendor/libs", fh.read())


if __name__ == "__main__":
    unittest.main()
