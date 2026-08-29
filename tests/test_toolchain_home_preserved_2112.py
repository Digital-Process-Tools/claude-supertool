"""#2112 -- the HOME redirect #2096 added for preset-auth isolation broke
rustup: cargo-check's real-crate tests failed on CI (ubuntu-latest, job
99087584607) because rustup resolves its default toolchain from
RUSTUP_HOME/CARGO_HOME, defaulting to $HOME/.rustup and $HOME/.cargo, and a
redirected HOME with nothing under it leaves rustup with no default to
choose.

tests/conftest.py's fix (_preserve_home_derived_toolchain_config,
EXTERNAL_TOOLCHAIN_HOME_VARS) re-sets RUSTUP_HOME/CARGO_HOME to their real,
pre-redirect locations before HOME moves. Neither of the two machines that
built #2096 and #2112's fix runs a rustup-managed cargo, so nothing in the
local suite exercises the actual rustup failure -- the only tests that would
have caught a regression in the fix itself were the CI legs that carry a
real rustup toolchain, which is exactly the slow, expensive feedback loop
#2112 arrived through in the first place. These tests exercise the
mechanism directly, independent of whether cargo or rustup exist on the
machine running the suite, so a regression here (a var-name typo, the two
statements reordered, an inverted existing/resolved branch) is caught on
every runner rather than only a rustup-equipped one.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import conftest

#: Captured at module import time, before any fixture (autouse or otherwise)
#: has run for any test in this file -- calling os.path.expanduser("~")
#: from inside a test body would read the FAKE home #2096's own autouse
#: fixture has already redirected HOME to by then, not the real one.
REAL_HOME = os.path.expanduser("~")


def test_rustup_and_cargo_home_default_to_the_real_homes_own_subdirectories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No RUSTUP_HOME/CARGO_HOME set anywhere -- the ordinary case on a
    rustup-managed CI runner, which is exactly what broke in #2112. Each
    must resolve to <real_home>/.rustup and <real_home>/.cargo, the same
    defaults rustup itself would have picked from an unredirected HOME."""
    monkeypatch.delenv("RUSTUP_HOME", raising=False)
    monkeypatch.delenv("CARGO_HOME", raising=False)
    real_home = "/not/the/actual/home/for/this/test"

    conftest._preserve_home_derived_toolchain_config(monkeypatch, real_home)

    assert os.environ["RUSTUP_HOME"] == str(Path(real_home) / ".rustup")
    assert os.environ["CARGO_HOME"] == str(Path(real_home) / ".cargo")


def test_an_explicit_rustup_home_already_in_the_environment_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CI image or a developer may point RUSTUP_HOME/CARGO_HOME somewhere
    other than <home>/.rustup on purpose -- this fixture must not invent a
    default over an explicit choice already in the real environment."""
    monkeypatch.setenv("RUSTUP_HOME", "/opt/rust/rustup")
    monkeypatch.setenv("CARGO_HOME", "/opt/rust/cargo")
    real_home = "/not/the/actual/home/for/this/test"

    conftest._preserve_home_derived_toolchain_config(monkeypatch, real_home)

    assert os.environ["RUSTUP_HOME"] == "/opt/rust/rustup"
    assert os.environ["CARGO_HOME"] == "/opt/rust/cargo"


def test_the_autouse_fixture_itself_leaves_rustup_and_cargo_home_pointed_at_the_real_home() -> None:
    """End to end, through the actual autouse fixture every test in this
    suite already runs under (not a call to the helper in isolation): HOME
    is redirected, as #2096 requires, but RUSTUP_HOME/CARGO_HOME still point
    under the real home this test process actually started with -- which is
    the property the CI regression needed and the four preset auth readers
    never cared about either way.
    """
    assert os.environ["HOME"] != REAL_HOME
    assert os.environ["RUSTUP_HOME"] == str(Path(REAL_HOME) / ".rustup")
    assert os.environ["CARGO_HOME"] == str(Path(REAL_HOME) / ".cargo")
