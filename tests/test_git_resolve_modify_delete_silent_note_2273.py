"""git-resolve's `_hunk_note` returns a stated "could not check" line for
every case where it has nothing to compare against -- never a silent `""`,
per its own docstring contract. #2273 found the one case that DID return
`""`: a modify/delete conflict, whose working-tree file at `checkout
--theirs` time already carries no `<<<<<<<` markers, so `_block_ranges`
returns `[]` and the old code took the "unreachable on git-resolve's own
call path" branch that turns out to be reachable after all.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_2273", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

_ID = ["-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture"]


def _run(args, cwd):
    res = subprocess.run(["git", *_ID, *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return res


def _build_modify_delete_repo(tmp_path: Path) -> Path:
    """A merge where one side deletes a file and the other modifies it. Git
    resolves this as a modify/delete CONFLICT (listed by `git diff
    --name-only --diff-filter=U`), but leaves the modified side's content in
    the working tree with NO conflict markers at all -- there is nothing for
    `_block_ranges` to find.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["init"], repo)
    f = repo / "f.txt"
    f.write_text("hello\n", encoding="utf-8")
    _run(["add", "f.txt"], repo)
    _run(["commit", "-m", "init"], repo)

    _run(["checkout", "-b", "branch_modify"], repo)
    f.write_text("hello world\n", encoding="utf-8")
    _run(["commit", "-am", "modify"], repo)

    _run(["checkout", "master"], repo)
    _run(["rm", "f.txt"], repo)
    _run(["commit", "-m", "delete"], repo)

    merge = subprocess.run(["git", *_ID, "merge", "branch_modify"], cwd=repo,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    assert merge.returncode != 0, "expected a merge conflict"
    assert "modify/delete" in (merge.stdout + merge.stderr)
    return repo


def test_modify_delete_conflict_is_listed_and_carries_no_markers(tmp_path):
    """Settle the issue's own flagged-uncertain premise before trusting
    anything downstream of it.
    """
    repo = _build_modify_delete_repo(tmp_path)
    names = subprocess.run(
        ["git", *_ID, "diff", "--name-only", "--diff-filter=U", "-z"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8",
    ).stdout.split("\0")
    assert "f.txt" in names
    text = (repo / "f.txt").read_text(encoding="utf-8")
    assert "<<<<<<<" not in text


def test_checkout_theirs_on_modify_delete_never_prints_a_silent_line(monkeypatch, capsys, tmp_path):
    """MUST FIRE: `_hunk_note` must never render as nothing on git-resolve's
    own call path. Before the fix this printed no "outside-conflict check"
    line at all for a modify/delete conflict -- byte-identical to the
    deliberate 'both' omission and to a clean, verified resolution.
    """
    repo = _build_modify_delete_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "theirs", "f.txt"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "outside-conflict check" in out, (
        "the receipt must say a stated line here, not go silent -- got:\n" + out
    )


def test_hunk_note_returns_a_stated_line_when_pre_has_no_conflict_blocks():
    """Direct unit check of the function's own contract, independent of the
    CLI: PRE with no `<<<<<<<` markers at all must never yield `""`.
    """
    note = resolve._hunk_note("hello world\n", "hello world\n")
    assert note != ""
    assert "outside-conflict check" in note
