r"""#2152: a test fixture must not write a `tmp_path`-derived path into a TOML
*basic* string, or the payload is unparseable on Windows only.

PR #2148 went red on `pytest (windows-latest, 3.9)` and `(windows-latest,
3.10)` for exactly this: a fixture built its `path` field as an f-string
double-quoted TOML value with `tmp_path` interpolated directly inside the
quotes. A Windows `tmp_path` is all backslashes (`C:\Users\runneradmin\...`),
and a TOML *basic* string (`"..."`) processes backslash escapes -- `\U` reads
as "the start of an 8-hex-digit Unicode escape" and the parse fails. The
suite it happened in was the tests for backslash handling in payloads,
defeated by backslash handling in their own payload, on the only platform
whose paths contain backslashes -- invisible on Linux and macOS, where
`tmp_path` has no backslash to trip over.

Two TOML string forms never have this problem and are both in wide use
already in this suite: a **literal** string, single-quoted (`'...'`) or
triple-single-quoted (three single quotes on each side), preserves a backslash exactly as typed;
and a value built by one of the sibling helpers already spread across this
suite (`_toml_path`, `_toml_str`, `json.dumps`) escapes it correctly before
it ever reaches a basic string. Only a *bare* interpolation of `tmp_path`
straight into a double-quoted (`"..."`) field is the bug.

**What counts as path-shaped, decided:** not the field name (`path` is too
narrow -- a `content` field can carry one too, and #2152 says so explicitly)
and not "any string that could hold a backslash" (too wide to be anything
but noise over a suite with ~1670 ASCII `write_text()` fixture calls). Keyed
on the payload-writing *shape* instead, the same axis
`test_no_test_file_decodes_a_read_by_locale` uses: an f-string whose
double-quoted segment interpolates an expression that names `tmp_path` --
the one concrete, suite-wide source of platform-dependent backslashes here.
A single-quoted or triple-quoted TOML string is exempt by construction (TOML
itself, not this scan, makes it safe), and an expression already routed
through one of the safe helpers above, or through `.replace(` (hand-rolled
escaping), is exempt too -- see `_SAFE_MARKERS` below.

**Whether `test_toml_escape_divergence_684.py` needs an exemption register:
no.** Its fixtures use `tmp_path` only to place the payload *file* on disk
(`tmp_path / "p.toml"`), never inside a TOML field's own value -- the
Windows-path strings it feeds through basic strings are Python literals
(`"C:\\\\Users\\\\dev"`) with no `tmp_path` in them at all, so this scan does
not and should not match it. Confirmed by running the scan against that file
below.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent

# Wrapped in braces already means "an expression sat here"; this only needs
# to know whether that expression's own source names tmp_path.
_TMP_PATH_REF = re.compile(r"\btmp_path\b")

# An expression already escaped, by a helper this suite already uses
# (`_toml_path`, `_toml_str`) or by hand (`str(x).replace(...)`), or built
# through `json.dumps` (which escapes backslashes the same way TOML basic
# strings expect). Any of these makes the interpolation safe regardless of
# whether it names tmp_path.
_SAFE_MARKERS = ("_toml_path(", "_toml_str(", "json.dumps(", ".replace(")

#: A TOML key = "..." assignment (basic string, single line) whose body
#: interpolates a bare expression containing tmp_path. Applied to the
#: f-string with each `{expr}` placeholder still literally `{expr}` in the
#: text (see `_flatten_joinedstr`), so this is looking at source shape, not
#: at parsed TOML.
_BASIC_STRING_INTERP = re.compile(
    r'\b\w+\s*=\s*"([^"\n]*\{[^}]*\}[^"\n]*)"'
)
_PLACEHOLDER = re.compile(r"\{([^}]*)\}")


def _flatten_joinedstr(node: ast.JoinedStr) -> str:
    """Reconstruct an f-string's source shape: each `{expr}` becomes the
    literal text `{<expr source>}`, exactly where it sat in the original --
    close enough to the source for the assignment regex above to see it."""
    out = []
    for v in node.values:
        if isinstance(v, ast.Constant):
            out.append(str(v.value))
        elif isinstance(v, ast.FormattedValue):
            out.append("{" + ast.unparse(v.value) + "}")
    return "".join(out)


def toml_basic_string_tmp_path_violations(path: Path) -> list:
    """Every f-string in `path` that interpolates a bare tmp_path-naming
    expression into a double-quoted (basic-string) TOML field."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        flat = _flatten_joinedstr(node)
        if '"""' in flat:
            # A triple-double-quoted segment sits somewhere in this f-string;
            # the single-`"` regex below cannot tell that context apart from
            # a real basic string without a real TOML parser, so this skips
            # rather than risk a false positive on a literal block (#2152's
            # residual: reasoning on the false-negative side, not the noisy
            # one).
            continue
        for match in _BASIC_STRING_INTERP.finditer(flat):
            body = match.group(1)
            placeholders = _PLACEHOLDER.findall(body)
            for expr in placeholders:
                if not _TMP_PATH_REF.search(expr):
                    continue
                if any(marker in expr for marker in _SAFE_MARKERS):
                    continue
                found.append((node.lineno, expr.strip()))
    return found


