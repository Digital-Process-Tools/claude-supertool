"""2246 -- the forge-supplied artifact expiry timestamp must be flattened
through `_untrusted.flat()` exactly like its sibling `name` field, in both
`presets/gitlab/job.py::print_artifacts` and
`presets/github/job.py::print_artifacts`.

Both functions already route the artifact `name` through `_untrusted.flat()`
before printing it, but left the expiry field (`artifacts_expire_at` /
`expires_at`) unflattened in the same function -- the one field beside a
flattened sibling that was not. A forge-supplied string carrying an embedded
newline is used here as the probe: unflattened, it would open a second,
unprefixed line in the rendered receipt; flattened, `_untrusted.flat()`
collapses the embedded newline to a space and the whole value stays on the
one line it was printed on.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PRESETS_DIR = Path(__file__).parent.parent / "presets"


def _load(name: str, relative: str):
    path = PRESETS_DIR / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FORGED_LINE = "FORGED-LINE-2246: not a real artifact field"
MALICIOUS_EXPIRY = "2024-01-01T00:00:00Z\n" + FORGED_LINE


def test_gitlab_print_artifacts_flattens_the_expiry_field(monkeypatch, capsys):
    if str(PRESETS_DIR) not in sys.path:
        sys.path.insert(0, str(PRESETS_DIR))
    job = _load("gitlab_job_2246", "gitlab/job.py")
    meta = {
        "artifacts": ["archive"],
        "artifacts_file": {"filename": "build.zip", "size": 100},
        "artifacts_expire_at": MALICIOUS_EXPIRY,
    }
    monkeypatch.setattr(job, "_job_row", lambda job_id: (meta, ""))
    monkeypatch.setattr(job, "_is_past", lambda value: False)
    job.print_artifacts("1")
    out = capsys.readouterr().out
    assert FORGED_LINE not in out.splitlines(), (
        "the forged newline in artifacts_expire_at opened its own line -- "
        "the expiry field reached print() unflattened"
    )
    assert "FORGED-LINE-2246" in out.replace("\n", " ")


def test_github_print_artifacts_flattens_the_expires_at_field(monkeypatch, capsys):
    if str(PRESETS_DIR) not in sys.path:
        sys.path.insert(0, str(PRESETS_DIR))
    job = _load("github_job_2246", "github/job.py")
    artifacts = [{
        "name": "build-output",
        "size_in_bytes": 100,
        "expired": False,
        "expires_at": MALICIOUS_EXPIRY,
    }]
    monkeypatch.setattr(job, "_job_run_id", lambda job_id: ("77", ""))
    monkeypatch.setattr(job, "_run_artifacts", lambda run_id: (artifacts, ""))
    job.print_artifacts("1")
    out = capsys.readouterr().out
    assert FORGED_LINE not in out.splitlines(), (
        "the forged newline in expires_at opened its own line -- the expiry "
        "field reached print() unflattened"
    )
    assert "FORGED-LINE-2246" in out.replace("\n", " ")
