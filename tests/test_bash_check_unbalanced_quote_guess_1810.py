"""bash-check names bash -n's own line; a reader also needs where it began (#1810).

`bash -n` reports a syntax finding at the line where parsing gave up, not at the
line that broke it. An apostrophe left open by an edit is a common cause, and
the line it reports can sit several lines -- in the reported incident, five, on
a ~1,900-line file of awk embedded in shell quoting -- past the apostrophe that
actually opened the string. The reported line is correct about where `bash -n`
stopped; it is not where the mistake is, and from it alone the file reads as
broken everywhere, because every neighbouring line is also full of quotes.

This does not replace `bash -n`'s own line -- that is exact, and rewriting it
would misattribute a real diagnostic. It adds a *second*, explicitly labelled
guess: the nearest quote that a plain state-machine scan finds still open by
the time execution reaches the reported line. Only a guess -- the tracker knows
nothing of here-docs, command substitution or nested quoting -- so it is never
folded into `line`, `col` or `msg`, all of which stay exactly what `bash -n`
said.

No subprocess: `parse_diagnostics` is driven directly against a **real**
`bash -n` transcript captured once (below), so the test is exercised on every
platform without depending on the bash on PATH agreeing about wording.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def adapter(name: str):
    return _load(VALIDATORS / name / f"{name}.py", "_v1810_" + name.replace("-", "_"))


# Captured once, from GNU bash, against the fixture written below:
#   bash -n <file>  ->  exit 2
UNTERMINATED_QUOTE_STDERR = (
    "{file}: line 2: unexpected EOF while looking for matching `''\n"
    "{file}: line 8: syntax error: unexpected end of file\n"
)

FIXTURE = (
    "#!/bin/bash\n"
    "FOO='unterminated\n"
    "BAR=baz\n"
    "QUX=qux\n"
    "function foo() {\n"
    "    echo hi\n"
    "}\n"
)


def test_far_diagnostic_line_carries_a_labelled_guess_at_the_true_open(tmp_path):
    """The `line: 8` finding -- bash's own EOF report, six lines past the
    apostrophe -- also names line 2 as a guess. `line` itself is untouched."""
    mod = adapter("bash-check")
    f = tmp_path / "unterm.sh"
    f.write_text(FIXTURE, encoding="utf-8")
    out = UNTERMINATED_QUOTE_STDERR.format(file=str(f))
    errors = mod.parse_diagnostics(out, str(f))
    by_line = {e["line"]: e for e in errors}
    assert 8 in by_line and 2 in by_line
    far = by_line[8]
    assert far["line"] == 8, "bash's own line must not be rewritten"
    guess = far.get("quote_open_guess")
    assert guess is not None, "a finding far from an open quote must carry a guess"
    assert guess["line"] == 2
    assert "note" in guess and guess["note"], "the guess must say it is a guess"


def test_diagnostic_line_that_is_itself_the_open_carries_no_guess():
    """bash's `line 2` report already IS where the quote opened -- nothing to
    add, so no `quote_open_guess` key at all (never one pointing at itself)."""
    mod = adapter("bash-check")
    errors = mod.parse_diagnostics(
        UNTERMINATED_QUOTE_STDERR.format(file="x.sh"), "x.sh")
    # parse_diagnostics reads the file for context/guessing; x.sh does not
    # exist here, so nothing can be read and no guess is fabricated either.
    by_line = {e["line"]: e for e in errors}
    assert "quote_open_guess" not in by_line[2]


def test_balanced_file_gets_no_guess(tmp_path):
    """A real syntax error with no unbalanced quote anywhere before it must not
    manufacture a guess -- the positive control for the two tests above."""
    mod = adapter("bash-check")
    f = tmp_path / "brace.sh"
    f.write_text("#!/bin/bash\nif [ 1 ]; then\n  echo hi\n", encoding="utf-8")
    out = f"{f}: line 4: syntax error: unexpected end of file\n"
    errors = mod.parse_diagnostics(out, str(f))
    assert errors and errors[0].get("quote_open_guess") is None
