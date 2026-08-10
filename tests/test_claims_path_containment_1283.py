"""#1283 — `claims:PATH` read any path on the machine.

`presets/claims/check.py` resolved an absolute argument directly and a
relative one against the git toplevel, with no containment check at all. Preset
ops are absent from the core's `_PATH_ARG_POSITIONS`, so nothing upstream
covered it either — one call, one path, two opposite answers:

    $ supertool 'read:/etc/hosts' 'claims:/etc/hosts'
    --- read:/etc/hosts ---
    ERROR: path escapes cwd: '/etc/hosts' …
    --- claims:/etc/hosts ---
    PASS (0.25s)
    /etc/hosts: holds 0 | contradicted 0 | couldn't check 1

Not only an existence oracle: reference-shaped substrings of the out-of-boundary
file come back in the render, and the read has already happened by the time
anything is printed.

The boundary enforced here is the **repository root**, not the core's cwd, and
that is deliberate rather than convenient: `claims` resolves a relative argument
against the git toplevel, and `_path_findings` has always answered `path leaves
the repository root` for a doc citing `/etc/hosts` or `../..`. The op already
owned that boundary for the paths it reads *about*; it did not apply it to the
path it reads *from*. The core's cwd rule stays underneath as defence in depth
for the day preset ops reach the chokepoint.

The refusal is a refusal — exit 2, nothing on stdout. A rendered document with
zero findings is what an unread file must never look like.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _symlink import require_symlink  # noqa: E402

_ROOT = Path(__file__).parent.parent

MARKER = "sk-live-DO-NOT-READ-1283"

NL = chr(10)


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


check = _load("presets/claims/check.py", "claims_check_1283")


def _tree(tmp_path: Path) -> Path:
    """A repo root, and a sibling directory that is outside it."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "note.md").write_text("A doc." + NL, encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(
        "token " + MARKER + " and `presets/secret-thing.py:12`" + NL,
        encoding="utf-8")
    return root


def _main(monkeypatch, root: Path, argv, allow: bool = False):
    """The CLI entry point, with the whole-suite containment opt-out removed.

    `tests/conftest.py` sets SUPERTOOL_ALLOW_OUTSIDE_CWD=1 for every test so
    tmp_path fixtures keep working. Leaving it set here would make every
    assertion below pass against the unfixed code.
    """
    if allow:
        monkeypatch.setenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", "1")
    else:
        monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    monkeypatch.setattr(check, "_root", lambda: root)
    monkeypatch.setattr(check, "_repo_slug", lambda _r: None)
    return check.main(argv)


def _refused(rc: int, out: str, err: str) -> None:
    assert rc == 2, "a path outside the boundary must not be checked"
    assert "repository root" in err, err
    assert out == "", (
        "a refusal that also renders a document reads as a clean check: " + out)
    assert MARKER not in out + err, "the refusal echoed the file's contents"


def test_an_absolute_path_outside_the_repo_root_is_refused(
        tmp_path, monkeypatch, capsys):
    root = _tree(tmp_path)
    target = tmp_path / "outside" / "secret.md"
    rc = _main(monkeypatch, root, [str(target)])
    cap = capsys.readouterr()
    _refused(rc, cap.out, cap.err)


def test_a_relative_argument_that_climbs_out_is_refused(
        tmp_path, monkeypatch, capsys):
    root = _tree(tmp_path)
    rc = _main(monkeypatch, root, ["../outside/secret.md"])
    cap = capsys.readouterr()
    _refused(rc, cap.out, cap.err)


def test_the_refusal_is_not_an_existence_oracle(tmp_path, monkeypatch, capsys):
    """`no such file` for one outside path and a render for another answers
    the question the boundary exists to refuse."""
    root = _tree(tmp_path)
    rc = _main(monkeypatch, root, [str(tmp_path / "outside" / "nope.md")])
    cap = capsys.readouterr()
    assert rc == 2
    assert "repository root" in cap.err, cap.err
    assert "no such file" not in cap.err, (
        "an absent path outside the boundary answered a different question "
        "than a present one: " + cap.err)


def test_a_symlink_inside_the_root_pointing_out_is_refused(
        tmp_path, monkeypatch, capsys):
    """A lexical check on the argument would pass this one. The boundary is
    about where the bytes are, so the check has to resolve."""
    require_symlink()
    root = _tree(tmp_path)
    link = root / "docs" / "link.md"
    link.symlink_to(tmp_path / "outside" / "secret.md")
    rc = _main(monkeypatch, root, ["docs/link.md"])
    cap = capsys.readouterr()
    _refused(rc, cap.out, cap.err)


def test_a_sibling_whose_name_starts_with_the_root_is_outside_it(
        tmp_path, monkeypatch, capsys):
    """The `+ os.sep` in the prefix test, pinned. `/repo-evil` starts with
    `/repo` and is not inside it — a bare `startswith` would admit the whole
    directory, and every other test here uses a sibling that shares no prefix,
    so none of them would notice."""
    root = _tree(tmp_path)
    evil = tmp_path / (root.name + "-evil")
    evil.mkdir()
    (evil / "doc.md").write_text("token " + MARKER + NL, encoding="utf-8")
    rc = _main(monkeypatch, root, [str(evil / "doc.md")])
    cap = capsys.readouterr()
    _refused(rc, cap.out, cap.err)


def test_a_document_inside_the_root_is_still_checked(
        tmp_path, monkeypatch, capsys):
    """The gate is a boundary, not a blanket refusal."""
    root = _tree(tmp_path)
    assert _main(monkeypatch, root, ["docs/note.md"]) == 0
    assert capsys.readouterr().out.startswith("docs/note.md: holds 0")


def test_the_env_opt_out_is_honoured(tmp_path, monkeypatch, capsys):
    """The refusal names two ways to allow it. Both have to work, or the
    message is the next stale claim in this repo."""
    root = _tree(tmp_path)
    target = tmp_path / "outside" / "secret.md"
    assert _main(monkeypatch, root, [str(target)], allow=True) == 0
    assert "holds 0" in capsys.readouterr().out


def test_the_config_opt_out_is_honoured(tmp_path, monkeypatch, capsys):
    root = _tree(tmp_path)
    (root / ".supertool.json").write_text(
        json.dumps({"allow_outside_cwd": True}), encoding="utf-8")
    target = tmp_path / "outside" / "secret.md"
    assert _main(monkeypatch, root, [str(target)]) == 0
    assert "holds 0" in capsys.readouterr().out


def test_a_broken_config_does_not_open_the_boundary(
        tmp_path, monkeypatch, capsys):
    """`_safe_path` wraps its config read so a broken file never raises out of
    a path check. Failing closed is the other half of that: an unparseable
    config must not read as an opt-out."""
    root = _tree(tmp_path)
    (root / ".supertool.json").write_text("{not json", encoding="utf-8")
    rc = _main(monkeypatch, root, [str(tmp_path / "outside" / "secret.md")])
    cap = capsys.readouterr()
    _refused(rc, cap.out, cap.err)


def test_the_boundary_is_the_root_and_the_message_says_so(
        tmp_path, monkeypatch, capsys):
    """`claims` resolves relative paths against the git toplevel and the core
    resolves against cwd. Those differ from a subdirectory, so the refusal has
    to name which one it enforced rather than leave the reader to assume."""
    root = _tree(tmp_path)
    _main(monkeypatch, root, [str(tmp_path / "outside" / "secret.md")])
    err = capsys.readouterr().err
    assert os.path.realpath(str(root)) in err, (
        "the refusal did not say which root it measured against: " + err)
