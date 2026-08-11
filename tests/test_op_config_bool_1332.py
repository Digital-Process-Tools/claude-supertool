"""#1332 — an int-backed setting could not be switched off, and said nothing.

`_get_op_int` ends in ``val if isinstance(val, int) and val > 0 else default``,
so a configured ``0`` in ``.supertool.json`` is indistinguishable from an absent
key and the helper's own default wins. That is right for the thresholds it was
built for — ``max_lines``, ``max_bytes``, ``count_ceiling``, ``char_window``,
where a zero is meaningless — and wrong for anything boolean-shaped whose
default is on, which is how ``read.elide`` shipped documented and inert.

Two halves here, and they are separable on purpose:

* ``_get_op_bool`` — the helper a switch belongs on, where ``0`` means off.
* ``_get_op_int`` — still refuses a non-positive threshold, but now **says so**,
  the way it already names a bad env value. The deciding question in the issue
  was whether a caller writing ``0`` can tell from the output that it was
  ignored; before this they could not.
"""
from __future__ import annotations

import pytest

import supertool


@pytest.fixture
def config(monkeypatch):
    """Install a `.supertool.json` body without touching the filesystem.

    Also clears the two env vars that would otherwise decide these answers
    before the config is consulted. `conftest` sets `SUPERTOOL_READ_NO_ELIDE=1`
    for the whole run so no test touches the developer's real elide cache — a
    test asserting the config default would read that as its own result.
    """
    monkeypatch.delenv("SUPERTOOL_READ_NO_ELIDE", raising=False)
    monkeypatch.delenv("SUPERTOOL_READ_ELIDE", raising=False)

    def _install(body):
        monkeypatch.setattr(supertool, "_CONFIG", body)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    return _install


# ---------------------------------------------------------------------------
# _get_op_bool — a switch can be turned off
# ---------------------------------------------------------------------------

def test_configured_zero_turns_a_default_on_switch_off(config):
    """The issue's reproduction, against the helper rather than one call site."""
    config({"builtin-ops": {"read": {"elide": 0}}})
    assert supertool._get_op_bool("read", "elide", True) is False


def test_configured_one_turns_a_default_off_switch_on(config):
    config({"builtin-ops": {"read": {"abstract": 1}}})
    assert supertool._get_op_bool("read", "abstract", False) is True


def test_absent_key_keeps_the_default(config):
    config({"builtin-ops": {"read": {}}})
    assert supertool._get_op_bool("read", "elide", True) is True
    assert supertool._get_op_bool("read", "abstract", False) is False


def test_json_true_and_false_are_honoured(config):
    config({"builtin-ops": {"read": {"elide": False}}})
    assert supertool._get_op_bool("read", "elide", True) is False
    config({"builtin-ops": {"read": {"elide": True}}})
    assert supertool._get_op_bool("read", "elide", True) is True


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("FALSE", False), ("no", False),
    ("off", False), ("", True),
    ("1", True), ("true", True), ("yes", True), ("on", True),
])
def test_string_forms(config, raw, expected):
    """`.supertool.json` is hand-written, so `"0"` and `"false"` arrive too."""
    config({"builtin-ops": {"read": {"elide": raw}}})
    assert supertool._get_op_bool("read", "elide", True) is expected


def test_env_overrides_config_both_ways(config, monkeypatch):
    config({"builtin-ops": {"read": {"elide": 0}}})
    monkeypatch.setenv("SUPERTOOL_READ_ELIDE", "1")
    assert supertool._get_op_bool("read", "elide", True) is True
    monkeypatch.setenv("SUPERTOOL_READ_ELIDE", "0")
    assert supertool._get_op_bool("read", "elide", True) is False


def test_unusable_value_is_announced_not_swallowed(config, capsys):
    """An unreadable switch is `skipped`, and says which state is in force."""
    config({"builtin-ops": {"read": {"elide": [1]}}})
    value = supertool._get_op_bool("read", "elide", True)
    out = capsys.readouterr().out
    assert value is True
    assert "read.elide" in out
    assert "using True" in out, out


def test_unusable_env_is_announced(config, monkeypatch, capsys):
    config({})
    monkeypatch.setenv("SUPERTOOL_READ_ELIDE", "maybe")
    assert supertool._get_op_bool("read", "elide", True) is True
    out = capsys.readouterr().out
    assert "SUPERTOOL_READ_ELIDE" in out
    assert "'maybe'" in out
    assert "using True" in out, out


# ---------------------------------------------------------------------------
# _get_op_int — still a positive threshold, but no longer silent about it
# ---------------------------------------------------------------------------

def test_int_threshold_zero_is_still_refused(config):
    """`read.max_lines: 0` must NOT become "read no lines".

    Honouring a zero here would trade a loud misconfiguration for a silent
    empty read, which is the worse of the two failures.
    """
    config({"builtin-ops": {"read": {"max_lines": 0}}})
    assert supertool._get_op_int("read", "max_lines", 300) == 300


def test_int_threshold_zero_is_announced(config, capsys):
    config({"builtin-ops": {"read": {"max_lines": 0}}})
    value = supertool._get_op_int("read", "max_lines", 300)
    out = capsys.readouterr().out
    assert "read.max_lines" in out, out
    assert "0" in out
    assert f"using {value}" in out, out


def test_int_threshold_negative_is_announced(config, capsys):
    config({"builtin-ops": {"grep": {"max_results": -5}}})
    supertool._get_op_int("grep", "max_results", 10)
    assert "grep.max_results" in capsys.readouterr().out


def test_int_threshold_json_true_is_not_a_threshold(config, capsys):
    """`isinstance(True, int)` is True, so `"max_lines": true` read as 1.

    A one-line read is not what anybody writing that meant, and nothing said a
    word about it.
    """
    config({"builtin-ops": {"read": {"max_lines": True}}})
    assert supertool._get_op_int("read", "max_lines", 300) == 300
    assert "read.max_lines" in capsys.readouterr().out


def test_int_good_value_stays_silent(config, capsys):
    config({"builtin-ops": {"read": {"max_lines": 42}}})
    assert supertool._get_op_int("read", "max_lines", 300) == 42
    assert capsys.readouterr().out == ""


def test_int_absent_key_stays_silent(config, capsys):
    """The commonest case by far — a notice here would be on every read."""
    config({})
    assert supertool._get_op_int("read", "max_lines", 300) == 300
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# The two call sites that carry a flag now ride the bool helper
# ---------------------------------------------------------------------------

def test_read_elide_off_by_config(config):
    config({"builtin-ops": {"read": {"elide": 0}}})
    assert supertool._read_elide_enabled() is False


def test_read_elide_on_by_default(config):
    config({})
    assert supertool._read_elide_enabled() is True


def test_read_elide_env_escape_still_works(config, monkeypatch):
    config({})
    monkeypatch.setenv("SUPERTOOL_READ_NO_ELIDE", "1")
    assert supertool._read_elide_enabled() is False


def test_abstract_switch_reads_as_a_bool(config):
    config({"builtin-ops": {"read": {"abstract": 0}}})
    assert supertool._get_op_bool("read", "abstract", False) is False
    config({"builtin-ops": {"read": {"php_abstract": 1}}})
    assert supertool._get_op_bool("read", "php_abstract", False) is True
