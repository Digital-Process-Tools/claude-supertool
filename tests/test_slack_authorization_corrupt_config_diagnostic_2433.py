"""#2433 -- `load_project_config` silently returns `{}` on a corrupt or
malformed `.supertool.json`, with nothing written to stderr, unlike the
trust-violation branch eight lines above it in the same function which
already writes a `WARNING: ...` diagnostic (#2416).

This is a diagnostic defect, not a privilege one: the fail-closed behaviour
(every arm still returns `{}`, meaning every Slack channel off) is correct
and must not change. What must change is that a maintainer staring at a
silently-off Slack integration can tell a corrupt config from a project
that simply never configured one.

Positive controls sit beside every "must now warn" case, per this repo's
own testing discipline: an absent config must stay silent (it is not an
error), and a corrupt config must still fail closed (the diagnostic change
must not weaken the security behaviour it is layered onto).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import unittest
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LoadProjectConfigCorruptDiagnosticTest(unittest.TestCase):
    """`load_project_config` must warn on a present-but-broken config,
    stay silent on an absent one, and fail closed on both (#2433)."""

    def setUp(self):
        import tempfile
        import shutil
        self.auth = _load("presets/slack/_authorization.py",
                           f"slack_authorization_corrupt_2433_{id(self)}")
        self._tmp = tempfile.mkdtemp(prefix="st2433_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        os.makedirs(os.path.join(self._tmp, ".git"))

    def _run_in_tmp(self):
        old_cwd = os.getcwd()
        os.chdir(self._tmp)
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                data = self.auth.load_project_config()
        finally:
            os.chdir(old_cwd)
        return data, stderr.getvalue()

    # -- must now warn --------------------------------------------------

    def test_unparseable_json_warns_and_fails_closed(self):
        """Invalid JSON: must warn on stderr AND still return `{}`
        (fail-closed unchanged)."""
        cfg = os.path.join(self._tmp, ".supertool.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")

        data, err = self._run_in_tmp()

        self.assertEqual(data, {})
        self.assertIn("WARNING", err)
        self.assertIn(".supertool.json", err)

    def test_json_that_is_not_an_object_warns_and_fails_closed(self):
        """Valid JSON but not a dict (e.g. a bare list) is malformed for
        this loader's purposes -- must warn AND still return `{}`."""
        cfg = os.path.join(self._tmp, ".supertool.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump([1, 2, 3], fh)

        data, err = self._run_in_tmp()

        self.assertEqual(data, {})
        self.assertIn("WARNING", err)

    # -- positive controls -----------------------------------------------

    def test_absent_config_stays_silent(self):
        """Positive control: nothing configured is not an error -- the
        absent-config arm must NOT start emitting warnings just because
        the corrupt-config arm now does."""
        data, err = self._run_in_tmp()

        self.assertEqual(data, {})
        self.assertEqual(err, "")

    def test_valid_config_stays_silent_and_loads(self):
        """Positive control: an ordinary, well-formed config must load
        with no warning at all."""
        cfg = os.path.join(self._tmp, ".supertool.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"slack": {"channels": {"C0": {"level": "open"}}}}, fh)

        data, err = self._run_in_tmp()

        self.assertEqual(data, {"slack": {"channels": {"C0": {"level": "open"}}}})
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
