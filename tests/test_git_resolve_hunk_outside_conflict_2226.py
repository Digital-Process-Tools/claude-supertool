"""git-resolve's whole-file checkout can silently revert content outside any
conflict block — #2226. `checkout --ours/--theirs` is git's own whole-file
operation; a file with content that diverged between the sides OUTSIDE any
marked conflict is rewritten right along with it, and until this fix the
receipt (`markers: clean`, `Resolved: 1`) said nothing about it.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_2226", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

_ID = ["-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture"]


def _run(args, cwd):
    res = subprocess.run(["git", *_ID, *args], cwd=cwd, capture_output=True, text=True)
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return res


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _base_text(conflict: str = "v0", other: str = "v0") -> str:
    """Two changeable paragraphs separated by enough unchanged filler lines
    that git's own hunk-granularity diff (xdiff) keeps them as two separate
    hunks rather than folding them into one conflict block — the same
    distance (roughly) the real incident had (#2226: 300 lines).
    """
    lines = ["header\n"]
    lines += [f"filler {i}\n" for i in range(1, 6)]
    lines.append(f"conflict para: {conflict}\n")
    lines += [f"filler {i}\n" for i in range(6, 11)]
    lines.append(f"other para: {other}\n")
    lines += [f"filler {i}\n" for i in range(11, 13)]
    return "".join(lines)


def _build_repo_with_unrelated_change(tmp_path: Path) -> Path:
    """Reproduce the #2226 incident: one conflicted paragraph, plus a second
    paragraph that only ONE side changed after the branch point — it
    auto-merges cleanly, carries no markers, and yet a whole-file
    `checkout --theirs` discards it anyway.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["init"], repo)
    f = repo / "doc.md"
    _write(f, _base_text())
    _run(["add", "."], repo)
    _run(["commit", "-m", "base"], repo)

    _run(["branch", "theirs-branch"], repo)

    _write(f, _base_text(conflict="ours"))
    _run(["commit", "-am", "ours changes conflict para"], repo)
    _write(f, _base_text(conflict="ours", other="v1 -- merged 40 minutes earlier"))
    _run(["commit", "-am", "unrelated change lands on ours"], repo)

    _run(["checkout", "theirs-branch"], repo)
    _write(f, _base_text(conflict="theirs"))
    _run(["commit", "-am", "theirs changes conflict para"], repo)

    _run(["checkout", "master"], repo)
    merge = subprocess.run(["git", *_ID, "merge", "theirs-branch"], cwd=repo,
                            capture_output=True, text=True)
    assert merge.returncode != 0, "expected a merge conflict"
    return repo


def _build_repo_conflict_only(tmp_path: Path) -> Path:
    """Same shape, but the "other" paragraph never diverges between sides —
    the must-not-fire control for the fixture above.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["init"], repo)
    f = repo / "doc.md"
    _write(f, _base_text())
    _run(["add", "."], repo)
    _run(["commit", "-m", "base"], repo)

    _run(["branch", "theirs-branch"], repo)

    _write(f, _base_text(conflict="ours"))
    _run(["commit", "-am", "ours changes conflict para"], repo)

    _run(["checkout", "theirs-branch"], repo)
    _write(f, _base_text(conflict="theirs"))
    _run(["commit", "-am", "theirs changes conflict para"], repo)

    _run(["checkout", "master"], repo)
    merge = subprocess.run(["git", *_ID, "merge", "theirs-branch"], cwd=repo,
                            capture_output=True, text=True)
    assert merge.returncode != 0, "expected a merge conflict"
    return repo


def _resolve_theirs(repo: Path, monkeypatch, capsys) -> str:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "theirs", "doc.md"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    return out


def test_checkout_theirs_reports_the_hunk_it_moved_outside_the_conflict(monkeypatch, capsys, tmp_path):
    """MUST FIRE: an unrelated, unconflicted paragraph gets silently reverted
    by the whole-file checkout, and the receipt must name it.
    """
    repo = _build_repo_with_unrelated_change(tmp_path)
    out = _resolve_theirs(repo, monkeypatch, capsys)
    assert "1 conflict block(s)" in out
    assert "1 outside any conflict" in out
    # The unrelated paragraph really was reverted -- prove the mechanism, not
    # just the words in the receipt.
    assert (repo / "doc.md").read_text() == _base_text(conflict="theirs", other="v0")


def test_checkout_theirs_says_nothing_extra_when_only_the_conflict_moved(monkeypatch, capsys, tmp_path):
    """MUST NOT FIRE: a resolution that only touches the conflicted block
    never says "outside any conflict" — even though difflib's own hunk
    count for a whole-file rewrite is not always literally 1 (matching text
    around the resolved block can split it into more than one opcode); the
    receipt line the caller has to trust is the "outside" clause, not the
    raw hunk count.
    """
    repo = _build_repo_conflict_only(tmp_path)
    out = _resolve_theirs(repo, monkeypatch, capsys)
    assert "1 conflict block(s)" in out
    assert "outside" not in out


def test_hunk_note_excludes_the_conflict_block_lines_themselves(monkeypatch):
    """Judgment call: a naive line-diff between pre- and post-resolution text
    would count the conflict block's OWN replaced lines as "outside" the
    conflict, since every one of them differs. `_hunk_note` must exclude the
    block's own span before counting, or every ordinary resolution triggers
    a false alarm.
    """
    pre = (
        "keep me\n"
        "<<<<<<< HEAD\n"
        "ours line\n"
        "=======\n"
        "theirs line\n"
        ">>>>>>> branch\n"
        "keep me too\n"
    )
    post = "keep me\ntheirs line\nkeep me too\n"
    note = resolve._hunk_note(pre, post)
    assert "outside" not in note
    assert "1 conflict block(s)" in note


def test_hunk_note_says_nothing_when_it_cannot_read_either_snapshot():
    """Three states, not two (#1858 pattern): a check that could not run must
    not render as a clean "0 outside" result.
    """
    assert resolve._hunk_note(None, "post\n") == ""
    assert resolve._hunk_note("pre\n", None) == ""
