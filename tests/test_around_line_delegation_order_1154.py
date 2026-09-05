r"""#1154 -- `around:PATH:LINE:N` disclosing its own reinterpretation.

Filed against `_around_line_delegation` (#1086): "the tool reinterpreted the
arguments as `around_line` and reported the substitution *after* answering."
Reproducing that against master (commit 1b74e10) does not show the ordering
defect -- `_around_line_delegation`'s return has always been `note + chr(10)
+ op_around_line(...)`, disclosure first, since #1086 introduced it. What is
still live from #1154 is the question the issue itself raises: should this
be a refusal instead of a substitution, on the grounds the caller's intent
is genuinely ambiguous?

It is not, by construction, and this file pins why: `_around_line_delegation`
only converts a call into an answer when the literal reading is IMPOSSIBLE --
the numeric token must not name a file that actually exists (`os.path.exists`
gates it). A path that happens to be a real file called e.g. "5" is answered
literally, with no delegation and no disclosure, because that reading was
never ambiguous -- it was always the correct one. So the "substitution vs
refusal" choice collapses: delegation only ever fires on a call that already
has exactly one reading (`around_line`'s), which is a disclosed substitution,
not a guess among genuine alternatives. This file keeps that decision
honest: the ordering is asserted directly, and the one case where the
literal path IS real is asserted to answer literally, un-disclosed and
un-substituted -- the paired "must not fire" this dimension needs (a "must
not substitute" that passes when nothing ran at all would be worthless).
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _numbered(tmp_path: Path, name: str, count: int) -> Path:
    f = tmp_path / name
    f.write_bytes(("\n".join(f"line{i}" for i in range(1, count + 1)) + "\n").encode())
    return f


def test_the_disclosure_precedes_the_answer_it_discloses(tmp_path: Path) -> None:
    """Scanning output top-to-bottom must hit the disclosure before the
    reinterpreted answer, or a reader skimming for the verdict reads the
    answer to a question they did not ask before learning it was
    substituted."""
    f = _numbered(tmp_path, "many.txt", 60)
    out = supertool.dispatch(f"around:{f}:30:4")
    note_at = out.find("read as around_line")
    answer_at = out.find("line30")
    assert note_at != -1, f"no disclosure at all: {out!r}"
    assert answer_at != -1, f"no answer at all: {out!r}"
    assert note_at < answer_at, (
        "the disclosure must come before the reinterpreted answer, not "
        f"after it: note at {note_at}, answer at {answer_at}\n{out!r}")


def test_a_numeric_path_that_is_a_real_file_is_answered_literally_not_substituted(
    tmp_path: Path, monkeypatch,
) -> None:
    """The paired 'must fire' for the assertion above: when the numeric token
    names a file that genuinely exists, that reading is not merely
    plausible, it is correct -- so no delegation, no disclosure, and the
    literal answer (a grep of `pattern` inside the numerically-named file)
    comes back untouched.

    The numeric path has to be spelled BARE for this to actually exercise
    the `os.path.exists(path)` half of the guard: an absolute
    `tmp_path`-rooted path (e.g. `/tmp/.../ambig/5`) fails `_is_ascii_int`
    on the slashes alone, so a test built that way passes even with
    `os.path.exists` deleted from the guard entirely -- confirmed by
    mutating the guard to `if not _is_ascii_int(path): return ""` and
    re-running: the absolute-path version still passed, exercising nothing
    about existence (review finding on #1154's first draft). `cd` into the
    fixture directory and use the relative name `5` so the token dispatch
    actually sees is the bare digits `_is_ascii_int` accepts, which is the
    only way to reach the `os.path.exists` branch at all."""
    d = tmp_path / "ambig"
    d.mkdir()
    (d / "5").write_text("needle-line\nother\n")
    (d / "pat.txt").write_text("irrelevant\n")
    monkeypatch.chdir(d)
    out = supertool.dispatch("around:pat.txt:5:2")
    assert "read as around_line" not in out, (
        "a real file must never be reinterpreted as a line number just "
        f"because its name is digits: {out!r}")
    assert "no match" in out, (
        "the literal reading -- searching the numerically-named file for "
        f"the pattern file's path as a string -- must be what actually ran: "
        f"{out!r}")
