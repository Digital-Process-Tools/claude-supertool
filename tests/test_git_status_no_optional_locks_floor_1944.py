"""#1944 follow-up — a git too old for `--no-optional-locks` must not read as clean.

`run()` returns stdout only and ignores the exit code for `rev-parse
--is-inside-work-tree`, deliberately, because a real "outside repository" and
a corrupt `.git` both exit 128 with an empty stdout and git does not expose a
distinction. Adding `--no-optional-locks` (git 2.15+, no documented floor in
this repository) introduced a THIRD cause of an empty stdout: git rejecting
the flag itself, exit 129, usage text on stderr. That is not "outside
repository" -- it is "we could not ask the question" -- and the pre-existing
`rev.strip() != "true"` branch could not tell the two apart, so a file inside
a real, dirty repository would report a fabricated `state: "clean"` the
instant `$GIT_BIN` pointed at a git older than 2.15. That is exactly the
"absence read as clean" class #1202/#1882 already fixed twice in this file.

The same failure reaches `diff --numstat` and `status --porcelain` too --
their outputs feed `_parse_numstat("")` / `_parse_state("")`, both of which
read empty as "nothing changed" -- so the fix lives in `run()` itself rather
than only in the rev-parse call site.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "git-status" / "git-status.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("git_status_1944_floor", ADAPTER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FlagRejectedProcess:
    """Stands in for a git old enough that `--no-optional-locks` is unknown.

    Exit 129, empty stdout, usage text on stderr naming the flag verbatim --
    measured against real git 2.46.2 rejecting a bogus global flag the same
    way (`git --totally-bogus-flag rev-parse --is-inside-work-tree`).
    """

    def __init__(self, argv: list) -> None:
        self.args = argv
        self.returncode = 129

    def communicate(self, timeout=None):  # noqa: ANN001
        return "", "unknown option: --no-optional-locks\nusage: git [...]\n"

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout=None):  # noqa: ANN001
        return 129


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "base.txt").write_text("line1\nline2\n", encoding="utf-8")
    _git(tmp_path, "add", "base.txt")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "base.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return tmp_path


def _drive_flag_rejected(monkeypatch: pytest.MonkeyPatch, target: Path) -> dict:
    mod = _load()

    def _popen(argv, **_kwargs):
        return _FlagRejectedProcess(list(argv))

    monkeypatch.setattr(mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/git")
    monkeypatch.setattr(mod.sys, "argv", [str(ADAPTER), str(target)])
    emitted: list = []
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: emitted.append(" ".join(map(str, a))))
    mod.main()
    assert emitted, "the adapter emitted nothing"
    return json.loads(emitted[-1])


def test_a_rejected_flag_is_not_reported_as_a_clean_working_tree(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST NOT FIRE. The regression: state:"clean" for a real, dirty repo."""
    payload = _drive_flag_rejected(monkeypatch, repo / "base.txt")
    metrics = payload.get("metrics") or {}
    assert metrics.get("state") != "clean", payload
    assert payload.get("ok") is not True, payload


def test_a_rejected_flag_declines_with_the_reserved_code(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST NOT FIRE (the flag-rejected case must decline, not fabricate)."""
    payload = _drive_flag_rejected(monkeypatch, repo / "base.txt")
    assert payload["ok"] is False, payload
    codes = [e["code"] for e in payload["errors"]]
    assert codes == ["adapter"], payload
    msg = payload["errors"][0]["msg"].lower()
    assert "no-optional-locks" in msg, payload
    assert "git" in msg, payload


def test_a_real_dirty_repo_still_reports_clean_state_baseline(
        repo: Path) -> None:
    """MUST FIRE. The unmocked positive control: a real modern git is fine."""
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), str(repo / "base.txt")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True, payload
    assert payload["metrics"]["state"] == "modified", payload
