"""git-push does not point at the watch op where the build does not ship it (#2746).

The directory build denies presets/watch.json (#2734), yet `_watch_advisory`
ended every push that has an open PR with "Watch pipeline: supertool
'watch:...'", a command that answers "unknown operation" there. It now asks
whether presets/watch.json sits beside presets/git/; the full build is pinned
unchanged beside it.
"""
import importlib.util
import types
from pathlib import Path

import pytest

PUSH = Path(__file__).resolve().parent.parent / "presets" / "git" / "push.py"


@pytest.fixture()
def push(monkeypatch):
    spec = importlib.util.spec_from_file_location("push_2746", PUSH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_watch_target", lambda mr: ("github-pr", "12"))
    return mod


LOOKUP = types.SimpleNamespace(answered=True, mr={"number": 12}, reason="")


def test_full_build_still_offers_the_watch(push, monkeypatch, capsys) -> None:
    monkeypatch.setattr(push, "_watch_op_shipped", lambda: True)
    push._watch_advisory(LOOKUP, set())
    assert "watch:github-pr:12" in capsys.readouterr().out


def test_directory_build_offers_nothing_unprompted(push, monkeypatch, capsys) -> None:
    monkeypatch.setattr(push, "_watch_op_shipped", lambda: False)
    push._watch_advisory(LOOKUP, set())
    assert "watch:" not in capsys.readouterr().out


def test_directory_build_answers_a_requested_watch(push, monkeypatch, capsys) -> None:
    monkeypatch.setattr(push, "_watch_op_shipped", lambda: False)
    push._watch_advisory(LOOKUP, {"watch"})
    out = capsys.readouterr().out
    assert "not in this build" in out and "supertool-cli@dpt-plugins" in out, out


def test_the_probe_reads_the_real_tree(push) -> None:
    assert push._watch_op_shipped() is (PUSH.parent.parent / "watch.json").is_file()
