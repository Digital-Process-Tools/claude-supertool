"""git-resolve `both` on source files — refuse per file, disclose when forced (#744).

The defect is not `_union_file`: it mirrors git's own `merge=union` driver and is
correct. The defect is applying a strategy whose correctness depends on the file
type and reporting only that it completed. On `CHANGELOG.md` a union is right
almost always; on a `.py` renderer it silently duplicates a block — and the
result still parses, so neither the marker gate nor the syntax digest sees it.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

_common = sys.modules["_git_common"]


def _patch_git(monkeypatch, fn) -> None:
    monkeypatch.setattr(resolve, "_git", fn)
    monkeypatch.setattr(_common, "_git", fn)
    # `_list_conflicts` reads through `_git_verbatim` since #1708 - text mode
    # rewrites a CR inside a NUL record - so a double that stops at `_git`
    # leaves the listing running real git and every receipt reads
    # "No conflicted files."
    monkeypatch.setattr(_common, "_git_verbatim", fn)


# The shape from the rebase that filed the issue: master factored the comment
# rendering into a helper, the branch kept its own `if comments:` loop. Neither
# side is a superset of the other, and the union is two renderings of the same
# comments — valid Python, wrong output.
PY_CONFLICT = (
    "def render(issue, comments):\n"
    "    out = []\n"
    "<<<<<<< HEAD\n"
    "    out.extend(_comment_lines(comments))\n"
    "=======\n"
    "    if comments:\n"
    "        for c in comments:\n"
    "            out.append(c.body)\n"
    ">>>>>>> branch\n"
    "    return out\n"
)

MD_CONFLICT = (
    "## Unreleased\n"
    "<<<<<<< HEAD\n"
    "- fixed the thing\n"
    "=======\n"
    "- fixed the other thing\n"
    ">>>>>>> branch\n"
)


def _fake_git(calls, conflicted, staged, union_attr=()):
    """git double scoped to the four calls resolve.py makes — no blanket run stub.

    `conflicted` is the live UU set; `add --` removes a path from it, so the
    Remaining count in the receipt reflects what was actually staged.
    """
    def fake_git(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="".join(p + chr(0) for p in conflicted), stderr="")
        if args[:3] == ["check-attr", "merge", "--"]:
            rows = "".join(
                f"{p}: merge: {'union' if p in union_attr else 'unspecified'}\n" for p in args[3:])
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=rows, stderr="")
        if args[:2] == ["add", "--"]:
            staged.append(args[2])
            if args[2] in conflicted:
                conflicted.remove(args[2])
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    return fake_git


def _run(monkeypatch, capsys, argv, conflicted, union_attr=()):
    calls: list[list[str]] = []
    staged: list[str] = []
    _patch_git(monkeypatch, _fake_git(calls, conflicted, staged, union_attr))
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: "validate: ok" for p in ps})
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", *argv])
    rc = resolve.main()
    return rc, capsys.readouterr().out, staged


# ---------------------------------------------------------------------------
# The hazard itself — why a syntax check proves nothing here
# ---------------------------------------------------------------------------

def test_union_of_python_parses_cleanly_while_duplicating_the_render(tmp_path) -> None:
    """The union is valid Python AND renders every comment twice.

    This is the evidence the guard exists for: `ast.parse` accepts the result,
    so the receipt's `validate: ok` digest is true and useless.
    """
    f = tmp_path / "renderer.py"
    f.write_text(PY_CONFLICT, encoding="utf-8")
    ok, err = resolve._union_file(str(f))
    assert ok and err == ""
    text = f.read_text(encoding="utf-8")

    ast.parse(text)  # clean parse — the failure is semantic, not syntactic
    assert "_comment_lines(comments)" in text
    assert "for c in comments:" in text  # both renderings, back to back


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------

def test_both_all_refuses_python_source_and_leaves_it_conflicted(monkeypatch, capsys, tmp_path) -> None:
    f = tmp_path / "renderer.py"
    f.write_text(PY_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])

    assert rc == 1
    assert "refused" in out.lower()
    assert "Refused: 1" in out
    assert "force" in out  # the receipt names the way through
    assert staged == []
    # File untouched — markers intact, so git itself blocks `rebase --continue`
    assert f.read_text(encoding="utf-8") == PY_CONFLICT


def test_both_all_mixed_set_resolves_markdown_and_refuses_python(monkeypatch, capsys, tmp_path) -> None:
    """The real-world call: one CHANGELOG plus four renderers.

    The habit case still works — refusing the whole set would only train the
    override. Only the files where union is wrong are held back.
    """
    md = tmp_path / "CHANGELOG.md"
    md.write_text(MD_CONFLICT, encoding="utf-8")
    pys = []
    for name in ("a.py", "b.py", "c.py", "d.py"):
        p = tmp_path / name
        p.write_text(PY_CONFLICT, encoding="utf-8")
        pys.append(p)

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(md), *[str(p) for p in pys]])

    assert rc == 1
    assert staged == [str(md)]
    assert "Resolved: 1" in out and "Refused: 4" in out
    assert "- fixed the thing" in md.read_text(encoding="utf-8")
    assert "- fixed the other thing" in md.read_text(encoding="utf-8")
    for p in pys:
        assert p.read_text(encoding="utf-8") == PY_CONFLICT
    assert "Remaining: 4" in out


def test_ours_on_source_is_unaffected(monkeypatch, capsys, tmp_path) -> None:
    """The guard is about union semantics only — a side pick is never blocked."""
    f = tmp_path / "renderer.py"
    f.write_text("clean\n", encoding="utf-8")
    rc, out, staged = _run(monkeypatch, capsys, ["ours", "all"], [str(f)])
    assert rc == 0
    assert staged == [str(f)]
    assert "Refused" not in out


def test_both_on_non_source_is_unaffected(monkeypatch, capsys, tmp_path) -> None:
    f = tmp_path / "CHANGELOG.md"
    f.write_text(MD_CONFLICT, encoding="utf-8")
    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)])
    assert rc == 0
    assert staged == [str(f)]
    assert "Refused" not in out


# ---------------------------------------------------------------------------
# The two ways through
# ---------------------------------------------------------------------------

def test_force_token_unions_source_and_discloses_in_tally(monkeypatch, capsys, tmp_path) -> None:
    f = tmp_path / "renderer.py"
    f.write_text(PY_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all", "force"], [str(f)])

    assert rc == 0
    assert staged == [str(f)]
    assert "source" in out and "verify manually" in out
    text = f.read_text(encoding="utf-8")
    assert "_comment_lines(comments)" in text and "for c in comments:" in text


def test_merge_union_gitattribute_allows_both_on_source(monkeypatch, capsys, tmp_path) -> None:
    """A repo that declares `merge=union` for a path has already answered this."""
    f = tmp_path / "generated_list.py"
    f.write_text(PY_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", "all"], [str(f)], union_attr=(str(f),))

    assert rc == 0
    assert staged == [str(f)]
    assert "Refused" not in out


def test_check_attr_failure_does_not_open_the_gate(monkeypatch, capsys, tmp_path) -> None:
    """git check-attr unavailable → fall back to refusing, never to unioning."""
    f = tmp_path / "renderer.py"
    f.write_text(PY_CONFLICT, encoding="utf-8")
    calls: list[list[str]] = []
    staged: list[str] = []
    inner = _fake_git(calls, [str(f)], staged)

    def fake_git(args, timeout=10):
        if args[:3] == ["check-attr", "merge", "--"]:
            return subprocess.CompletedProcess(args=args, returncode=128, stdout="", stderr="boom")
        return inner(args, timeout)

    _patch_git(monkeypatch, fake_git)
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: "validate: ok" for p in ps})
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "both", "all"])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "Refused: 1" in out
    assert staged == []


# ---------------------------------------------------------------------------
# Partial (BLOCKS) path — same strategy, same guard
# ---------------------------------------------------------------------------

def test_partial_both_on_source_is_refused(monkeypatch, capsys, tmp_path) -> None:
    f = tmp_path / "renderer.py"
    f.write_text(PY_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", str(f), "1"], [str(f)])

    assert rc == 1
    assert "refused" in out.lower()
    assert staged == []
    assert f.read_text(encoding="utf-8") == PY_CONFLICT


def test_partial_both_on_source_forced_runs(monkeypatch, capsys, tmp_path) -> None:
    f = tmp_path / "renderer.py"
    f.write_text(PY_CONFLICT, encoding="utf-8")

    rc, out, staged = _run(monkeypatch, capsys, ["both", str(f), "1", "force"], [str(f)])

    assert rc == 0
    assert staged == [str(f)]
    text = f.read_text(encoding="utf-8")
    assert "_comment_lines(comments)" in text and "for c in comments:" in text


def test_force_token_is_not_read_as_a_block_selector(monkeypatch, capsys, tmp_path) -> None:
    """`both:all:force` must not fail the BLOCKS integer check."""
    f = tmp_path / "CHANGELOG.md"
    f.write_text(MD_CONFLICT, encoding="utf-8")
    rc, out, staged = _run(monkeypatch, capsys, ["both", "all", "force"], [str(f)])
    assert "BLOCKS must be" not in out
    assert rc == 0


# ---------------------------------------------------------------------------
# The discriminator itself
# ---------------------------------------------------------------------------

def test_source_extension_matching_is_case_insensitive() -> None:
    assert resolve._is_source_path("Foo.PY")
    assert resolve._is_source_path("dir/bar.Js")
    assert not resolve._is_source_path("CHANGELOG.md")
    assert not resolve._is_source_path("notes.txt")
    assert not resolve._is_source_path("no_extension_at_all")
