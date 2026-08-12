"""`gl-issue` must not write outside IMAGE_DIR when the API picks the name (#1484).

`iid` comes from the `glab issue view --output json` reply, so the directory
`_download_images` writes into is chosen by the remote host. Two failures on
master: `os.makedirs` ran before anything validated `iid`, and the per-file
containment check was anchored to that already-escaped directory instead of to
`IMAGE_DIR`.

These assert on the **filesystem** -- what exists under and outside the root
after the call -- not on the return value and not on a guard having been called.
A site can call a guard and write anyway.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "issue.py"
_spec = importlib.util.spec_from_file_location("gitlab_issue_1484", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)

URLS = ["https://gitlab.example/-/project/uploads/abc123/shot.png"]


def _traversal(*tail: str) -> str:
    """`../<tail>` built from os.pardir, so this is not a POSIX literal.

    One level, so the escape lands on a path the test names exactly and the
    filesystem assertion is the one that fails.
    """
    return os.path.join(os.pardir, *tail)


class _Recorder:
    """A `subprocess.run` that never succeeds, so a refusal and a failed fetch
    are told apart by `calls`, not by whether the run blew up. The assertion
    that matters is on the filesystem, and it has to be reached to be made."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return subprocess.CompletedProcess(args=["glab"], returncode=1, stdout=b"")


def test_traversing_iid_creates_nothing_outside_image_dir(monkeypatch, tmp_path, capsys):
    root = tmp_path / "images"
    root.mkdir()
    outside = tmp_path / "escaped"
    monkeypatch.setattr(issue, "IMAGE_DIR", str(root))
    run = _Recorder()
    monkeypatch.setattr(issue.subprocess, "run", run)

    issue._download_images(URLS, _traversal("escaped"))

    assert not outside.exists(), f"created {outside} -- outside IMAGE_DIR {root}"
    assert set(root.rglob("*")) == set(), "wrote inside the root for a refused id"
    assert run.calls == 0, "fetched an attachment after the id was refused"
    assert "skipped" in capsys.readouterr().out.lower(), "refused in silence"


def test_absolute_iid_creates_nothing_outside_image_dir(monkeypatch, tmp_path):
    """os.path.join drops the root entirely when the second arg is absolute."""
    root = tmp_path / "images"
    root.mkdir()
    outside = tmp_path / "abs-escaped"
    monkeypatch.setattr(issue, "IMAGE_DIR", str(root))
    run = _Recorder()
    monkeypatch.setattr(issue.subprocess, "run", run)

    issue._download_images(URLS, str(outside))

    assert not outside.exists(), f"created {outside} -- os.path.join discarded the root"
    assert run.calls == 0, "fetched an attachment after the id was refused"


def test_numeric_iid_still_downloads(monkeypatch, tmp_path):
    """The refusal must not eat the ordinary path."""
    root = tmp_path / "images"
    monkeypatch.setattr(issue, "IMAGE_DIR", str(root))

    class _Result:
        returncode = 0
        stdout = b"PNGDATA"

    monkeypatch.setattr(issue.subprocess, "run", lambda *a, **k: _Result())

    got = issue._download_images(URLS, "12345")

    assert len(got) == 1, f"numeric iid downloaded nothing: {got}"
    written = Path(got[0])
    assert written.read_bytes() == b"PNGDATA"
    assert written.parent == root / "12345"
