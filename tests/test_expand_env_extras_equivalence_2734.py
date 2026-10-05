"""#2734 -- what a child command's `$VAR` expansion must keep doing if the
whole-environment copy that feeds it goes away.

Five call sites (`_supertool_presets.py`'s preset launcher, four in
`_supertool_validate.py`) build `env = os.environ.copy()`, merge a few EXTRAS
into it (`SUPERTOOL_ARG_SEP`, `SUPERTOOL_<KEY>` per op-config key, the cmd's
own `KEY=VALUE` prefix), and hand the merged mapping to `_expand_env`. The
portal's line scanner reads that bound copy, looked up by a name computed at
run time, as "an environment variable read through an alias of the
environment object".

These tests pin the contract of that merged expansion -- `_expand_env(cmd,
{**os.environ, **extras})` -- form by form, so that any replacement which
stops binding the copy is held to the same outputs. `IMPLEMENTATIONS` is the
list a replacement joins.

The forms that matter, because `os.path.expandvars` -- the obvious stdlib
substitute -- gets each of them wrong:

- an env-only value is `shlex.quote`d (#2290: a Windows path's backslashes
  otherwise vanish in `shlex.split`; a value with a space otherwise splits);
- inside the template's own single quotes it is spliced, not re-quoted
  (#2291) -- and `ntpath.expandvars` does not expand inside `'...'` at all;
- `%VAR%` is NOT expanded on any platform -- `ntpath.expandvars` does.

Each "stays literal" case is paired with an "expands" case on the same name
in the same fixture, so a no-op implementation fails here too.
"""
from __future__ import annotations

import os
import shlex

import pytest

import supertool


def _merged_copy(cmd: str, extras: dict) -> str:
    """Today's shape at every call site: a whole-environment copy, extras on top."""
    return supertool._expand_env(cmd, {**os.environ, **extras})


def _extras_only(cmd: str, extras: dict) -> str:
    """The shape every call site now uses: only the extras, no environ copy.

    `_expand_env` resolves a name the extras do not hold through
    `os.path.expandvars("${NAME}")`, one token at a time."""
    return supertool._expand_env(cmd, extras)


IMPLEMENTATIONS = [
    pytest.param(_merged_copy, id="merged-copy"),
    pytest.param(_extras_only, id="extras-only"),
]


@pytest.fixture
def envvars(monkeypatch):
    monkeypatch.setenv("ST2734_PLAIN", "plain")
    monkeypatch.setenv("ST2734_SPACED", "a b")
    monkeypatch.setenv("ST2734_WINPATH", "C:\\Users\\runner\\x")
    monkeypatch.setenv("ST2734_SHARED", "from-env")
    monkeypatch.delenv("ST2734_UNSET", raising=False)


@pytest.mark.parametrize("expand", IMPLEMENTATIONS)
def test_an_extra_wins_over_an_env_var_of_the_same_name(expand, envvars):
    assert expand("t $ST2734_SHARED", {"ST2734_SHARED": "from-extra"}) == "t from-extra"
    # positive control: without the extra, the env value is what expands
    assert expand("t $ST2734_SHARED", {}) == "t from-env"


@pytest.mark.parametrize("expand", IMPLEMENTATIONS)
def test_an_env_only_var_expands_in_both_spellings(expand, envvars):
    assert expand("t $ST2734_PLAIN ${ST2734_PLAIN}", {}) == "t plain plain"


@pytest.mark.parametrize("expand", IMPLEMENTATIONS)
def test_an_unknown_var_stays_as_written(expand, envvars):
    assert expand("t $ST2734_UNSET ${ST2734_UNSET}", {}) == "t $ST2734_UNSET ${ST2734_UNSET}"
    # positive control on the same template, once the name is known
    assert expand("t $ST2734_UNSET", {"ST2734_UNSET": "v"}) == "t v"


@pytest.mark.parametrize("expand", IMPLEMENTATIONS)
def test_an_env_only_value_is_shell_quoted_outside_template_quotes(expand, envvars):
    assert shlex.split(expand("t $ST2734_SPACED", {})) == ["t", "a b"]
    assert shlex.split(expand("t $ST2734_WINPATH", {})) == ["t", "C:\\Users\\runner\\x"]


@pytest.mark.parametrize("expand", IMPLEMENTATIONS)
def test_an_env_only_value_is_spliced_inside_template_single_quotes(expand, envvars):
    out = expand("bash -c 'echo $ST2734_SPACED'", {})
    assert shlex.split(out) == ["bash", "-c", "echo a b"]


@pytest.mark.parametrize("expand", IMPLEMENTATIONS)
def test_percent_form_is_never_expanded(expand, envvars):
    assert expand("t %ST2734_PLAIN%", {}) == "t %ST2734_PLAIN%"
    assert expand("t $ST2734_PLAIN", {}) == "t plain"


@pytest.mark.parametrize("expand", IMPLEMENTATIONS)
def test_double_dollar_is_a_literal_dollar_then_a_reference(expand, envvars):
    assert expand("t $$ST2734_PLAIN", {}) == "t $plain"


@pytest.mark.parametrize("expand", IMPLEMENTATIONS)
def test_a_digit_led_name_is_not_a_reference(expand, envvars, monkeypatch):
    monkeypatch.setenv("1", "one")
    assert expand("t $1 ${1}", {}) == "t $1 ${1}"

def test_known_gap_a_value_equal_to_its_own_reference_reads_as_unset(monkeypatch):
    """Documented in `_expand_env`'s docstring: `expandvars` returns the probe
    unchanged both for an unset name and for one whose value IS the probe, so
    the extras-only lookup cannot tell them apart. The merged copy could. This
    pins the gap so a change to it is a decision, not an accident."""
    monkeypatch.setenv("ST2734_SELF", "${ST2734_SELF}")
    monkeypatch.setenv("ST2734_PLAIN", "plain")
    assert _merged_copy("t $ST2734_SELF", {}) == "t '${ST2734_SELF}'"
    assert _extras_only("t $ST2734_SELF", {}) == "t $ST2734_SELF"
    # positive control: the same lookup does resolve an ordinary env value
    assert _extras_only("t $ST2734_PLAIN", {}) == "t plain"
