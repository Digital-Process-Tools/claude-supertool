"""End-to-end pins for git-commit's two message routes (issue #400).

#400 reported that git-commit had no @payload route and silently mangled a
multi-line message. The route exists — it landed in #340 — and the mangling
in the report came from bash `$(printf '\\n\\n')`, whose command
substitution strips trailing newlines before supertool is ever invoked.

What was genuinely missing is an end-to-end pin. The @file unit tests in
tests/test_at_file_route.py stub `_at_file_specs`, so a regression in the
syntax-derived field registry, in preset argv quoting, or in commit.py's
message handling would leave them green while no correct message ever
reached git. These tests assert the committed bytes instead.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"

COAUTHOR = "Test Bot <bot@example.invalid>"


def _repo(tmp_path: Path) -> Path:
    """A throwaway git repo wired to the shipped git preset, one file staged."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n')
    (work / "a.txt").write_text("hi\n")
    subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
    return work


def _run(args: list[str], cwd: Path, stdin: str = "") -> tuple[int, str, str]:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=60,
        cwd=str(cwd), env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _message(cwd: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=format:%B"],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


PAYLOAD_MESSAGE = (
    "feat(git): subject with a colon\n"
    "\n"
    "Body line one, also with a colon: here.\n"
    "Body line two.\n"
)

EXPECTED_MESSAGE = (
    "feat(git): subject with a colon\n"
    "\n"
    "Body line one, also with a colon: here.\n"
    "Body line two.\n"
    "\n"
    f"Co-Authored-By: {COAUTHOR}\n"
)


def test_payload_stdin_route_commits_the_message_byte_for_byte(tmp_path: Path) -> None:
    """`git-commit:@-` — colon in subject, blank line, multi-line body, trailer."""
    work = _repo(tmp_path)
    payload = (
        f'message = """{PAYLOAD_MESSAGE.rstrip()}"""\n'
        'paths = ["a.txt"]\n'
    )
    code, out, err = _run(["git-commit:@-"], cwd=work, stdin=payload)
    assert code == 0, f"stdout={out} stderr={err}"
    assert _message(work) == EXPECTED_MESSAGE


def test_payload_file_route_commits_the_message_byte_for_byte(tmp_path: Path) -> None:
    """`git-commit:@msg.toml` must behave identically to the stdin form."""
    work = _repo(tmp_path)
    (work / "msg.toml").write_text(
        f'message = """{PAYLOAD_MESSAGE.rstrip()}"""\n'
        'paths = ["a.txt"]\n'
    )
    code, out, err = _run(["git-commit:@msg.toml"], cwd=work)
    assert code == 0, f"stdout={out} stderr={err}"
    assert _message(work) == EXPECTED_MESSAGE


def test_payload_json_route_commits_the_message_byte_for_byte(tmp_path: Path) -> None:
    """JSON payloads auto-detect the same as TOML ones."""
    import json
    work = _repo(tmp_path)
    (work / "msg.json").write_text(
        json.dumps({"message": PAYLOAD_MESSAGE.rstrip(), "paths": ["a.txt"]})
    )
    code, out, err = _run(["git-commit:@msg.json"], cwd=work)
    assert code == 0, f"stdout={out} stderr={err}"
    assert _message(work) == EXPECTED_MESSAGE


def test_payload_trailer_already_present_is_not_duplicated(tmp_path: Path) -> None:
    """A payload that carries its own Co-Authored-By keeps exactly one."""
    work = _repo(tmp_path)
    body = (
        "fix(git): subject with a colon\n"
        "\n"
        f"Co-Authored-By: {COAUTHOR}"
    )
    payload = f'message = """{body}"""\npaths = ["a.txt"]\n'
    code, out, err = _run(["git-commit:@-"], cwd=work, stdin=payload)
    assert code == 0, f"stdout={out} stderr={err}"
    assert _message(work) == (
        "fix(git): subject with a colon\n"
        "\n"
        f"Co-Authored-By: {COAUTHOR}\n"
    )


def test_payload_route_stages_the_listed_paths(tmp_path: Path) -> None:
    """`paths` is variadic: every listed path lands in the commit."""
    work = _repo(tmp_path)
    (work / "b.txt").write_text("b\n")
    (work / "c.txt").write_text("c\n")
    payload = (
        'message = """chore(git): stage two paths"""\n'
        'paths = ["b.txt", "c.txt"]\n'
    )
    code, out, err = _run(["git-commit:@-"], cwd=work, stdin=payload)
    assert code == 0, f"stdout={out} stderr={err}"
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=work, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert sorted(committed) == ["a.txt", "b.txt", "c.txt"]


def test_colon_route_single_line_is_unchanged(tmp_path: Path) -> None:
    """The colon CLI keeps working: subject with a colon + appended trailer."""
    work = _repo(tmp_path)
    code, out, err = _run(
        ["git-commit:::feat(git): colon route subject:::a.txt"], cwd=work,
    )
    assert code == 0, f"stdout={out} stderr={err}"
    assert _message(work) == (
        "feat(git): colon route subject\n"
        "\n"
        f"Co-Authored-By: {COAUTHOR}\n"
    )


def test_colon_route_preserves_real_newlines(tmp_path: Path) -> None:
    """A genuine newline in the colon-CLI argument survives to the commit.

    This is the behaviour #400 believed was broken. It is not: an argv that
    actually contains "\\n" bytes commits them verbatim.
    """
    work = _repo(tmp_path)
    arg = (
        "git-commit:::feat(git): subject with a colon\n"
        "\n"
        "Body line, colon: included.\n"
        ":::a.txt"
    )
    code, out, err = _run([arg], cwd=work)
    assert code == 0, f"stdout={out} stderr={err}"
    assert _message(work) == (
        "feat(git): subject with a colon\n"
        "\n"
        "Body line, colon: included.\n"
        "\n"
        f"Co-Authored-By: {COAUTHOR}\n"
    )


def test_reported_mangling_happens_in_the_shell_not_in_the_op(tmp_path: Path) -> None:
    """#400's reproduction loses its newlines before supertool sees them.

    `"...$(printf '\\n\\n')..."` — command substitution strips ALL trailing
    newlines, so bash builds a single-line string. The op then faithfully
    commits that single line, which is the reported symptom.
    """
    built = subprocess.run(
        ["bash", "-c",
         "printf '%s' \"subject (#12167)$(printf '\\n\\n')"
         "Co-Authored-By: Max <noreply>\""],
        capture_output=True, text=True, check=True,
    ).stdout
    assert built == "subject (#12167)Co-Authored-By: Max <noreply>"
    assert "\n" not in built

    work = _repo(tmp_path)
    code, out, err = _run([f"git-commit:::{built}:::a.txt"], cwd=work)
    assert code == 0, f"stdout={out} stderr={err}"
    assert _message(work).splitlines()[0] == (
        "subject (#12167)Co-Authored-By: Max <noreply>"
    )
