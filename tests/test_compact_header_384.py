"""Compact op header (#384) and the remaining DX papercuts from #380.

Every mutating op prints its full arguments in the section header and then the
diff underneath, so for a content-heavy edit the old and new strings appear
twice. On a 6-op batch carrying 10-20 line code blocks, the headers alone were
a large fraction of the response — and the cost scales with exactly the usage
pattern supertool encourages.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


def _long(tag: str, n: int = 20) -> str:
    return "\n".join(f"    line {i} of the {tag} block here" for i in range(n))


# ---------------------------------------------------------------------------
# header stays verbatim when it is already cheap
# ---------------------------------------------------------------------------

def test_short_edit_keeps_its_verbatim_header(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    arg = f"edit:::a = 1:::a = 2:::{f}"
    out = supertool.dispatch(arg)
    assert out.startswith(f"--- {arg} ---\n")


def test_read_op_header_is_never_rebuilt(tmp_path: Path) -> None:
    """Only mutating ops carry duplicated content; reads keep their header."""
    f = tmp_path / "x.py"
    f.write_text("a = 1\n" * 200)
    arg = f"read:{f}:::grep=" + ("a" * 200)
    out = supertool.dispatch(arg)
    assert out.startswith(f"--- {arg} ---\n")


# ---------------------------------------------------------------------------
# header is rebuilt when the arguments get heavy
# ---------------------------------------------------------------------------

def test_long_edit_header_names_anchor_and_path(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    old, new = _long("old"), _long("new")
    f.write_text(old + "\n")
    out = supertool.dispatch(f"edit:::{old}:::{new}:::{f}")
    head = out.splitlines()[0]
    assert head.startswith('--- edit: "    line 0 of the old block here')
    assert head.endswith(f"→ {f} ---")
    assert "NEW block" not in head
    # The whole point: the header is now a fraction of the arguments.
    assert len(head) < len(old)


def test_long_header_reports_how_much_it_elided(tmp_path: Path) -> None:
    """A silent truncation reads as 'that was the whole argument'."""
    f = tmp_path / "x.py"
    old = _long("old")
    f.write_text(old + "\n")
    out = supertool.dispatch(f"edit:::{old}:::changed:::{f}")
    assert "chars)" in out.splitlines()[0]


def test_long_header_is_one_line(tmp_path: Path) -> None:
    """A multi-line anchor previously produced a multi-line header."""
    f = tmp_path / "x.py"
    old = _long("old")
    f.write_text(old + "\n")
    out = supertool.dispatch(f"edit:::{old}:::changed:::{f}")
    assert out.splitlines()[0].endswith("---")
    assert "⏎" in out.splitlines()[0]


def test_batch_sub_op_header_is_compact(tmp_path: Path) -> None:
    """The case from the issue — a batch joins its parts into the header arg."""
    f = tmp_path / "x.py"
    old, new = _long("old"), _long("new")
    f.write_text(old + "\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(f), "old": old, "new": new},
    ]))
    out = supertool.dispatch(f"batch:@{payload}")
    sub_header = next(l for l in out.splitlines() if l.startswith("--- edit"))
    # Previously the header held old + new in full, twice over with the diff.
    assert len(sub_header) < len(old) + len(new)
    assert "new block" not in sub_header


def test_long_paste_header_drops_the_content(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    content = _long("pasted")
    out = supertool.dispatch(f"paste:::{f}:::{content}")
    head = out.splitlines()[0]
    assert str(f) in head
    assert "chars)" in head
    assert "pasted block" not in head


def test_long_replace_lines_header_keeps_the_range(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n")
    out = supertool.dispatch(f"replace_lines:::{f}:::2:::2:::{_long('new')}")
    head = out.splitlines()[0]
    assert "lines 2-2" in head
    assert str(f) in head
    assert "new block" not in head


def test_failed_edit_keeps_its_verbatim_header(tmp_path: Path) -> None:
    """On failure no diff renders, so the header is the only surviving copy of
    what the caller sent — eliding it takes the reproduction material away at
    the one moment it is needed."""
    f = tmp_path / "x.py"
    f.write_text("something else entirely\n")
    old = _long("old")
    arg = f"edit:::{old}:::changed:::{f}"
    out = supertool.dispatch(arg)
    assert "ERROR: old string not found" in out
    assert out.startswith(f"--- {arg} ---\n")
    assert "line 19 of the old block here" in out


def test_failed_write_keeps_its_verbatim_header(tmp_path: Path) -> None:
    """Same rule for a write that never happened, whatever the reason."""
    content = _long("pasted")
    arg = f"paste:::{tmp_path}:::{content}"  # a directory — cannot be written
    out = supertool.dispatch(arg)
    assert "ERROR" in out
    assert out.startswith(f"--- {arg} ---\n")


def test_compact_header_helper_returns_empty_for_unknown_op() -> None:
    """A non-mutating op keeps its verbatim header rather than losing args."""
    assert supertool._compact_header_arg("grep", ["grep", "x", "y"]) == ""


# ---------------------------------------------------------------------------
# #380 — `\\` at end of line in a shell script
# ---------------------------------------------------------------------------

def test_trailing_double_backslash_in_sh_is_flagged(tmp_path: Path) -> None:
    """`bash -n` accepts it and it runs differently — only the bytes show it."""
    f = tmp_path / "deploy.sh"
    payload = tmp_path / "p.toml"
    payload.write_text(
        f'path = "{f.as_posix()}"\n'
        "content = '''FOO=$(cmd \\\\\n    --arg)\n'''\n"
    )
    out = supertool.dispatch(f"paste:@{payload}")
    assert "a line ends with" in out
    assert "escaped backslash, not a line continuation" in out


def test_clean_shell_script_is_not_flagged(tmp_path: Path) -> None:
    """A real line continuation — a single trailing backslash — stays quiet."""
    f = tmp_path / "deploy.sh"
    payload = tmp_path / "p.toml"
    payload.write_text(
        f'path = "{f.as_posix()}"\n'
        "content = '''FOO=$(cmd \\\n    --arg)\n'''\n"
    )
    out = supertool.dispatch(f"paste:@{payload}")
    assert "a line ends with" not in out


def test_double_backslash_outside_a_shell_script_is_not_flagged(tmp_path: Path) -> None:
    """In Python, C, or JSON a `\\\\` at end of line is ordinary."""
    f = tmp_path / "x.py"
    payload = tmp_path / "p.toml"
    payload.write_text(
        f'path = "{f.as_posix()}"\n'
        "content = '''SEP = \"\\\\\\\\\"\n'''\n"
    )
    out = supertool.dispatch(f"paste:@{payload}")
    assert "a line ends with" not in out


def test_warning_does_not_leak_into_the_next_op(tmp_path: Path) -> None:
    """The queue is drained per dispatch, not accumulated for the process."""
    sh = tmp_path / "deploy.sh"
    payload = tmp_path / "p.toml"
    payload.write_text(
        f'path = "{sh.as_posix()}"\n'
        "content = '''FOO=$(cmd \\\\\n    --arg)\n'''\n"
    )
    assert "a line ends with" in supertool.dispatch(f"paste:@{payload}")
    other = tmp_path / "y.py"
    out = supertool.dispatch(f"paste:::{other}:::x = 1")
    assert "a line ends with" not in out


def test_sh_helper_matches_only_the_shell_suffixes() -> None:
    bad = "cmd \\\\\n"
    assert supertool._sh_backslash_warning("a.sh", bad)
    assert supertool._sh_backslash_warning("a.bash", bad)
    assert supertool._sh_backslash_warning("a.zsh", bad)
    assert not supertool._sh_backslash_warning("a.py", bad)
    assert not supertool._sh_backslash_warning("a.sh", "cmd \\\n")


def test_zero_match_replace_keeps_its_verbatim_header(tmp_path: Path) -> None:
    """op_replace's zero-match returns "(0 occurrences of 'x' found)" — a
    failure that never says ERROR. The header swap tests whether anything was
    WRITTEN, not whether the receipt looks like an error."""
    f = tmp_path / "x.py"
    f.write_text("nothing matching here\n")
    old = _long("old")
    arg = f"replace:::{old}:::changed:::{f}"
    out = supertool.dispatch(arg)
    assert "0 occurrences" in out
    assert out.startswith(f"--- {arg} ---\n")
    assert "line 19 of the old block here" in out


def test_replace_dry_keeps_its_verbatim_header(tmp_path: Path) -> None:
    """A preview writes nothing, so its arguments are all the caller has."""
    f = tmp_path / "x.py"
    old = _long("old")
    f.write_text(old + "\n")
    arg = f"replace_dry:::{old}:::changed:::{f}"
    out = supertool.dispatch(arg)
    assert out.startswith(f"--- {arg} ---\n")


def test_successful_replace_gets_the_compact_header(tmp_path: Path) -> None:
    """The counterpart: a write did happen, so the elision is safe."""
    f = tmp_path / "x.py"
    old = _long("old")
    f.write_text(old + "\n")
    out = supertool.dispatch(f"replace:::{old}:::changed:::{f}")
    assert out.splitlines()[0].startswith('--- replace: "    line 0 of the old')


def test_backslash_before_trailing_whitespace_is_flagged(tmp_path: Path) -> None:
    """A backslash followed by spaces never continues the line — the escape
    applies to the space — and the difference is invisible in a diff."""
    warning = supertool._sh_backslash_warning("a.sh", "cmd \\ \n")
    assert "followed by whitespace" in warning


def test_trailing_whitespace_case_is_parity_independent(tmp_path: Path) -> None:
    for run in range(1, 5):
        content = "cmd " + "\\" * run + "  \n"
        assert supertool._sh_backslash_warning("a.sh", content), run


@pytest.mark.parametrize("n,warns", [(1, False), (2, True), (3, False),
                                     (4, True), (5, False)])
def test_backslash_run_parity_decides(n: int, warns: bool) -> None:
    """Bash consumes trailing backslashes pairwise from the left, so parity is
    the whole question. A run of 3 is one escaped backslash plus a genuine
    continuation — correct code, and a substring check for `\\\\` flags it."""
    content = "cmd " + "\\" * n + "\n"
    assert bool(supertool._sh_backslash_warning("a.sh", content)) is warns


def test_rollback_retracts_its_warning(tmp_path: Path) -> None:
    """The bytes complained about are no longer on disk after a rollback, and a
    warning about them would be worse than none."""
    f = tmp_path / "deploy.sh"
    supertool._WRITE_WARNINGS.clear()
    supertool._atomic_write(str(f), "cmd \\\\\n")
    assert supertool._WRITE_WARNINGS, "warning was not queued"
    supertool._drop_write_warnings(str(f))
    assert supertool._WRITE_WARNINGS == []


def test_rewriting_a_path_replaces_its_warning(tmp_path: Path) -> None:
    """The queue describes what is on disk now, not every write that happened."""
    f = tmp_path / "deploy.sh"
    supertool._WRITE_WARNINGS.clear()
    supertool._atomic_write(str(f), "cmd \\\\\n")
    assert len(supertool._WRITE_WARNINGS) == 1
    supertool._atomic_write(str(f), "cmd \\\n")   # fixed — a real continuation
    assert supertool._WRITE_WARNINGS == []


# ---------------------------------------------------------------------------
# #380 — error wording
# ---------------------------------------------------------------------------

def test_path_not_found_names_the_cwd_op(tmp_path: Path, monkeypatch) -> None:
    """The failure message is where someone learns `cwd:` exists."""
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("foo", "no/such/dir")
    assert "ERROR: path not found" in out
    assert "cwd:PATH" in out
