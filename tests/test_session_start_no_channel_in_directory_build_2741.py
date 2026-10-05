"""The directory build has no `channel` op, so the session hook must not call it.

`.github/release-branch.json` denies `presets/watch.json` (#2734), which is
where `channel` is declared. `hooks/session_start.py` still ran
`channel:stranded` after the op listing, so every session on a directory
install began with "ERROR: unknown operation: channel" and the whole list of
valid operations (seen on the built tree, 2026-10-05, #2741).

The hook now asks for the stranded report only when the plugin root ships
`presets/watch.json`. Both directions are pinned: a negative alone would also
pass if the hook never called anything.
"""
import importlib.util
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "session_start.py"


def _load():
    spec = importlib.util.spec_from_file_location("session_start_2741", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _calls(tmp_path, monkeypatch, ship_watch: bool):
    if ship_watch:
        (tmp_path / "presets").mkdir()
        (tmp_path / "presets" / "watch.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    mod = _load()
    seen = []
    monkeypatch.setattr(mod.subprocess, "call", lambda argv, **kw: seen.append(argv[2:]) or 0)
    mod.onboard(str(tmp_path / "supertool.py"))
    return seen


def test_directory_build_does_not_ask_for_the_channel(tmp_path, monkeypatch) -> None:
    seen = _calls(tmp_path, monkeypatch, ship_watch=False)
    assert ["introduction", "output-format", "ops:session"] in seen, seen
    assert ["channel:stranded"] not in seen, seen


def test_full_build_still_asks_for_the_channel(tmp_path, monkeypatch) -> None:
    seen = _calls(tmp_path, monkeypatch, ship_watch=True)
    assert ["channel:stranded"] in seen, seen
