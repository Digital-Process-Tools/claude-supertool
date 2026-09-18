"""ops:grep=PATTERN — filtering the roster without piping through grep (#1318).

`ops` prints every op's signature and refuses an unrecognised argument (#1231)
rather than silently discarding it — but that left `help:OP` / `ops:roster` as
the only routes to "which op does X", neither of which searches. Piping `ops`
through `grep` is what the raw-command guard blocks, correctly, so the only
route left was a redirect-to-temp-file workaround: the detour the guard exists
to prevent, reached by obeying it. Three independent agents hit this in one
week (#1318).

`ops:grep=PATTERN` matches op name, syntax and description, and always states
`N of M` — including `0 of M` — so an empty result can never be misread as a
short roster, the exact absence-as-answer defect `_ops_argument_refusal`'s own
docstring names.

Three more cases found by the self-review round and pinned here rather than
just fixed in prose:

- A pattern containing `:` (searching for `read:PATH`-style syntax, the case
  the op's own docstring names) was silently truncated at the first colon by
  `_split_arg`'s tokenizing — no error, just a quietly narrower search.
- The filter only walked `.supertool.json`'s own sections, so a real,
  dispatchable op with no config entry (`introduction`, `version`, ...) read
  `0 of M matched` — the exact defect #1318 was filed to remove, reintroduced
  one level down.
- Every other pattern slot in this file (`grep`, `around`, `between`, `read`'s
  own `grep=`) is case-sensitive by default; this one was not, silently.
"""

from __future__ import annotations

import supertool


def _fake_config():
    return {
        "builtin-ops": {
            "read": {"syntax": "read:PATH", "description": "Read a file"},
            "paste": {"syntax": "paste:PATH:CONTENT",
                      "description": "Creates a new file on disk"},
        },
        "ops": {
            "custom-op": {"syntax": "custom-op:X",
                          "description": "Does something custom"},
        },
    }


def _expected_total():
    """Documented names plus every dispatchable builtin the fixture omits —
    the same union `op_ops_filter` now searches (#1318 review)."""
    config = _fake_config()
    documented = set(config["builtin-ops"]) | set(config["ops"])
    return len(documented | set(supertool._valid_op_names()))


def test_ops_grep_matches_on_name(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=paste")
    assert "paste:PATH:CONTENT" in out
    assert "read:PATH" not in out
    assert "custom-op:X" not in out


def test_ops_grep_matches_on_description(monkeypatch) -> None:
    """'paste' never appears in this pattern — only its description does."""
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=Creates a new file")
    assert "paste:PATH:CONTENT" in out
    assert "read:PATH" not in out


def test_ops_grep_states_the_count_when_something_matches(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=paste")
    assert f"1 of {_expected_total()}" in out


def test_ops_grep_zero_matches_states_the_count_not_an_empty_listing(monkeypatch) -> None:
    """The whole point (#1318): an empty result must never read like a short roster."""
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=zzzznomatch")
    assert f"0 of {_expected_total()}" in out
    assert "paste" not in out


def test_ops_grep_empty_pattern_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=")
    assert "ERROR" in out


def test_ops_unknown_argument_refusal_names_the_filter(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:zzzz")
    assert "ops:grep=PATTERN" in out


def test_ops_compact_does_not_gain_the_filter(monkeypatch) -> None:
    """Scoped to bare `ops`, matching roster/session/full's own scope (#1231)."""
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops-compact:grep=paste")
    assert "ERROR" in out


# --- self-review round: colon truncation, undocumented ops, case-sensitivity --

def test_ops_grep_pattern_with_colon_is_not_truncated(monkeypatch) -> None:
    """A colon-bearing pattern must reach `op_ops_filter` whole.

    Before the fix, `ops:grep=PATH:CONTENT` was cut at the first colon by
    `_split_arg`'s tokenizer, so the search actually run was `PATH` alone —
    which also matches `read:PATH` and would report 2 matches, not 1.
    """
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=PATH:CONTENT")
    assert "paste:PATH:CONTENT" in out
    assert "read:PATH" not in out
    assert f"1 of {_expected_total()}" in out


def test_ops_grep_triple_colon_form_also_matches_the_single_colon_form(monkeypatch) -> None:
    """`ops:::grep=PATTERN` takes `_dispatch_impl`'s OTHER tokenizer branch
    (`arg.split(":::")`, not `_split_arg`) — it never hit the truncation bug
    the sibling test above pins, since that branch's `parts[1]` already held
    the whole colon-bearing remainder before this fix existed. Kept as a
    same-answer regression check between the two invocation forms, not as a
    second instance of the truncation fix (self-review round 2 caught the
    original docstring overclaiming that)."""
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    single = supertool.dispatch("ops:grep=PATH:CONTENT")
    triple = supertool.dispatch("ops:::grep=PATH:CONTENT")
    assert single == triple
    assert "paste:PATH:CONTENT" in triple
    assert "read:PATH" not in triple


def test_ops_grep_finds_a_dispatchable_op_with_no_config_entry(monkeypatch) -> None:
    """`version` is a real, callable op absent from the fake config's
    `builtin-ops` section — the filter must still find it rather than
    reading `0 of M matched` for an op that plainly exists (#1318 review)."""
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=^version$")
    assert "`version`" in out
    assert "0 of" not in out


def test_ops_grep_empty_config_lists_names_not_zero_of_zero(monkeypatch) -> None:
    """No `.supertool.json` at all must not read as zero ops existing —
    `op_ops()` already falls back to built-in names for this state (#1318 review)."""
    monkeypatch.setattr(supertool, "_CONFIG", {})
    out = supertool.dispatch("ops:grep=version")
    assert "0 of 0" not in out
    assert "version" in out


def test_ops_grep_is_case_sensitive(monkeypatch) -> None:
    """Matches every other pattern slot in this file (`grep`, `around`,
    `between`, `read`'s own `grep=`), which are case-sensitive by default."""
    monkeypatch.setattr(supertool, "_CONFIG", _fake_config())
    out = supertool.dispatch("ops:grep=PASTE")
    assert f"0 of {_expected_total()}" in out
