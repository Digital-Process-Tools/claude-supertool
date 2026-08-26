"""quote_open_guess reaches the receipt a caller actually reads (#1810).

The adapter can compute the right hint, but nothing was worth building if the
renderer that turns a validator payload into what a human or agent sees never
walks the new key -- `_validator_render_row` and `_validator_render_diff` are
the only two places that do that, and neither knew about `quote_open_guess`
before this. `_validator_render_diff` in particular is the one shown at the
exact moment #1810 is about: right after an edit gets rolled back.
"""
from __future__ import annotations

import supertool


ERR = {"line": 8, "code": "syntax", "msg": "syntax error: unexpected end of file",
       "quote_open_guess": {"line": 2, "note": "best-effort: a quote opened here."}}


def test_render_row_verbose_shows_the_guess():
    data = {"tool": "bash-check", "ok": False, "count": 1, "duration_ms": 1,
            "errors": [dict(ERR)]}
    lines = supertool._validator_render_row(data, verbose=True)
    body = "\n".join(lines)
    assert "L2" in body and "opened" in body, body


def test_render_diff_shows_the_guess_on_a_fresh_failure():
    """No baseline (`before=None`) -- the shape an edit's own post-write
    validator pass produces, and the one #1810's own incident went through."""
    after = {"tool": "bash-check", "ok": False, "count": 1, "duration_ms": 1,
             "errors": [dict(ERR)]}
    lines = supertool._validator_render_diff(None, after)
    body = "\n".join(lines)
    assert "L2" in body and "opened" in body, body


def test_render_diff_shows_the_guess_on_a_regression():
    """A baselined run that regressed -- the other branch of the same function."""
    before = {"tool": "bash-check", "ok": True, "count": 0, "duration_ms": 1, "errors": []}
    after = {"tool": "bash-check", "ok": False, "count": 1, "duration_ms": 1,
              "errors": [dict(ERR)]}
    lines = supertool._validator_render_diff(before, after)
    body = "\n".join(lines)
    assert "L2" in body and "opened" in body, body


def test_no_guess_key_adds_nothing():
    """No `quote_open_guess` on the error -- no extra line, no crash."""
    err = {"line": 8, "code": "syntax", "msg": "m"}
    lines = supertool._validator_render_diff(
        None, {"tool": "t", "ok": False, "count": 1, "duration_ms": 1, "errors": [err]})
    assert not any("opened" in l for l in lines), lines
