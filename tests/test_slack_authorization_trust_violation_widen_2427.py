"""#2427 follow-up (found by self-review's Explore pass) -- a project
`.supertool.json` skipped for TRUST reasons (group/world-writable, or owned
by a different local user -- `_config_trust_violation`) reopened the exact
silent-widen bug the malformed-JSON fix closed, just triggered by a
permission/ownership change instead of a JSON parse error.

`load_project_config_result()`'s walk continues past a trust-violated
candidate looking for a further, trusted config higher up (`#2416`'s
design, unchanged and correct on its own). But if the walk then reaches
the repo-root boundary with NO trusted candidate ever found, it returned
`ProjectConfigResult(data=None, error=None)` -- byte-identical to "this
project never configured Slack at all" -- even though a candidate WAS
found and skipped for being untrustworthy. `resolve_channel` only fails
closed when `.error` is set, so this fell through to the machine-owner's
(possibly wider) base level, exactly the #2427 shape.

TDD: red against the pre-fix code (walk exhausts with a skipped candidate,
`.error` is None, `resolve_channel` widens); green once the walk remembers
whether anything was skipped for trust reasons and reports an error at
the exhausted-walk boundary rather than a clean absence.

Positive control: a walk with NO candidate ever skipped for trust reasons
must still resolve `.error is None` -- the ordinary "no config at all"
case must not start looking like a trust violation happened.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


auth = _load("presets/slack/_authorization.py", "slack_authorization_trustwiden_2427")


def _write_home_config(tmp_path: Path, data: dict) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "supertool").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "supertool" / "slack_authorization.json").write_text(
        json.dumps(data), encoding="utf-8")
    return home


def _set_home(monkeypatch, path) -> None:
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    assert Path(os.path.expanduser("~")) == Path(str(path))


def _walk_from(project_dir: Path) -> "auth.ProjectConfigResult":
    old_cwd = os.getcwd()
    os.chdir(project_dir)
    try:
        return auth.load_project_config_result()
    finally:
        os.chdir(old_cwd)


def test_a_walk_exhausted_after_a_trust_violation_is_not_a_clean_absence(
    monkeypatch, tmp_path,
) -> None:
    """No further trusted candidate exists above the group-writable one --
    the walk must not report this the same way as 'nothing was ever
    there'."""
    if os.name != "posix":
        import pytest
        pytest.skip("group/world-writable mode bits are POSIX-only")

    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()
    cfg = project_dir / ".supertool.json"
    cfg.write_text(json.dumps({"slack": {"channels": {"C0123": "off"}}}),
                   encoding="utf-8")
    cfg.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IWOTH)  # world-writable

    result = _walk_from(project_dir)

    assert result.data is None
    assert result.error is not None, (
        "a group/world-writable .supertool.json was skipped for being "
        "untrustworthy, and no further trusted candidate exists above it "
        "-- .error must say so rather than reading exactly like no "
        "config ever existed here"
    )


def test_no_trust_violation_anywhere_stays_the_clean_absence(
    monkeypatch, tmp_path,
) -> None:
    """Positive control: an ordinary, never-configured project must not
    start reporting an error just because the trust-violation case now
    does."""
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    result = _walk_from(project_dir)

    assert result.data is None
    assert result.error is None


def test_the_end_to_end_widen_is_closed_for_trust_violations_too(
    monkeypatch, tmp_path,
) -> None:
    """Same end-to-end shape as the malformed-JSON regression test:
    the machine-owner grants `allowlist`, the project tried to narrow to
    `off` but its config is untrustworthy (world-writable) with nothing
    trusted above it -- the channel must fail closed, not widen."""
    if os.name != "posix":
        import pytest
        pytest.skip("group/world-writable mode bits are POSIX-only")

    home = _write_home_config(tmp_path, {"channels": {
        "C0123": {"level": "allowlist", "users": ["U024BE7LH"]}}})
    _set_home(monkeypatch, str(home))

    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()
    cfg = project_dir / ".supertool.json"
    cfg.write_text(json.dumps({"slack": {"channels": {"C0123": "off"}}}),
                   encoding="utf-8")
    cfg.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IWOTH)

    project_result = _walk_from(project_dir)
    d = auth.resolve_channel("C0123", user_id="U024BE7LH",
                             project_config=project_result.data,
                             project_config_error=project_result.error)

    assert d.level == "off", (
        f"a trust-violated project config that had narrowed this channel "
        f"silently widened to the machine-owner's base level: {d.level!r} "
        f"(detail={d.detail!r})"
    )
