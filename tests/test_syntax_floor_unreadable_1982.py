"""#1982 -- a path the floor checker could not open must not render as a
syntax error in the code.

`_syntax_floor_check`'s child snippet (`_SYNTAX_FLOOR_COMPILE`) already
distinguishes the two failure arms correctly: a `SyntaxError` from a real
parse failure, and an `(OSError, ValueError)` from a path that could not be
opened at all -- deleted mid-walk, a dangling symlink, a permissions error.
The parent used to erase that distinction: both arms fed the same
hard-coded `"code": "syntax"` literal, so a file the check never compiled
came back looking exactly like one that failed to parse.

Paired tests, so the fix cannot pass by making the checker silent about
everything: a path that does not exist must NOT produce a syntax finding,
and a path that genuinely fails to compile still MUST.
"""
import supertool


def _floor_or_skip():
    interp = supertool._syntax_floor_interpreter()
    if interp is None:
        floor = "{0}.{1}".format(*supertool.SYNTAX_FLOOR)
        import pytest
        pytest.skip(
            f"NO FLOOR INTERPRETER: nothing at Python {floor} to compile with. "
            f"Set ${supertool.SYNTAX_FLOOR_ENV} or install python{floor}. "
            "This check did NOT run -- the floor CI leg is the guarantee it runs at all."
        )
    return interp


def test_a_path_that_does_not_exist_is_not_reported_as_a_syntax_error(tmp_path):
    """The child's `(OSError, ValueError)` arm fires on this path -- it never
    reaches `compile()` at all. The parent must not fold that into `errors`
    under `code: syntax`: that reads as a defect in source that was never
    read."""
    _floor_or_skip()
    missing = str(tmp_path / "vanished-mid-walk.py")
    result = supertool._syntax_floor_check([missing])
    assert result.get("errors") == [], result
    assert result.get("count") == 0, result
    assert result.get("ok") is True, result
    assert result.get("checked") == 0, (
        f"a path that was never compiled must not count as checked: {result!r}"
    )
    skipped = result.get("skipped_paths") or []
    assert any(e.get("file") == missing for e in skipped), (
        f"the unreadable path must surface somewhere other than errors: {result!r}"
    )


def test_a_genuinely_illegal_file_still_reports_a_syntax_error(tmp_path):
    """Positive control, paired with the case above so it cannot pass
    vacuously by the checker reporting nothing for anything."""
    _floor_or_skip()
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n    pass\n", encoding="utf-8")
    result = supertool._syntax_floor_check([str(bad)])
    assert result.get("ok") is False, result
    assert result.get("count") == 1, result
    assert result["errors"][0]["code"] == "syntax"
    assert result["errors"][0]["file"] == str(bad)
    assert result.get("checked") == 1, result
    assert not result.get("skipped_paths"), (
        f"a real syntax error must not also show up as skipped: {result!r}"
    )
