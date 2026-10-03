"""#2731 -- strip comments and docstrings from every shipped `.py` file in the
release tree, using `tokenize` for comments and `ast` only to locate docstring
positions (never `ast.unparse`, which reformats code rather than cutting it).

Every removed line must become a blank line (so a traceback into the shipped
file still names the right source line), every stripped file must still
`compile()`, and every one of these behaviours is paired with a "must be kept"
twin: a stripper that deleted everything would pass every removal check here,
and the twins are what would catch it.
"""

from __future__ import annotations

import importlib.util
import sys
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#2705)"
    spec = importlib.util.spec_from_file_location("build_release_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_release_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _strip(mod, src: str, path: str = "x.py") -> str:
    out = mod.strip_py(src.encode("utf-8"), path)
    return out.decode("utf-8")


def _no_comment_tokens(text: str) -> None:
    """A real behaviour assertion, not a substring check: re-tokenize the
    STRIPPED text and assert no COMMENT token survived anywhere in it (a
    bare `"#" not in text` would also pass if the stripper had deleted the
    whole file)."""
    import io
    toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    comments = [t for t in toks if t.type == tokenize.COMMENT
                and not t.string.startswith("#!")
                and "coding" not in t.string]
    assert not comments, comments


# -- module docstring ---------------------------------------------------------

def test_module_docstring_only_file_becomes_legal_empty_module(mod):
    src = '"""Just a module docstring, nothing else."""\n'
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    assert "Just a module docstring" not in out
    assert out.count("\n") == src.count("\n")


def test_module_docstring_with_code_after_is_blanked_not_deleted(mod):
    src = '"""Module doc."""\n\nVALUE = 1\n'
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    assert "Module doc" not in out
    assert "VALUE = 1" in out
    assert out.count("\n") == src.count("\n")


# -- function / class docstrings ----------------------------------------------

def test_function_docstring_removed_line_count_preserved(mod):
    src = (
        "def f():\n"
        '    """A docstring\n'
        "    spanning two lines.\n"
        '    """\n'
        "    return 1\n"
    )
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    assert "docstring" not in out
    assert "return 1" in out
    assert out.count("\n") == src.count("\n")


def test_docstring_only_function_gets_pass(mod):
    src = (
        "def f():\n"
        '    """Only a docstring, nothing else in the body."""\n'
    )
    out = _strip(mod, src)
    ns: dict = {}
    exec(compile(out, "x.py", "exec"), ns)
    assert ns["f"]() is None
    assert "pass" in out


def test_docstring_only_class_gets_pass(mod):
    src = (
        "class C:\n"
        '    """Only a docstring."""\n'
    )
    out = _strip(mod, src)
    ns: dict = {}
    exec(compile(out, "x.py", "exec"), ns)
    assert ns["C"]() is not None
    assert "pass" in out


def test_nested_function_docstring_only_gets_its_own_pass(mod):
    src = (
        "def outer():\n"
        "    def inner():\n"
        '        """Nested, docstring-only."""\n'
        "    return inner\n"
    )
    out = _strip(mod, src)
    ns: dict = {}
    exec(compile(out, "x.py", "exec"), ns)
    assert ns["outer"]()() is None
    assert "Nested" not in out


def test_nested_class_docstring_with_body_after_is_blanked(mod):
    src = (
        "class Outer:\n"
        "    class Inner:\n"
        '        """Nested class doc."""\n'
        "        x = 1\n"
    )
    out = _strip(mod, src)
    ns: dict = {}
    exec(compile(out, "x.py", "exec"), ns)
    assert ns["Outer"].Inner.x == 1
    assert "Nested class doc" not in out


# -- comments ------------------------------------------------------------------

def test_standalone_comment_line_becomes_blank(mod):
    src = "x = 1\n# a standalone comment\ny = 2\n"
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    _no_comment_tokens(out)
    assert "x = 1" in out and "y = 2" in out
    assert out.count("\n") == src.count("\n")


def test_trailing_noqa_comment_removed_code_kept(mod):
    src = "import os  # noqa: F401\n"
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    _no_comment_tokens(out)
    assert "import os" in out
    assert "noqa" not in out


def test_type_comment_removed_code_kept(mod):
    src = "x = []  # type: list[int]\n"
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    _no_comment_tokens(out)
    assert "x = []" in out
    assert "type:" not in out


def test_hash_inside_a_plain_string_is_not_a_comment(mod):
    src = 'MARKER = "value with a # inside it, not a comment"\n'
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    ns: dict = {}
    exec(out, ns)
    assert ns["MARKER"] == "value with a # inside it, not a comment"


def test_hash_inside_fstring_literal_text_is_not_a_comment(mod):
    src = 'x = 1\nMARKER = f"value is {x} # not a comment"\n'
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    ns: dict = {}
    exec(out, ns)
    assert ns["MARKER"] == "value is 1 # not a comment"


def test_fstring_with_trailing_real_comment_both_handled(mod):
    src = 'x = 1\nMARKER = f"{x}"  # a real trailing comment\n'
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    _no_comment_tokens(out)
    ns: dict = {}
    exec(out, ns)
    assert ns["MARKER"] == "1"
    assert "real trailing" not in out


def test_line_continuation_with_trailing_comment(mod):
    src = (
        "total = 1 + \\\n"
        "    2  # add two\n"
    )
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    _no_comment_tokens(out)
    ns: dict = {}
    exec(out, ns)
    assert ns["total"] == 3
    assert "add two" not in out


def test_multiline_string_assigned_to_a_name_is_not_a_docstring(mod):
    src = (
        'DATA = """line one\n'
        "has a # inside it\n"
        'line three"""\n'
    )
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    ns: dict = {}
    exec(out, ns)
    assert ns["DATA"] == "line one\nhas a # inside it\nline three"


def test_multiline_string_mid_statement_not_first_is_not_a_docstring(mod):
    src = (
        "def f():\n"
        "    x = 1\n"
        '    y = """kept\n'
        'verbatim"""\n'
        "    return x, y\n"
    )
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    ns: dict = {}
    exec(out, ns)
    assert ns["f"]() == (1, "kept\nverbatim")


# -- line numbers --------------------------------------------------------------

def test_traceback_line_number_survives_stripping(mod):
    src = (
        "def f():\n"
        '    """A docstring that will be removed."""\n'
        "    # a comment that will be removed too\n"
        '    raise ValueError("boom")\n'
    )
    out = _strip(mod, src)
    ns: dict = {}
    exec(compile(out, "x.py", "exec"), ns)
    with pytest.raises(ValueError) as exc_info:
        ns["f"]()
    tb = exc_info.tb
    while tb.tb_next:
        tb = tb.tb_next
    assert tb.tb_lineno == src.splitlines().index('    raise ValueError("boom")') + 1


# -- shebang / encoding cookie --------------------------------------------------

def test_shebang_line_kept(mod):
    src = "#!/usr/bin/env python3\nx = 1\n"
    out = _strip(mod, src)
    assert out.startswith("#!/usr/bin/env python3\n")


def test_encoding_cookie_kept(mod):
    src = "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\nx = 1\n"
    out = _strip(mod, src)
    assert "coding: utf-8" in out


# -- non-ASCII byte/char offset safety (ast col_offset is UTF-8 BYTES) --------

def test_docstring_after_nonascii_code_on_same_line_is_not_mis_sliced(mod):
    src = 'def f(): """doc with \u2014 em dash inside"""\n'
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    assert "em dash" not in out
    ns: dict = {}
    exec(out, ns)
    assert ns["f"]() is None


def test_u2028_line_separator_does_not_shift_line_numbers(mod):
    # `str.splitlines()` treats U+2028 as a line break; `ast`/`tokenize` do
    # not. Using the former to build the stripper's own line list -- the
    # actual bug hit while implementing this against this repo's own
    # presets/_declared_workflows.py:408 -- silently misaligns every span
    # after the first U+2028 and corrupts an unrelated statement.
    src = (
        'MARKER = "has a \u2028 line separator inside a string"\n'
        "def f():\n"
        '    """docstring on the next real line."""\n'
        "    return 1\n"
    )
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    ns: dict = {}
    exec(out, ns)
    assert ns["f"]() == 1
    assert "docstring on the next" not in out


def test_inline_def_with_trailing_statement_keeps_the_trailing_statement(mod):
    # `def f(): <docstring>; return 1` -- the docstring and the statement
    # that follows it on the SAME physical line via `;` must not be blanked
    # together: only the docstring's own column range is the span.
    src = 'def f(): """doc"""; return 1\n'
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    ns: dict = {}
    exec(out, ns)
    assert ns["f"]() == 1


# -- semicolon-joined statements sharing the docstring's own line ------------
# (#2731 self-review: `needs_pass` keyed on "is this the def/class header
# line", which covers the inline-def case below but missed the far more
# ordinary shape of a docstring on its own line followed by `; stmt` on
# that SAME line -- at class scope and at module scope alike.)

def test_module_docstring_with_semicolon_statement_same_line(mod):
    src = '"""Module doc."""; VALUE = 1\n'
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    ns: dict = {}
    exec(out, ns)
    assert ns["VALUE"] == 1
    assert "Module doc" not in out


def test_class_docstring_with_semicolon_statement_same_line(mod):
    src = (
        "class C:\n"
        '    """Doc."""; x = 1\n'
    )
    out = _strip(mod, src)
    compile(out, "x.py", "exec")
    ns: dict = {}
    exec(compile(out, "x.py", "exec"), ns)
    assert ns["C"].x == 1
    assert "Doc." not in out


def test_function_docstring_with_semicolon_statement_same_line(mod):
    src = (
        "def f():\n"
        '    """Doc."""; return 1\n'
    )
    out = _strip(mod, src)
    ns: dict = {}
    exec(compile(out, "x.py", "exec"), ns)
    assert ns["f"]() == 1


def test_multiline_docstring_with_trailing_semicolon_statement_refused(mod):
    """A multi-line docstring whose CLOSING line also carries `; stmt` is
    refused with a clear, named BuildError rather than silently mis-handled
    or left to fail downstream with an opaque "does not parse"."""
    src = (
        "def f():\n"
        '    """line one\n'
        '    line two"""; return 1\n'
    )
    with pytest.raises(mod.BuildError, match="not supported by this stripper"):
        mod.strip_py(src.encode("utf-8"), "x.py")


# -- UTF-8 BOM -----------------------------------------------------------------

def test_leading_utf8_bom_is_kept_and_does_not_break_parsing(mod):
    """`compile()` on bytes tolerates a leading BOM; `ast.parse()` on the
    decoded `str` does not -- the BOM must be peeled off before `ast.parse`
    and put back on the result, the same shape as the shebang/encoding
    cookie treatment just above."""
    src = "\ufeffx = 1  # comment\n".encode("utf-8")
    assert src.startswith(b"\xef\xbb\xbf")
    out = mod.strip_py(src, "x.py")
    assert out.startswith(b"\xef\xbb\xbf")
    compile(out, "x.py", "exec")
    ns: dict = {}
    exec(compile(out, "x.py", "exec"), ns)
    assert ns["x"] == 1
    assert b"comment" not in out


# -- the compile()/syntax-floor guarantees the issue requires -----------------

def test_stripped_output_actually_strips_and_still_compiles(mod):
    good = (
        "def f():\n"
        '    """A docstring."""\n'
        "    # a comment\n"
        "    return 1  # noqa\n"
    )
    out = _strip(mod, good)
    assert out != good
    assert "docstring" not in out and "a comment" not in out and "noqa" not in out
    compile(out, "x.py", "exec")
    ns: dict = {}
    exec(compile(out, "x.py", "exec"), ns)
    assert ns["f"]() == 1


def test_non_utf8_source_raises_builderror_not_silently_kept(mod):
    with pytest.raises(mod.BuildError):
        mod.strip_py(b"\xff\xfe not valid utf-8", "x.py")
