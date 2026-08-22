"""#1871 -- both radar tiers asserted `killed by signal N` on a returncode
nothing observed on Windows.

`gh_prs.py` and `gl_mrs.py` both fire this arm on `result.returncode < 0`. On
POSIX `subprocess` documents `-N` for a signal, so the old wording was exact
there. On Windows the same field carries the process exit status, and
`_winapi.GetExitCodeProcess` is declared `unsigned long`, so a negative
value should not arrive at all -- but the negative spelling of the same DWORD
circulates widely enough (shells, Python 2) that nobody here trusts it without
a Windows runner to check it on. Reasoned, not observed.

So the fix is wording, not a platform branch: the message states the
returncode, which is true on every platform, and hedges the signal reading
rather than asserting it. Nothing here drives a real subprocess; `subprocess.run`
is replaced with a fake result.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh_tier = _load("watch_gh_prs_1871", WATCH_DIR / "tiers" / "gh_prs.py")
gl_tier = _load("watch_gl_mrs_1871", WATCH_DIR / "tiers" / "gl_mrs.py")


def _fake_run(returncode: int, stderr: str = ""):
    def _run(*_a, **_k):
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)
    return _run


def _run_gh(monkeypatch, returncode: int, stderr: str = "") -> None:
    monkeypatch.setattr(gh_tier.subprocess, "run", _fake_run(returncode, stderr))


def _run_gl(monkeypatch, returncode: int, stderr: str = "") -> None:
    monkeypatch.setattr(gl_tier.mrs, "_run", _fake_run(returncode, stderr))


@pytest.mark.parametrize("tier,call,driver", [
    (gh_tier, lambda: gh_tier.live_open_prs({}), _run_gh),
    (gl_tier, lambda: gl_tier._query({}, 20), _run_gl),
])
def test_a_negative_returncode_states_the_number_and_hedges_the_mechanism(
        tier, call, driver, monkeypatch) -> None:
    driver(monkeypatch, -9, "oom")
    with pytest.raises(tier.RadarUnreachable) as caught:
        call()
    msg = str(caught.value)
    # The returncode is true on every platform -- always state it.
    assert "-9" in msg, msg
    # The old, unconditional claim must be gone: on Windows nobody has
    # observed whether this arm even fires, let alone that the mechanism is
    # a POSIX signal.
    assert "was killed by signal" not in msg, msg
    # The hedge: still legible to a POSIX reader, no longer asserted as fact
    # for a platform nobody has checked.
    assert "posix" in msg.lower(), msg
    assert "windows" in msg.lower(), msg

    # must-fire, same fixture: a returncode that is NOT negative must not
    # produce this arm's wording at all -- without this the assertions above
    # would pass on a function that always raises this message.
    driver(monkeypatch, 1, "not found")
    with pytest.raises(tier.RadarError) as caught2:
        call()
    assert "posix" not in str(caught2.value).lower()


def test_both_tiers_use_the_same_wording(monkeypatch) -> None:
    """One change to both tiers, not a divergence -- the issue's own framing."""
    _run_gh(monkeypatch, -9, "x")
    _run_gl(monkeypatch, -9, "x")
    with pytest.raises(gh_tier.RadarUnreachable) as gh_caught:
        gh_tier.live_open_prs({})
    with pytest.raises(gl_tier.RadarUnreachable) as gl_caught:
        gl_tier._query({}, 20)
    gh_msg = str(gh_caught.value)
    gl_msg = str(gl_caught.value)
    # Both hedge the same way -- only the tool name differs.
    assert gh_msg.replace("gh pr list", "X") == gl_msg.replace("glab mr list", "X"), (
        gh_msg, gl_msg)
