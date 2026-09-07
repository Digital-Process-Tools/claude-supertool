"""`worktree:setup` must not silently overwrite a real provisioning manifest
with an empty one when `read_manifest` reports `.error` (#2386).

`read_manifest` now correctly distinguishes "no manifest was ever written"
from "I could not tell" (#2371's `.error` contract) -- a transient git
failure resolving the manifest path, or a manifest file that exists but
failed to parse, both come back as `ConfigResult(None, error=...)`. Before
this fix, `setup_op.run` treated `.error` exactly like "empty", rebuilt an
in-memory manifest from scratch, and unconditionally `write_manifest`'d it
over whatever was already on disk -- discarding any `linked`/`copied`/
`excluded` entries a PRIOR successful setup run had recorded that this run
does not itself re-declare or re-create.

Paired with a positive control (this repo's CLAUDE.md: "a negative
assertion needs a positive control"): a genuine first-ever setup, with no
`.error` at all, must still correctly write a fresh manifest -- a fix that
made setup NEVER write a manifest would pass the first half of this file
just as well as a correct one.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).parent.parent

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


class SetupManifestErrorNoClobberTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tests"))
        from _preset_loader import load_preset_module  # noqa: PLC0415
        cls._common = load_preset_module("worktree", "_common", prefix="wt2386_common_")
        cls.setup_op = load_preset_module("worktree", "setup_op", prefix="wt2386_setup_")

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="st2386_")
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

    def _add_worktree(self, name="wt", branch="feature"):
        wt = os.path.join(self._tmp, name)
        r = _git(["worktree", "add", "-q", "-b", branch, wt], self.primary)
        self.assertEqual(r.returncode, 0, r.stderr)
        return wt

    def test_transient_read_error_does_not_clobber_a_real_manifest(self):
        """A prior, successful setup run recorded real `linked`/`copied`
        entries. This run's own `read_manifest` call fails transiently
        (mocked, matching #2371's `.error` contract) -- the on-disk
        manifest must survive byte-for-byte, never be replaced with an
        empty (or partial) one.
        """
        self._write_config({"link": ["vendor/libs"], "copy": ["data/cache"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "vendor", "libs"))
        with open(os.path.join(self.primary, "vendor", "libs", "a.so"), "w") as fh:
            fh.write("lib")
        os.makedirs(os.path.join(self.primary, "data", "cache"))
        with open(os.path.join(self.primary, "data", "cache", "x.bin"), "w") as fh:
            fh.write("cache")
        wt = Path(self._add_worktree())

        # First, real setup: writes a real manifest recording both entries.
        rc, out = self.setup_op.run(wt)
        self.assertEqual(rc, 0, out)
        manifest_path_str = _git(
            ["rev-parse", "--git-path", "worktree-setup/manifest.json"], str(wt),
        ).stdout.strip()
        manifest_path = Path(manifest_path_str)
        before = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(before["linked"]), ["vendor/libs"])
        self.assertEqual(sorted(before["copied"]), ["data/cache"])

        # Now simulate this run's OWN read_manifest call failing transiently
        # -- the real manifest on disk is fine, `read_manifest` just could
        # not tell (#2371's `.error` contract).
        with unittest.mock.patch.object(
            self.setup_op._common, "read_manifest",
            return_value=self.setup_op._common.ConfigResult(
                None, error="could not resolve the provisioning manifest path: git did not answer",
            ),
        ):
            rc2, out2 = self.setup_op.run(wt)
        self.assertEqual(rc2, 0, out2)

        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(before, after, "on-disk manifest was clobbered by a transient read failure")

    def test_genuine_first_setup_still_writes_a_fresh_manifest(self):
        """Positive control: no `.error` at all (real, first-ever setup) --
        a fresh manifest must still be created, correctly.
        """
        self._write_config({"link": ["vendor/libs"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "vendor", "libs"))
        with open(os.path.join(self.primary, "vendor", "libs", "a.so"), "w") as fh:
            fh.write("lib")
        wt = Path(self._add_worktree())

        rc, out = self.setup_op.run(wt)
        self.assertEqual(rc, 0, out)

        manifest_path_str = _git(
            ["rev-parse", "--git-path", "worktree-setup/manifest.json"], str(wt),
        ).stdout.strip()
        manifest_path = Path(manifest_path_str)
        self.assertTrue(manifest_path.is_file())
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["linked"]), ["vendor/libs"])


if __name__ == "__main__":
    unittest.main()
