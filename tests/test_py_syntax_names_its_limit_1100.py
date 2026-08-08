"""`py-syntax` says what it did NOT check, on every green row (#1100).

The incident: a payload write landed bytes that parsed and could not be
imported. `py-syntax` passed, the module was the tool's own core, and every
subsequent supertool call in that worktree died -- behind a green validator.

`py-syntax` answers "does this parse". It is read as "is this a working
module", because that is the question a caller who just wrote a file actually
has. Between the two sits everything that only fails at import: a regex
compiled at module level, an undefined name at class-body scope, a circular
import.

The chosen fix of the three the issue offered is the third -- the validator
names its own limit. The green is qualified in the column that already exists,
so it costs no line and cannot drift out of date.

Why not the other two:

* **Importing the file.** Import executes module-level code. Deciding to run
  arbitrary just-edited bytes is a containment decision, and the answer here is
  no: the file that triggered this report was the tool's own core, being edited
  by the tool, in a worktree the author was mid-change in. `py_compile` is not
  an import and would have passed this file too, so the cheap half of that
  option buys nothing.
* **Making the doubled-backslash warning a refusal.** That is #1087 and it
  lands in this same change -- so picking it here would leave #1100 with no
  outcome of its own and leave every other route to an unimportable file
  (undefined name, circular import, a bad regex from any cause) reading as a
  clean bill. The two compose: #1087 closes the vector that was reported, this
  closes the over-reading of the green.
"""
from pathlib import Path

import supertool

NL = chr(10)


def _spec_for(name: str) -> dict:
    return supertool._BUILTIN_SYNTAX_VALIDATORS[name]


def test_the_result_carries_the_limit_it_did_not_check(tmp_path: Path) -> None:
    """Asserted on the result dict, not the rendered row -- the row is one
    consumer and a receipt is not the only thing that reads a verdict."""
    f = tmp_path / "ok.py"
    f.write_text("x = 1" + NL, encoding="utf-8")
    res = supertool._builtin_syntax_run("py-syntax", "python", str(f))
    assert res["ok"] is True
    assert "scope" in res, res
    assert "import" in res["scope"].lower(), res


def test_a_file_that_parses_but_cannot_import_still_passes_and_says_so(
    tmp_path: Path,
) -> None:
    """The load-bearing case, and the reason the qualification is not cosmetic.

    This module parses. Importing it raises -- the regex is compiled at module
    level and does not compile. `py-syntax` returns ok, correctly, because it
    was only ever asked whether the file parses; what changes is that the row
    no longer reads as a working module."""
    f = tmp_path / "unimportable.py"
    f.write_text("import re" + NL + 'P = re.compile("(")' + NL, encoding="utf-8")
    res = supertool._builtin_syntax_run("py-syntax", "python", str(f))
    assert res["ok"] is True, "premise check: this file must parse"
    raised = False
    try:
        exec(compile(f.read_text(encoding="utf-8"), str(f), "exec"), {})
    except Exception:
        raised = True
    assert raised, "premise check: this file must fail at import time"
    assert "not import" in res["scope"].lower(), res


def test_the_green_row_prints_the_limit_instead_of_no_new_errors(
    tmp_path: Path,
) -> None:
    """Where the reader actually meets it. `ok (no new errors)` is the string
    that got over-read; the scope replaces it rather than sitting beside it, so
    there is no version of the row that carries the old reading."""
    f = tmp_path / "ok.py"
    f.write_text("x = 1" + NL, encoding="utf-8")
    res = supertool._builtin_syntax_run("py-syntax", "python", str(f))
    rows = supertool._validator_render_diff(res, res)
    joined = NL.join(rows)
    assert "not imported" in joined, joined
    assert "(no new errors)" not in joined, joined


def test_a_failing_row_is_unchanged(tmp_path: Path) -> None:
    """The scope qualifies a pass. On a red row the finding is the message and
    a hedge beside it would dilute the one line the reader must act on."""
    f = tmp_path / "bad.py"
    f.write_text("def f(:" + NL, encoding="utf-8")
    res = supertool._builtin_syntax_run("py-syntax", "python", str(f))
    assert res["ok"] is False
    rows = NL.join(supertool._validator_render_diff(None, res))
    assert "not imported" not in rows, rows


def test_a_validator_with_no_scope_renders_exactly_as_before(tmp_path: Path) -> None:
    """The blast radius. Every other validator's green row is untouched -- the
    qualification is a property of this checker, not a new hedge on all of
    them."""
    res = {"tool": "fake", "file": "x.py", "ok": True, "count": 0,
           "errors": [], "elapsed_s": 0.1}
    rows = NL.join(supertool._validator_render_diff(res, res))
    assert "(no new errors)" in rows, rows


def test_the_rollback_guarantee_is_not_weakened(tmp_path: Path) -> None:
    """A validator that hedges its pass must still hard-fail its fail. The
    rollback contract reads from the same result, and softening the marker
    would have quietly turned the syntax floor off."""
    assert _spec_for("py-syntax")["rollback_on_fail"] is True
    f = tmp_path / "bad.py"
    f.write_text("def f(:" + NL, encoding="utf-8")
    res = supertool._builtin_syntax_run("py-syntax", "python", str(f))
    assert supertool._validator_regressed(None, res) is True
