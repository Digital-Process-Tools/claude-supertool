"""`@payload` references resolve against the invocation directory, not `cwd:`. #672

`cwd:` exists because the target repo has no `./supertool` wrapper — so the call
is made from a directory that has one, and the payload the caller just wrote
lands *there*, next to the call. Resolving the `@reference` against the `cwd:`
target therefore looks for it on the wrong side of the boundary every time,
which is the documented workflow, not a corner of it.

Two kinds of path are conflated today and are separated here:

  * the ``@reference`` — an argument the caller typed, resolved against the
    directory the call was made from;
  * ``path = `` *inside* the payload — repo content, resolved against ``cwd:``.

The second must keep working exactly as before; that is what makes `cwd:`
useful at all.

No fallback: a payload that resolves under neither root is an error naming both
roots, never a silent second lookup. Guessing which root a file happens to sit
under is how a tool starts reading a file the caller did not mean.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import supertool


@pytest.fixture
def restore_cwd():
    """main() mutates process cwd; snapshot + restore so it doesn't bleed."""
    saved = os.getcwd()
    yield
    os.chdir(saved)


@pytest.fixture
def two_roots(tmp_path: Path, monkeypatch, restore_cwd):
    """caller/ (where the call is made) and repo/ (where cwd: points)."""
    caller = tmp_path / "caller"
    repo = tmp_path / "repo"
    caller.mkdir()
    repo.mkdir()
    (repo / "target.txt").write_text("HELLO-672\n", encoding="utf-8")
    (caller / "target.txt").write_text("WRONG-ROOT\n", encoding="utf-8")
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)
    monkeypatch.chdir(caller)
    return caller, repo


# ---------------------------------------------------------------------------
# The reported case: read-op payload route
# ---------------------------------------------------------------------------

def test_read_payload_resolves_against_invocation_dir(two_roots, capsys) -> None:
    caller, repo = two_roots
    (caller / "p.toml").write_text('path = "target.txt"' + chr(10), encoding="utf-8")

    rc = supertool.main([f"cwd:{repo}", "read:@p.toml"])
    out = capsys.readouterr().out

    assert rc == 0
    # payload found next to the call...
    assert "not found" not in out
    # ...and the in-payload path still resolved against cwd:, not the caller.
    assert "HELLO-672" in out
    assert "WRONG-ROOT" not in out


