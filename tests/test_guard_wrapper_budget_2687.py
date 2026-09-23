"""#2687: the guard-wrapper spawn timeout must scale on Windows, not sit at
a flat 120s.

`test_the_wrapper_denies_a_replaced_command` timed out at exactly 120s on a
`windows-latest` runner in PR #2683's CI (commit 402490ad) while the same
job's own duration report named it the slowest test in the whole suite at
180.66s on a run that did *not* hit the timeout -- the margin was already
thin on an ordinary run. Same shape as #658/#702: `tests/_adapter_budget.py`
fixed those by deriving the outer budget from the adapter's own inner
timeout and applying `WINDOWS_FACTOR`. The bash-wrapper spawn
(`hooks/pre-bash-guard.sh` under `bash.EXE`) declares no inner timeout of
its own -- there is nothing for `inner_budget()` to find -- so
`wrapper_budget()` applies the same `platform_factor()` multiplier directly
to a flat base instead of deriving one.
"""

from __future__ import annotations

from _adapter_budget import WRAPPER_ENV_OVERRIDE, platform_factor, wrapper_budget


def test_wrapper_budget_scales_an_explicit_base_by_the_platform_factor():
    assert wrapper_budget(120) == 120 * platform_factor()


def test_wrapper_budget_defaults_its_base_to_120():
    assert wrapper_budget() == 120 * platform_factor()


def test_env_override_wins_over_the_platform_factor(monkeypatch):
    monkeypatch.setenv(WRAPPER_ENV_OVERRIDE, "999")
    assert wrapper_budget(120) == 999


def test_a_non_positive_env_override_is_ignored(monkeypatch):
    monkeypatch.setenv(WRAPPER_ENV_OVERRIDE, "-5")
    assert wrapper_budget(120) == 120 * platform_factor()


def test_an_unparseable_env_override_is_ignored(monkeypatch):
    monkeypatch.setenv(WRAPPER_ENV_OVERRIDE, "not-a-number")
    assert wrapper_budget(120) == 120 * platform_factor()
