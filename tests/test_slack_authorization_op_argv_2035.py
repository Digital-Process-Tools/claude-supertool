"""#2035 -- `slack_authorization` op's argv handling.

The bug this file exists to pin: core splits `op:CHANNEL_ID:USER_ID` into
SEPARATE argv entries (the same shape `gh-job.py:JOB_ID:MODE` uses), not one
colon-joined string. An earlier draft of `auth.py::main` did
`toks[0].split(":")`, which is correct for a single joined token and wrong
for what core actually hands the script -- it silently dropped USER_ID on
every real call, so `may_instruct` always rendered as "no user_id given"
even when one was typed. Caught by running the op end-to-end, not by this
file's own harness, which is exactly why it earns a regression test.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


auth_op = _load("presets/slack/auth.py", "slack_auth_op_2035")


def _write_config(tmp_path: Path, data: dict) -> Path:
    import json
    home = tmp_path / "home"
    (home / ".config" / "supertool").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "supertool" / "slack_authorization.json").write_text(
        json.dumps(data), encoding="utf-8")
    return home


def _set_home(monkeypatch, path) -> None:
    """Point `~` at `path` on every platform `expanduser` supports.

    POSIX reads HOME; Windows (`ntpath.expanduser`) prefers USERPROFILE and
    falls back to HOMEDRIVE+HOMEPATH -- checked BEFORE HOME, not after, so a
    test that sets only HOME has zero effect on Windows and silently keeps
    reading whatever `tests/conftest.py::_no_real_preset_credentials`
    (an autouse fixture) already pointed USERPROFILE at for every test in
    this suite. Same pattern as `tests/test_tilde_path_1300.py::home`.
    """
    import os as _os
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    assert Path(_os.path.expanduser("~")) == Path(str(path)), (
        "expanduser(\"~\") did not resolve to the fake home on this "
        "platform -- the whole point of this helper (same check "
        "tests/test_tilde_path_1300.py::home already makes)"
    )



def test_channel_and_user_id_arrive_as_separate_argv_entries(
    monkeypatch, tmp_path, capsys,
) -> None:
    """Positive control for the real bug: USER_ID must reach `_one`, not be
    dropped on the floor because it arrived as `argv[1]` rather than glued
    onto `argv[0]` with a colon."""
    home = _write_config(tmp_path, {"channels": {
        "C0123456": {"level": "allowlist", "users": ["U024BE7LH"]}}})
    _set_home(monkeypatch, str(home))
    monkeypatch.chdir(tmp_path)

    rc = auth_op.main(["C0123456", "U024BE7LH"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "may_instruct: True" in out, out


def test_a_channel_with_no_user_id_argv_still_resolves(
    monkeypatch, tmp_path, capsys,
) -> None:
    home = _write_config(tmp_path, {"channels": {"C0123456": {"level": "context"}}})
    _set_home(monkeypatch, str(home))
    monkeypatch.chdir(tmp_path)

    rc = auth_op.main(["C0123456"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "no user_id given" in out


def test_no_argv_at_all_lists_every_declared_channel(
    monkeypatch, tmp_path, capsys,
) -> None:
    home = _write_config(tmp_path, {"channels": {
        "C_A": {"level": "context"}, "C_B": {"level": "off"}}})
    _set_home(monkeypatch, str(home))
    monkeypatch.chdir(tmp_path)

    rc = auth_op.main([])
    out = capsys.readouterr().out

    assert rc == 0
    assert "C_A" in out and "C_B" in out
