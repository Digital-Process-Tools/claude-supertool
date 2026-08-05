"""git-push cannot make a branch's first push when the branch carries a
same-repo, different-name upstream — and reports it as a remote rejection
against a target nobody asked for (#787).

Real hermetic git repos, no mocks: the trigger is `git worktree add -b
<branch> <path> <remote>/<ref>`, which is the normal way an agent starts
work. `branch.autoSetupMerge` (on by default) tracks the *start point*, not
the new branch — so a branch that has never been pushed anywhere still
resolves `@{upstream}` to `origin/master`. `git-push` treats a resolvable
`@{upstream}` as proof the branch has a real remote copy under its own name,
so it runs a bare `git push` and lets git's own `push.default` pick the
target. Git refuses:

    fatal: The upstream branch of your current branch does not match
    the name of your current branch.

which `git-push` renders as `NOT PUSHED - REJECTED  branch -> origin/master`
— a target nobody chose (it came from `push.default`, not the caller) and a
verb implying the remote refused, when the push was never attempted.

Fix: preemptively detect `has_upstream and remote_ref != branch` — the exact
precondition for that fatal, decidable before ever invoking `git push` — and
decline in the three-state shape: name the real branch/upstream state, say
nothing was pushed, and give both of git's own remedies as one-liners.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_787", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


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
        capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
    )


class MismatchedUpstreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="st787_")
        self.remote = os.path.join(self._tmp, "remote.git")
        self.origin_clone = os.path.join(self._tmp, "origin_clone")
        self.wt = os.path.join(self._tmp, "wt")

        assert _run(["init", "--bare", "-b", "master", "remote.git"],
                    self._tmp).returncode == 0

        # Seed master on the remote.
        assert _run(["clone", self.remote, "origin_clone"],
                    self._tmp).returncode == 0
        Path(self.origin_clone, "a.txt").write_text("base")
        assert _run(["add", "a.txt"], self.origin_clone).returncode == 0
        assert _run(["commit", "-m", "init"], self.origin_clone).returncode == 0
        assert _run(["push", "origin", "master"],
                    self.origin_clone).returncode == 0

        # The reproduction: `git worktree add -b <branch> <path> origin/master`,
        # exactly what an agent runs to start a fresh issue branch.
        assert _run(["fetch", "origin"], self.origin_clone).returncode == 0
        r = _run(["worktree", "add", "-b", "fix/787-first-push", self.wt,
                  "origin/master"], self.origin_clone)
        assert r.returncode == 0, r.stderr

        Path(self.wt, "b.txt").write_text("local work")
        assert _run(["add", "b.txt"], self.wt).returncode == 0
        assert _run(["commit", "-m", "local work"], self.wt).returncode == 0

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _sanity_phantom_upstream(self) -> None:
        # Precondition: @{upstream} resolves (to origin/master) even though
        # this branch has never been pushed anywhere under its own name.
        r = _run(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                  "@{upstream}"], self.wt)
        self.assertEqual(r.returncode, 0, "expected a resolvable @{upstream}")
        self.assertEqual(r.stdout.strip(), "origin/master")

    def _drive_push(self) -> tuple[int, str]:
        prev_cwd = os.getcwd()
        prev_env = {k: os.environ.get(k) for k in _HERMETIC_ENV}
        os.chdir(self.wt)
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
        self._sanity_phantom_upstream()

    def test_declines_instead_of_misreporting_a_remote_rejection(self) -> None:
        self._sanity_phantom_upstream()

        rc, out = self._drive_push()

        # The wrong words are the bug (#787): no REJECTED (implies the
        # remote refused something), no target that was never the caller's
        # (push.default picked "master", nobody asked for it).
        self.assertNotIn("REJECTED", out,
                         f"a push that was never attempted must not read "
                         f"as a remote rejection:\n{out}")
        self.assertNotIn("-> origin/master", out,
                         f"the branch was never going to master; that "
                         f"target came from push.default, not the "
                         f"caller:\n{out}")

        # Three-state shape: says the real state, says nothing was pushed,
        # names the remedy.
        self.assertIn("fix/787-first-push", out)
        self.assertIn("origin/master", out,
                      "must still name the phantom upstream it found")
        self.assertIn("Nothing was pushed", out)
        self.assertIn("git push -u origin HEAD", out,
                      "must name the one-liner that pushes under this "
                      "branch's own name")

        self.assertNotEqual(rc, 0, "a decline is not a success")

        # And the receipt on the [result] line must not claim a rejection
        # either — that is the exact line #787 quoted.
        result_line = [l for l in out.splitlines() if l.startswith("[result]")]
        self.assertTrue(result_line, f"expected a [result] line:\n{out}")
        self.assertNotIn("REJECTED", result_line[0])

        # Ground truth: nothing was actually sent to the remote. The branch
        # must not exist there under any name.
        r = _run(["ls-remote", "--heads", self.remote], self._tmp)
        self.assertNotIn("fix/787-first-push", r.stdout,
                         "declining must not have pushed anything")


if __name__ == "__main__":
    unittest.main()
