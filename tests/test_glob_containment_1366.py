"""`glob` was outside the containment gate entirely (#1366).

`read:/tmp/x/f.txt` refuses; `glob:/tmp/x/*.txt` answered. Same target, same
call, two verdicts. `glob` returns filenames rather than bytes, so what it
leaks is an **existence oracle** across the boundary — directory layout,
project names, whether a path exists — the same family as #1135 and #1142.

Why the guard lives in `op_glob` and not in `_PATH_ARG_POSITIONS`: a glob
pattern is not a path. The table gates a fixed slot as a literal filename, and
`glob`'s slot 1 holds magic characters, brace groups and `**`. That is the
shape #1166 established for `grep`/`around` and #1163/#1164 for `between` —
the op gates the value *it* computed, at the point it computed it.

Which part of the pattern is checked is the judgment, and each obvious half
fails on its own:

* the **literal prefix** before the first magic character passes
  `*/../../etc/*`, whose prefix is empty;
* filtering the **results** turns a refusal into a silently narrowed list,
  which is this repo's house defect wearing a clean receipt.

So both, and neither narrows: the pattern's own *reach* is checked before disk
is touched (every magic component neutralised to an ordinary name, because no
glob metacharacter can invent a `/` — only the literal separators and `..`
already in the pattern move the cursor), and the expanded results are checked
after, where a wildcard landing on an outward symlink is the only escape the
pattern could not predict. A result that escapes **refuses the whole call**.

`(0 files)` is never a refusal. That is the absence the tool manufactured,
indistinguishable from an empty directory, and it is exactly what `glob` said
about `~` until #1300.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import supertool

from _symlink import require_symlink

NL = chr(10)
MARK = "GLOB-1366"


@pytest.fixture
def boxed(tmp_path: Path, monkeypatch) -> Path:
    """cwd is `box/`; the target sits outside it, one level up.

    conftest sets SUPERTOOL_ALLOW_OUTSIDE_CWD=1 for the whole suite so
    tmp_path fixtures work at all — a containment test that does not put it
    back asserts nothing.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / (MARK + "-x.txt")).write_text("x" + NL, encoding="utf-8")
    (outside / (MARK + "-y.txt")).write_text("y" + NL, encoding="utf-8")
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.chdir(box)
    return outside


def _assert_refused(out: str) -> None:
    assert MARK not in out, (
        "a filename from outside the boundary reached the answer:" + NL + out)
    assert "escapes cwd" in out, (
        "the refusal has to name containment, the same word `read` uses for "
        "the same target:" + NL + out)
    assert "(0 files)" not in out, (
        "a refusal rendered as an empty result set is the absence the tool "
        "manufactured — indistinguishable from an empty directory:" + NL + out)


def test_an_absolute_pattern_outside_cwd_is_refused(boxed: Path) -> None:
    """The filed reproduction, with `read`'s verdict as the reference."""
    out = supertool.dispatch("glob:" + supertool._fwd(str(boxed / "*.txt")))
    _assert_refused(out)


def test_read_and_glob_agree_on_the_same_target(boxed: Path) -> None:
    """One call refusing while its sibling answers is the whole of #1366."""
    target = supertool._fwd(str(boxed / (MARK + "-x.txt")))
    read_out = supertool.dispatch("read:" + target)
    glob_out = supertool.dispatch("glob:" + supertool._fwd(str(boxed / "*.txt")))
    assert "escapes cwd" in read_out, read_out
    assert "escapes cwd" in glob_out, (
        "`read` refuses this target and `glob` must not answer about it:"
        + NL + glob_out)


def test_a_relative_escape_is_refused(boxed: Path) -> None:
    out = supertool.dispatch("glob:../outside/*.txt")
    _assert_refused(out)


def test_a_magic_component_cannot_carry_the_traversal(boxed: Path) -> None:
    """The literal-prefix answer, refuted.

    `*/../../outside/*.txt` has an EMPTY literal prefix, so a gate that
    checked only the text before the first magic character would clear it.
    The traversal is in the `..` components, which no wildcard can hide.
    """
    (Path.cwd() / "sub").mkdir()
    out = supertool.dispatch("glob:*/../../outside/*.txt")
    _assert_refused(out)


def test_a_doublestar_component_cannot_carry_the_traversal(boxed: Path) -> None:
    (Path.cwd() / "sub").mkdir()
    out = supertool.dispatch("glob:**/../../outside/*.txt")
    _assert_refused(out)


def test_a_brace_alternative_is_gated_per_branch(boxed: Path) -> None:
    """`_expand_braces` fans out INSIDE `_glob_files`, after any gate on the
    pattern string. A guard that does not expand braces itself reads
    `{.,/outside}` as one contained component and clears both branches.

    Relative rather than absolute on purpose: `_split_arg` reassembles a
    Windows drive letter only when the piece before the `:` is a lone letter,
    and `{.,C` is not one — an absolute spelling would split into a different
    call on Windows and then pass for the wrong reason."""
    out = supertool.dispatch("glob:{.,../outside}/*.txt")
    _assert_refused(out)


def test_a_wildcard_landing_on_an_outward_symlink_refuses_the_whole_call(
        boxed: Path) -> None:
    """The one escape the pattern cannot predict — and it must REFUSE, not
    quietly drop the entry. A narrowed list is a lie about the population."""
    require_symlink()
    (Path.cwd() / "link").symlink_to(boxed, target_is_directory=True)
    (Path.cwd() / "here.txt").write_text("local" + NL, encoding="utf-8")
    out = supertool.dispatch("glob:*/*.txt")
    _assert_refused(out)


def test_a_contained_pattern_still_answers(tmp_path: Path, monkeypatch) -> None:
    """The boundary. Refusing everything would pass every test above."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    box = tmp_path / "box"
    (box / "sub").mkdir(parents=True)
    (box / "sub" / "a.txt").write_text("a" + NL, encoding="utf-8")
    (box / "sub" / "b.txt").write_text("b" + NL, encoding="utf-8")
    monkeypatch.chdir(box)
    out = supertool.dispatch("glob:sub/*.txt")
    assert "escapes cwd" not in out, out
    assert "a.txt" in out and "b.txt" in out, out


def test_a_contained_recursive_pattern_still_answers(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    box = tmp_path / "box"
    (box / "a" / "b").mkdir(parents=True)
    (box / "a" / "b" / "deep.txt").write_text("d" + NL, encoding="utf-8")
    monkeypatch.chdir(box)
    out = supertool.dispatch("glob:**/*.txt")
    assert "escapes cwd" not in out, out
    assert "deep.txt" in out, out


def test_the_opt_out_still_opens_the_boundary(boxed: Path, monkeypatch) -> None:
    """Containment is opt-out-able everywhere else; `glob` must not become the
    one op that ignores the switch."""
    monkeypatch.setenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", "1")
    out = supertool.dispatch("glob:" + supertool._fwd(str(boxed / "*.txt")))
    assert "escapes cwd" not in out, out
    assert MARK + "-x.txt" in out, out


def test_the_mid_path_retry_cannot_be_used_to_escape(boxed: Path) -> None:
    """#363's `**/` retry re-globs a pattern that matched nothing. It only ever
    prefixes, so it cannot ascend — pinned so a future edit cannot make it."""
    out = supertool.dispatch("glob:../outside/*.txt")
    _assert_refused(out)
    assert "mid-path retry" not in out, out