def test_batch_payload_resolves_against_invocation_dir(two_roots, capsys) -> None:
    caller, repo = two_roots
    (caller / "b.toml").write_text(
        json.dumps({"ops": [{"op": "read", "path": "target.txt"}]}), encoding="utf-8"
    )

    rc = supertool.main([f"cwd:{repo}", "batch:@b.toml"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "HELLO-672" in out
    assert "WRONG-ROOT" not in out


def test_mutating_payload_resolves_against_invocation_dir(two_roots, capsys) -> None:
    caller, repo = two_roots
    (caller / "e.toml").write_text(
        json.dumps({"path": "target.txt", "old": "HELLO-672", "new": "EDITED-672"}),
        encoding="utf-8",
    )

    supertool.main([f"cwd:{repo}", "edit:@e.toml"])
    capsys.readouterr()

    assert (repo / "target.txt").read_text(encoding="utf-8") == "EDITED-672" + chr(10)
    assert (caller / "target.txt").read_text(encoding="utf-8") == "WRONG-ROOT" + chr(10)


# ---------------------------------------------------------------------------
# The error must stop lying: an absence produced by a moved root says so
# ---------------------------------------------------------------------------

def test_missing_everywhere_names_both_roots(two_roots, capsys) -> None:
    caller, repo = two_roots

    supertool.main([f"cwd:{repo}", "read:@nope.toml"])
    captured = capsys.readouterr()
    out = captured.out + captured.err

    assert "nope.toml" in out
    assert "invocation directory" in out
    assert str(caller) in out          # the root actually searched
    assert str(repo) in out            # the root the reader will assume
    assert "cwd:" in out


def test_payload_present_only_under_cwd_target_says_so(two_roots, capsys) -> None:
    caller, repo = two_roots
    (repo / "p.toml").write_text('path = "target.txt"' + chr(10), encoding="utf-8")

    rc = supertool.main([f"cwd:{repo}", "read:@p.toml"])
    out = capsys.readouterr().out

    # Not read from the wrong root — the whole point of declining to guess.
    assert "HELLO-672" not in out
    assert "ERROR" in out
    assert str(caller) in out
    assert str(repo) in out
    # The branch that knows *why* it failed: names the file it found under the
    # other root, and the absolute path that would have worked. A generic
    # "pass an absolute one" would satisfy a laxer assertion than this.
    assert "does exist under the cwd: target" in out
    assert str(repo / "p.toml") in out
    assert rc != 0


def test_missing_absolute_payload_stays_a_plain_absence(two_roots, capsys) -> None:
    """An absolute path that is not there is an absence, not a moved root."""
    caller, repo = two_roots

    supertool.main([f"cwd:{repo}", f"read:@{caller}/nope.toml"])
    out = capsys.readouterr().out

    assert "not found" in out
    assert "invocation directory" not in out   # no root-shift lecture to give


def test_batch_missing_payload_names_both_roots(two_roots, capsys) -> None:
    caller, repo = two_roots

    supertool.main([f"cwd:{repo}", "batch:@nope.toml"])
    out = capsys.readouterr().out

    assert "invocation directory" in out
    assert str(caller) in out
    assert str(repo) in out


# ---------------------------------------------------------------------------
# Same defect class: the auto-cwd drift recovery (#363) moves the root too
# ---------------------------------------------------------------------------

def test_auto_cwd_root_does_not_move_payload_root(two_roots, capsys, monkeypatch) -> None:
    caller, repo = two_roots
    (caller / "p.toml").write_text('path = "target.txt"' + chr(10), encoding="utf-8")
    monkeypatch.setattr(supertool, "_auto_cwd_root", lambda argv: str(repo))

    rc = supertool.main(["read:@p.toml"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "HELLO-672" in out
    assert "WRONG-ROOT" not in out


# ---------------------------------------------------------------------------
# Nothing else moves: no-cwd and absolute-path behaviour is untouched
# ---------------------------------------------------------------------------

def test_relative_payload_without_cwd_unchanged(two_roots, capsys) -> None:
    caller, _repo = two_roots
    (caller / "p.toml").write_text('path = "target.txt"' + chr(10), encoding="utf-8")

    rc = supertool.main(["read:@p.toml"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "WRONG-ROOT" in out


def test_absolute_payload_with_cwd_unchanged(two_roots, capsys) -> None:
    caller, repo = two_roots
    spec = repo / "p.toml"
    spec.write_text('path = "target.txt"' + chr(10), encoding="utf-8")

    rc = supertool.main([f"cwd:{repo}", f"read:@{spec}"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "HELLO-672" in out


def test_main_does_not_leak_its_root_into_a_later_dispatch(two_roots, capsys) -> None:
    """State scoped to the call — order-independent, not a side effect of xdist.

    main() leaves the process in the cwd: target; a dispatch() after it is a
    fresh caller standing there, and must resolve payloads there. A leaked
    invocation root would silently reach back into the previous call's
    directory — the long-lived-process failure (MCP mode).
    """
    caller, repo = two_roots
    (caller / "p.toml").write_text('path = "target.txt"' + chr(10), encoding="utf-8")
    supertool.main([f"cwd:{repo}", "read:@p.toml"])
    capsys.readouterr()

    out = supertool.dispatch("read:@p.toml")   # cwd is now repo/, and p.toml is not there

    assert "HELLO-672" not in out
    assert "not found" in out


def test_payload_under_moved_root_is_diagnosed_whatever_its_extension(two_roots, capsys) -> None:
    """The gate must catch a payload-shaped ref it can see under the other root.

    Extension-shape alone would leave `@p.payload` falling through to a
    literal-path read and the bare error this issue is about.
    """
    caller, repo = two_roots
    (repo / "p.payload").write_text('path = "target.txt"' + chr(10), encoding="utf-8")

    supertool.main([f"cwd:{repo}", "read:@p.payload"])
    out = capsys.readouterr().out

    assert "HELLO-672" not in out              # still not read from the wrong root
    assert "invocation directory" in out
    assert str(caller) in out


def test_direct_dispatch_without_main_unchanged(tmp_path: Path, monkeypatch, restore_cwd) -> None:
    """dispatch() outside main() — MCP mode, tests — still resolves against cwd."""
    (tmp_path / "t.txt").write_text("DISPATCH-672" + chr(10), encoding="utf-8")
    (tmp_path / "p.toml").write_text('path = "t.txt"' + chr(10), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    out = supertool.dispatch("read:@p.toml")

    assert "DISPATCH-672" in out


def test_at_pattern_that_is_not_a_payload_still_falls_through(two_roots) -> None:
    """`grep:@Override:src/` is a real search, not a payload reference."""
    caller, _repo = two_roots
    (caller / "src.java").write_text("@Override" + chr(10) + "void f() {}" + chr(10), encoding="utf-8")

    out = supertool.dispatch("grep:@Override:src.java")

    assert "@Override" in out
    assert "ERROR" not in out
