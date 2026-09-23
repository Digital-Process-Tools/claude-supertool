"""
The same defect -- rendering untrusted remote text without flattening every
line-boundary separator first -- has now been fixed three separate times at
three different call sites (#2671, #2680, #2681), each time by replacing a
hand-rolled

    text.replace("\n", " ")

(or the CRLF-then-LF pair `.replace("\r", " ").replace("\n", " ")`) with a
call to `_untrusted.flat()`. The hand-rolled idiom only ever strips `\r` and
`\n` -- it leaves U+2028, U+2029, VT, FF, FS, GS, RS and NEL untouched, every
one of which `str.splitlines()` (and this repo's own render conventions)
still treats as a line boundary, so attacker-chosen text carrying one of them
can still land at column 0 of a forged output row (#886).

Each round found the idiom by hand, in a self-review, never by a test (#2686).
This is that guard: an AST scan for the literal idiom itself -- a `.replace()`
call whose first argument is one of the three separator-only string literals
`"\r"`, `"\n"`, `"\r\n"` -- across the trees that render remote/tracker text.
Deliberately syntactic rather than a taint-and-sink scan like
`test_forged_branch_line_965.py`'s: the forged fields here (post text, comment
body, channel title, notification text) share no small common key vocabulary
the way a git refname does (`headRefName`, `baseRefName`, ...), so there is no
narrow field-name allowlist to key a taint scan on. The idiom itself -- a
literal-separator `.replace()` -- IS the narrow, low-false-positive signature:
`_untrusted.flat()` and `.scrub()` use named module constants (`_CRLF`, `_LF`),
never these three literals, so the canonical fix itself never trips this scan.

**Scanned trees, and why**: exactly the presets that render text chosen by
someone other than this repo's own operator -- `bluesky`, `devto`, `hashnode`,
`youtube`, `slack` (a publish/reply-echo preset whose own docstrings already
name reply text as stranger-authored -- see `presets/slack/publish.py`),
`watch` (poller-sourced notification/engagement text), `github`, `gitlab`,
`git` (the existing #965 scanner's own trees) and `notifiers`.
`presets/xml/_common.py` and `presets/claude-log/_common.py` are deliberately
NOT scanned: #2681's own commit message excludes both as rendering *local*
files, not remote/tracker text, and neither sits under any of the trees named
here (both are direct siblings of `presets/`'s per-service subdirectories).
`presets/_repo_target.py`'s `_one_line()` -- which does `.replace("\r\n",
"\n")` once, deliberately, ahead of an `isprintable()` sweep documented in its
own docstring as covering the rest of the separator set -- is excluded the
same way, by tree rather than by name: it is a top-level `presets/*.py` file,
not inside any scanned subdirectory, exactly like `_untrusted.py` itself.
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).parent.parent

#: The literal idiom every one of #2671/#2680/#2681's fixes replaced. Each is
#: a *complete* line-boundary-only value: a `.replace()` call passing one of
#: these strips exactly the two separators `str.splitlines()` was already
#: agreeing with, and nothing that also breaks on U+2028/U+2029/VT/FF/FS/GS/
#: RS/NEL. `_untrusted.flat()`/`.scrub()` never pass one of these three as a
#: literal -- they read `_CR`/`_LF`/`_CRLF`, module-level names built from
#: `chr(13)`/`chr(10)`, so the canonical fix is structurally invisible to
#: this scan.
BARE_SEPARATOR_LITERALS = frozenset({"\r", "\n", "\r\n"})

#: The trees that render text chosen by someone other than this repo's own
#: operator: a post, a comment, a PR/MR field, a poller's own payload. Not
#: "every preset" -- `presets/xml` and `presets/claude-log` render local
#: files (#2681's own commit message), and are excluded by simply not being
#: named here, the same way `_untrusted.py` and `_repo_target.py` (both
#: direct `presets/*.py` siblings, not inside any of these subdirectories)
#: are excluded by not being reached by `rglob` at all.
_SCANNED = (
    "presets/bluesky", "presets/devto", "presets/hashnode", "presets/youtube",
    "presets/slack", "presets/watch", "presets/github", "presets/gitlab",
    "presets/git", "notifiers",
)


def _bare_separator_replaces(path: Path, label: "str | None" = None) -> list[str]:
    """`label` is what a finding is reported under -- the file's path relative
    to the repo root by default, NOT `path.name` alone. #2671/#2680/#2681's own
    fixed sites share filenames across trees (`list.py`, `read.py`,
    `status_since.py` each exist in bluesky/devto/hashnode/youtube), so a
    basename-only label would collapse two distinct offending files at the
    same line number into one displayed finding under `set()`-dedup --
    correctly still failing the assertion, but under-reporting how many files
    need fixing.
    """
    if label is None:
        try:
            label = str(path.relative_to(_ROOT))
        except ValueError:
            label = path.name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace" and node.args):
            continue
        first = node.args[0]
        if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                and first.value in BARE_SEPARATOR_LITERALS):
            found.append(f"{label}:{node.lineno} .replace({first.value!r}, ...)")
    return sorted(set(found))


def test_no_preset_hand_rolls_the_bare_separator_replace_idiom() -> None:
    """checked N files, M trees (#1089's own convention, restated for #2686):
    a guard that cannot state its own boundary is the three-state defect this
    repo has filed more than any other, wearing a passing test's clothes.
    Printed unconditionally -- on the clean run too, not only on a finding --
    so a reader of `pytest -rA` or a failure's captured stdout sees the scope
    this assertion actually claims, not just its verdict.
    """
    offenders: list[str] = []
    scanned_files = 0
    for directory in _SCANNED:
        base = _ROOT / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            scanned_files += 1
            offenders.extend(_bare_separator_replaces(path))
    print(
        f"checked {scanned_files} files across {len(_SCANNED)} trees "
        f"({', '.join(_SCANNED)}), {len(BARE_SEPARATOR_LITERALS)} bare-"
        f"separator literals tracked "
        f"({', '.join(sorted(repr(s) for s in BARE_SEPARATOR_LITERALS))})."
    )
    # A tree renamed or moved out from under `_SCANNED` (#965's own sibling
    # scanner has the identical gap: it prints its file count too but never
    # floors it) must not let this guard go quietly dark for that tree --
    # `offenders == []` alone cannot tell "nothing to find" from "nothing was
    # looked at". Per-tree, not just an aggregate floor, so one moved
    # directory among nine is still named rather than lost in the total.
    missing = [d for d in _SCANNED if not (_ROOT / d).exists()]
    assert not missing, (
        f"{len(missing)} of {len(_SCANNED)} scanned trees do not exist -- "
        f"this guard has gone dark for them: {', '.join(missing)}")
    assert scanned_files > 0, (
        "0 files scanned across all of _SCANNED -- this guard is not "
        "looking at anything")
    assert offenders == [], (
        "a .replace() call hand-rolls the bare CR/LF-only flattening idiom "
        "instead of calling _untrusted.flat() (or .scrub()), which also "
        "handles U+2028/U+2029/VT/FF/FS/GS/RS/NEL:\n  "
        + "\n  ".join(sorted(set(offenders))))


def test_the_scanner_sees_the_defect_it_was_written_for(tmp_path: Path) -> None:
    """A scanner that cannot fail is not a guard (#851's own lesson)."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def render(rec):\n"
        "    text = (rec.get('text') or '').replace('\\n', ' ')[:160]\n"
        "    chained = (rec.get('body') or '').replace('\\r', ' ').replace('\\n', ' ')\n"
        "    safe = _untrusted.flat(rec.get('title') or '')\n"
        "    return f\"{text} {chained} {safe}\"\n",
        encoding="utf-8",
    )
    found = _bare_separator_replaces(sample)
    assert any(chr(92) + "n" in f for f in found)
    assert any(chr(92) + "r" in f for f in found)
    # `flat()` itself must never be mistaken for the idiom it replaces.
    assert not any("title" in f for f in found)
    assert len(found) == 3
