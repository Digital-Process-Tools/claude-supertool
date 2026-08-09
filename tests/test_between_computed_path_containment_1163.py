"""`between` opens a path the containment gate never saw (#1163, #1164).

`_PATH_ARG_POSITIONS["between"] = (2, 4)` gates fixed slots, but both readings
of `between` compute their path from a variable slot: symbol mode takes
`parts[-1]` (so a symbol carrying a ':' pushes the path past slot 2), and `re:`
mode joins `parts[4:]` (so a path carrying a ':' is only partly gated). Same
file, different slot, different answer.

The mirror (#1164) is the same table read from the other side: slot 2 is the
START *regex* in `between:re:START:END:PATH`, so a regex that looks like a path
refuses a legitimate local slice, naming a file the caller never asked for.

Every refusal test here asserts the bytes stayed unread AND that the message
names containment -- "file not found" is what a build without the hole would
say for an unrelated reason, and asserting "it errored" would pass on it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import supertool

NL = chr(10)
MARKER = "TOPSECRET-1163"

# The suite disables tree-sitter globally (conftest), so symbol mode short-
# circuits on "requires tree-sitter" before it ever reaches a file. The tests
# that assert on what symbol mode *reads* take the `enable_tree_sitter` fixture
# and skip when no grammar package is installed; the `re:` cases below need no
# parser and run everywhere.
try:
    import tree_sitter_language_pack  # noqa: F401
    _HAS_TS = True
except ImportError:  # pragma: no cover - depends on the environment
    try:
        import tree_sitter_languages  # noqa: F401
        _HAS_TS = True
    except ImportError:
        _HAS_TS = False

needs_ts = pytest.mark.skipif(
    not _HAS_TS, reason="no tree-sitter grammar package installed")


@pytest.fixture
def outside(tmp_path: Path, monkeypatch) -> Path:
    """cwd is a box under tmp_path; the secrets sit one level above it.

    conftest sets SUPERTOOL_ALLOW_OUTSIDE_CWD=1 globally so tmp_path fixtures
    work at all, so a containment test has to put it back or it asserts
    nothing.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    py = tmp_path / "outside.py"
    py.write_text("def secret_fn():" + NL + "    return " + chr(34) + MARKER
                  + chr(34) + NL, encoding="utf-8")
    (tmp_path / "outside.xml").write_text("<a>" + MARKER + "</a>" + NL,
                                          encoding="utf-8")
    (tmp_path / "outside.txt").write_text(
        "alpha" + NL + MARKER + NL + "omega" + NL, encoding="utf-8")
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.chdir(box)
    return py


@needs_ts
def test_symbol_mode_refuses_when_the_symbol_carries_a_colon(
    outside, enable_tree_sitter
) -> None:
    """The reported shape: four parts, so the path lands past every gated slot."""
    out = supertool.dispatch("between:Foo:barbaz:../outside.py")
    assert MARKER not in out, out
    assert "escapes cwd" in out, (
        "the file was opened and parsed through parts[3]; the same file at "
        "parts[2] is refused:" + NL + out)
    assert "not found in" not in out, (
        "'symbol X not found in PATH' is the existence oracle -- it answers "
        "only if the file was opened:" + NL + out)


@needs_ts
def test_symbol_mode_refuses_an_absolute_escape(
    outside: Path, enable_tree_sitter
) -> None:
    out = supertool.dispatch("between:Foo:barbaz:" + str(outside))
    assert MARKER not in out, out
    assert "escapes cwd" in out, out


@needs_ts
def test_the_extension_oracle_is_closed(
    outside: Path, enable_tree_sitter
) -> None:
    """tree-sitter's per-extension refusal is itself a read of the outside file.

    `.xml` answers differently from `.py`, which tells the caller the file
    exists and what it is called -- from a slot nothing gates.
    """
    out = supertool.dispatch("between:Foo:barbaz:../outside.xml")
    assert "tree-sitter does not support" not in out, (
        "the extension was inspected, so containment ran too late:" + NL + out)
    assert "escapes cwd" in out, out


@needs_ts
def test_more_colons_do_not_walk_further_past_the_gate(
    outside: Path, enable_tree_sitter
) -> None:
    """Six parts: slots 2 and 4 hold harmless tokens, the path is at parts[5]."""
    out = supertool.dispatch("between:a:b:c:d:../outside.py")
    assert MARKER not in out, out
    assert "escapes cwd" in out, out


def test_re_mode_refuses_an_escaping_path(outside: Path) -> None:
    out = supertool.dispatch("between:re:alpha:omega:../outside.txt")
    assert MARKER not in out, out
    assert "escapes cwd" in out, out


@pytest.mark.skipif(os.name == "nt",
                    reason="':' is not a legal filename character on Windows")
def test_re_mode_returns_a_body_from_outside_when_the_path_carries_a_colon(
    outside: Path, tmp_path: Path
) -> None:
    """The disclosure the issue could not demonstrate in symbol mode.

    `re:` joins parts[4:] on ':', so only the fragment before the first colon
    is gated. A directory whose name contains ':' makes the rest of the join
    resolve outside the boundary -- and `re:` prints the slice, not an error.
    """
    (tmp_path / "box" / "ok:..").mkdir()
    out = supertool.dispatch("between:re:alpha:omega:ok:../../../outside.txt")
    assert MARKER not in out, (
        "a file outside cwd was sliced and printed:" + NL + out)
    assert "escapes cwd" in out, out


@needs_ts
def test_a_colon_symbol_on_a_contained_file_still_reports_not_found(
    tmp_path: Path, monkeypatch, enable_tree_sitter
) -> None:
    """The boundary: containment must not swallow a local call.

    A symbol that cannot exist still has to fail as a *symbol* lookup on a file
    inside the boundary, not as a containment refusal.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    box = tmp_path / "box"
    box.mkdir()
    (box / "in.py").write_text("def local_fn():" + NL + "    return 1" + NL,
                               encoding="utf-8")
    monkeypatch.chdir(box)
    out = supertool.dispatch("between:Foo:barbaz:in.py")
    assert "escapes cwd" not in out, out
    assert "not found in" in out, out


@needs_ts
def test_a_contained_symbol_lookup_still_answers(
    tmp_path: Path, monkeypatch, enable_tree_sitter
) -> None:
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    box = tmp_path / "box"
    box.mkdir()
    (box / "in.py").write_text("def local_fn():" + NL + "    return 1" + NL,
                               encoding="utf-8")
    monkeypatch.chdir(box)
    out = supertool.dispatch("between:local_fn:in.py")
    assert "local_fn" in out, out
    assert "escapes cwd" not in out, out


def test_a_start_regex_that_looks_like_a_path_slices_a_local_file(
    tmp_path: Path, monkeypatch
) -> None:
    """#1164: slot 2 is a regex in the `re:` reading, and the table calls it a path.

    The call names exactly one path, `local.py`, and it is inside the boundary.
    Refusing it names /etc/passwd, which the caller never asked to open.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    box = tmp_path / "box"
    box.mkdir()
    (box / "local.py").write_text(
        "head" + NL + "PATH = " + chr(34) + "/etc/passwd" + chr(34) + NL
        + "body" + NL + "b = 1" + NL, encoding="utf-8")
    monkeypatch.chdir(box)
    out = supertool.dispatch("between:re:/etc/passwd:b:local.py")
    assert "escapes cwd" not in out, (
        "a regex is not a path -- the only path in this call is local.py, "
        "which is contained:" + NL + out)
    assert "body" in out, out
