"""The GitLab tier's failure kinds are types a caller can dispatch on (#1847).

`gh_prs.py` has told its two failure states apart by class since #1568 --
`RadarUnreachable` for "the request never landed", `RadarUnconfigured` for "no
credential is configured here". `gl_mrs.py` had neither: every failure, from a
`glab` that was never spawned to a JSON body that arrived and was wrong, was one
`RadarError` carrying different prose.

That is this repository's own defect class at the type level. A state that
exists only in a message renders, to a caller, exactly like a state that was
never distinguished -- and the caller that wants it is the one that must decide
whether to retry (the forge did not answer) or to stop (the board says X).

**Both shapes the issue offered pass a per-tier test.** The one that does not is
`test_the_two_tiers_name_one_class_object`: each tier's `_load` helper builds a
fresh module object per call, so "lift the classes to a shared file" done the
obvious way yields two unrelated `RadarError` classes with the same name and
fixes nothing a cross-tier caller can see. That test is the whole reason the
shared loader registers in `sys.modules`.

**Nothing here reads a message string.** Reading one is what the issue says
would mean nothing was fixed, so every assertion below is `isinstance` or `is`.
And every "must not be unreachable" case is paired with a "must be unreachable"
case driven through the same fake -- an assertion that a class is *absent*
passes on a tier that raises nothing at all.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WATCH_DIR = ROOT / "presets" / "watch"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh_tier = _module("radar_errors_1847_gh", WATCH_DIR / "tiers" / "gh_prs.py")
gl_tier = _module("radar_errors_1847_gl", WATCH_DIR / "tiers" / "gl_mrs.py")


class _Result:
    def __init__(self, out: str = "", err: str = "", code: int = 0):
        self.stdout, self.stderr, self.returncode = out, err, code


def _fake_glab(monkeypatch, *, out: str | None = None, err: str = "",
               code: int = 0, raises: BaseException | None = None):
    def _run(*_a, **_k):
        if raises is not None:
            raise raises
        body = json.dumps([]) if out is None else out
        return _Result(body, err, code)
    monkeypatch.setattr(gl_tier.mrs, "_run", _run)


def _gl_raises(monkeypatch, **kw) -> BaseException:
    """Drive `_query` to its failure and hand back the exception object."""
    _fake_glab(monkeypatch, **kw)
    with pytest.raises(gl_tier.RadarError) as caught:
        gl_tier._query({}, 20)
    return caught.value


# ---------------------------------------------------------------------------
# the classes exist, and they are one set rather than two
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["RadarError", "RadarUnreachable",
                                  "RadarUnconfigured"])
def test_both_tiers_expose_the_same_three_names(name: str) -> None:
    assert hasattr(gh_tier, name), f"gh_prs lost {name}"
    assert hasattr(gl_tier, name), (
        f"gl_mrs exposes no {name}, so a caller of the GitLab tier cannot "
        f"write the except arm the GitHub tier's callers already write")


@pytest.mark.parametrize("name", ["RadarError", "RadarUnreachable",
                                  "RadarUnconfigured"])
def test_the_two_tiers_name_one_class_object(name: str) -> None:
    """`gh_prs.X is gl_mrs.X`. The point of the lift, and the part that is easy
    to get wrong invisibly.

    Each tier resolves its helpers through its own `_load`, which builds a new
    module object every call. Two tiers loading the same *file* that way get two
    unrelated classes with identical names and identical docstrings -- and every
    per-tier test still passes, because each tier catches its own. A caller
    holding one tier's class and an exception from the other sees no match, and
    the failure is a silent `except` that never fires.
    """
    assert getattr(gh_tier, name) is getattr(gl_tier, name), (
        f"{name} is a different class object in each tier, so an "
        f"`except gh_prs.{name}` will not catch the GitLab tier's -- the "
        f"shared module has to be registered in sys.modules, not re-executed")


def test_the_subclass_chain_keeps_every_existing_except_radarerror_working() -> None:
    """The reason both new classes are subclasses rather than siblings: radar's
    tier isolation and `radar_state`'s filter arm catch `RadarError` and must
    keep behaving exactly as they did."""
    assert issubclass(gl_tier.RadarUnreachable, gl_tier.RadarError)
    assert issubclass(gl_tier.RadarUnconfigured, gl_tier.RadarUnreachable)


# ---------------------------------------------------------------------------
# the GitLab tier now answers with a type. Positive control first: without
# these, every "is not RadarUnreachable" case below would pass on a tier that
# had stopped raising anything.
# ---------------------------------------------------------------------------

def test_a_glab_that_was_never_spawned_is_unreachable(monkeypatch) -> None:
    """The spawn itself did not complete, so nothing was asked of GitLab."""
    exc = _gl_raises(monkeypatch, raises=FileNotFoundError("no glab here"))
    assert isinstance(exc, gl_tier.RadarUnreachable)


def test_a_glab_killed_by_a_signal_is_unreachable(monkeypatch) -> None:
    """A negative returncode is `subprocess` reporting a signal. A process that
    was killed did not finish deciding anything -- and under a loaded runner the
    OOM killer lands here with empty stderr, which the fallback arm would
    otherwise render as a verdict about the board."""
    exc = _gl_raises(monkeypatch, code=-9, err="")
    assert isinstance(exc, gl_tier.RadarUnreachable)


def test_an_answer_saying_the_credential_is_unusable_is_unreachable(
        monkeypatch) -> None:
    exc = _gl_raises(monkeypatch, code=1,
                     err="GET https://gitlab.com/api/v4/merge_requests: "
                         "401 Unauthorized")
    assert isinstance(exc, gl_tier.RadarUnreachable)


# ---------------------------------------------------------------------------
# ...and the failures that are NOT about reaching GitLab stay plain. Each one
# is a reply that arrived: what it said is a finding about the boundary, and
# calling it unreachable would tell a loop to retry something that will fail
# identically forever.
# ---------------------------------------------------------------------------

def test_a_product_failure_nothing_explains_is_not_unreachable(
        monkeypatch) -> None:
    exc = _gl_raises(monkeypatch, code=1,
                     err="the project 'acme/widget' could not be found")
    assert isinstance(exc, gl_tier.RadarError)
    assert not isinstance(exc, gl_tier.RadarUnreachable), (
        "glab answered and said what was wrong; that is a verdict about the "
        "board, not a transport failure")


def test_a_body_that_arrived_and_would_not_parse_is_not_unreachable(
        monkeypatch) -> None:
    exc = _gl_raises(monkeypatch, out="{not json", code=0)
    assert isinstance(exc, gl_tier.RadarError)
    assert not isinstance(exc, gl_tier.RadarUnreachable)


def test_a_body_that_arrived_and_was_the_wrong_shape_is_not_unreachable(
        monkeypatch) -> None:
    exc = _gl_raises(monkeypatch, out=json.dumps({"message": "nope"}), code=0)
    assert isinstance(exc, gl_tier.RadarError)
    assert not isinstance(exc, gl_tier.RadarUnreachable)


# ---------------------------------------------------------------------------
# the caller's own question, asked the way a caller asks it
# ---------------------------------------------------------------------------

def test_a_caller_sorts_the_two_kinds_without_reading_a_message(
        monkeypatch) -> None:
    """The issue's acceptance test. Two GitLab failures of different kinds,
    sorted by `except` alone."""
    def _classify(**kw) -> str:
        _fake_glab(monkeypatch, **kw)
        try:
            gl_tier._query({}, 20)
        except gl_tier.RadarUnreachable:
            return "retry"
        except gl_tier.RadarError:
            return "verdict"
        return "no failure at all"

    assert _classify(raises=OSError("connection reset")) == "retry"
    assert _classify(code=1, err="unknown option --milestne") == "verdict"
