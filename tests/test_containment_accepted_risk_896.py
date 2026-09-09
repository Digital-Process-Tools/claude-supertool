"""#896 F2/F3 -- two containment edges left as documented accepted risk
rather than closed, and pinned here so the documented boundary matches the
code's actual behavior rather than drifting.

## F2 -- the `@payload` reference itself is not containment-checked

`_resolve_at_path` never calls `_containment_error`/`_safe_path` on the
string after `@`. Verified low severity, not a content-disclosure channel:
a file outside cwd loads and parses, but any PATH-shaped field read out of
it is re-gated independently by the op that reads it, and a parse error
reports only a line/column position, never the file's own text.

## F3 -- TOCTOU between `_containment_error` and the op that follows it

`_containment_error` resolves each candidate once; the op that runs
afterwards resolves the same string again, independently. This is the shape
of every "check, then act" gate backed by two separate syscalls -- pinned
here as a documented, accepted, narrow-window race rather than a closed gap.
"""
from __future__ import annotations

import json

import pytest

import supertool


def test_an_outside_root_at_reference_loads_without_a_containment_error(
        tmp_path, monkeypatch) -> None:
    """F2: the reference itself is not gated -- this is what "not gated"
    actually means in code, not just in the issue's prose."""
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"greeting": "hello"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    loaded = supertool._load_at_file("@" + str(outside))
    assert loaded == {"greeting": "hello"}


def test_an_outside_root_at_reference_parse_error_leaks_no_content(
        tmp_path, monkeypatch) -> None:
    """F2's own severity claim: a broken file outside the root reports a
    position, never the text supertool read (`grep:@/etc/hosts` in the
    issue reads `Expected '=' ... at line 7, column 16`, nothing more)."""
    outside = tmp_path / "outside.toml"
    secret_line = "this-is-not-toml-and-must-not-be-echoed-back"
    outside.write_text(secret_line, encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file("@" + str(outside))
    assert secret_line not in str(excinfo.value)


def test_containment_error_resolves_a_candidate_exactly_once(monkeypatch) -> None:
    """F3, made concrete: `_containment_error` calls `_safe_path` (whose body
    calls `os.path.realpath`) exactly once per candidate. A second resolution
    inside the op that follows is a DIFFERENT syscall this function has no
    way to see or make atomic with its own -- that gap is what #896 F3
    documents rather than closes."""
    calls = []
    real_safe_path = supertool._safe_path

    def counting_safe_path(candidate, **kwargs):
        calls.append(candidate)
        return real_safe_path(candidate, **kwargs)

    monkeypatch.setattr(supertool, "_safe_path", counting_safe_path)
    err = supertool._containment_error(["some_file.txt"])
    assert err is None
    assert calls == ["some_file.txt"]