def test_no_test_file_interpolates_tmp_path_into_a_toml_basic_string() -> None:
    files = sorted(TESTS_DIR.rglob("*.py"))
    assert len(files) > 100, (
        f"only found {len(files)} files under tests/ -- the walk itself is "
        "broken, and a broken walk reports the same clean sheet as one that "
        "genuinely found nothing"
    )
    offenders = []
    for path in files:
        for lineno, expr in toml_basic_string_tmp_path_violations(path):
            offenders.append(
                f"{path.relative_to(REPO)}:{lineno}: interpolates `{expr}` "
                "into a TOML basic string (\"...\")"
            )
    assert not offenders, (
        "a test fixture writes a tmp_path-derived value straight into a TOML "
        "basic string -- on Windows tmp_path is all backslashes, and a "
        "basic string processes backslash escapes (\\U wants 8 hex digits), "
        "so the payload is unparseable there and nowhere else (#2152). Use a "
        "literal string (single-quoted, or triple-single-quoted) instead, or escape it first "
        "the way _toml_path()/_toml_str() already do elsewhere in this "
        "suite:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_does_not_flag_the_684_divergence_fixtures() -> None:
    """The module docstring's own claim, checked rather than merely stated:
    test_toml_escape_divergence_684.py's Windows-path basic strings are
    Python literals, not tmp_path, so they must not appear here."""
    target = TESTS_DIR / "test_toml_escape_divergence_684.py"
    assert target.exists(), "fixture moved or renamed -- update this test"
    assert toml_basic_string_tmp_path_violations(target) == []


def test_the_scan_catches_the_naive_interpolation_and_spares_the_safe_forms(
    tmp_path: Path,
) -> None:
    """Pins both directions on one fixture file, the same shape as
    `test_the_tests_scan_catches_a_read_and_spares_the_rest`: the naive
    f-string interpolation is reported with its line number, and the
    literal-string form, the `_toml_path()`-escaped form, and a `content`
    field carrying the same bug are each exercised in the direction that
    proves the scan is not vacuous."""
    fixture = tmp_path / "test_fake_payload_2152.py"
    fixture.write_text(
        "from pathlib import Path\n"
        "\n"
        "def _toml_path(target):\n"
        "    return chr(34) + str(target).replace(chr(92), chr(92) * 2) + chr(34)\n"
        "\n"
        "def test_naive(tmp_path):\n"
        '    payload = f\'path = "{tmp_path}/p.toml"\\n\'\n'
        "\n"
        "def test_naive_content_field(tmp_path):\n"
        '    payload = f\'content = "line one {tmp_path} line two"\\n\'\n'
        "\n"
        "def test_literal_is_safe(tmp_path):\n"
        "    payload = f\"path = '{tmp_path}'\\n\"\n"
        "\n"
        "def test_triple_literal_is_safe(tmp_path):\n"
        "    payload = f\"path = \" + chr(39)*3 + \"{tmp_path}\" + chr(39)*3\n"
        "\n"
        "def test_escaped_helper_is_safe(tmp_path):\n"
        "    payload = f'path = {_toml_path(tmp_path)}\\n'\n"
        "\n"
        "def test_unrelated_variable_is_not_flagged(tmp_path):\n"
        "    other = 1\n"
        '    payload = f\'count = "{other}"\\n\'\n',
        encoding="utf-8",
    )
    found = toml_basic_string_tmp_path_violations(fixture)
    linenos = {lineno for lineno, _ in found}
    # test_naive's f-string is on the line right after its def (line 7).
    assert 7 in linenos, f"missed the naive path= interpolation: {found}"
    assert 10 in linenos, f"missed the naive content= interpolation: {found}"
    assert len(found) == 2, (
        f"expected exactly the two naive interpolations, got: {found}"
    )
