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

Running them for the first time found two real defects that in-process
tests could not see, both fixed alongside:

  - `paths = [...]` did not parse on Python < 3.11. `_mini_toml_loads` — the
    fallback used when stdlib `tomllib` is absent — had no inline-array
    support, so the op's own documented payload form died with
    `unknown value type for 'paths'` on a third of the supported matrix
    (`requires-python = ">=3.9"`).
  - `git-commit` crashed on a non-UTF-8 console. commit.py prints ✓/✗ and
    runs as its own process, which does not inherit supertool.py's stdout
    reconfiguration, so a cp1252 console turned a successful commit into a
    UnicodeEncodeError traceback.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import supertool  # noqa: E402

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


def _run(
    args: list[str], cwd: Path, stdin: str = "", extra_env: dict = None,
) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _message(cwd: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=format:%B"],
        cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", check=True, errors="replace",
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
    """`paths` is variadic: every listed path lands in the commit — and only those.

    This assertion used to read `["a.txt", "b.txt", "c.txt"]`. `a.txt` is
    staged by `_repo` and named by nothing here, so what it pinned was the
    #1228 defect: `git-commit` committed the whole index rather than the paths
    it was given. The payload names two paths; two paths are committed, and
    `a.txt` stays staged exactly where the fixture left it.
    """
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
        cwd=work, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
    ).stdout.split()
    assert sorted(committed) == ["b.txt", "c.txt"]
    still_staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=work, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
    ).stdout.split()
    assert still_staged == ["a.txt"], out


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


# What #400's reproduction actually collapses to. Pinned as a literal so the
# half of the demonstration that is about *supertool* runs everywhere, and
# only the half that is about POSIX shell semantics needs a shell.
MANGLED_BY_SHELL = "subject (#12167)Co-Authored-By: Max <noreply>"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "asserts a POSIX shell property (command substitution strips trailing "
        "newlines). Driving bash from Python on Windows goes through "
        "list2cmdline, whose backslash-escaping of the nested double quotes "
        "Git Bash re-parses differently — the script arrives corrupted and "
        "exits 1, which would test the quoting round-trip rather than the "
        "shell behaviour. The op-side assertion below is not skipped."
    ),
)
def test_reported_mangling_is_produced_by_the_shell() -> None:
    """#400's reproduction loses its newlines before supertool sees them.

    `"...$(printf '\\n\\n')..."` — command substitution strips ALL trailing
    newlines, so bash builds a single-line string.
    """
    built = subprocess.run(
        ["bash", "-c",
         "printf '%s' \"subject (#12167)$(printf '\\n\\n')"
         "Co-Authored-By: Max <noreply>\""],
        capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
    ).stdout
    assert built == MANGLED_BY_SHELL
    assert "\n" not in built


def test_op_faithfully_commits_what_the_shell_handed_it(tmp_path: Path) -> None:
    """Given the collapsed string, the op commits exactly that.

    This is the half that indicts the shell rather than the op, so it runs on
    every platform — no shell involved, the collapsed value is a constant.
    """
    work = _repo(tmp_path)
    code, out, err = _run([f"git-commit:::{MANGLED_BY_SHELL}:::a.txt"], cwd=work)
    assert code == 0, f"stdout={out} stderr={err}"
    assert _message(work).splitlines()[0] == MANGLED_BY_SHELL


# --- The fallback TOML parser: `paths = [...]` on Python < 3.11 -------------
#
# These call _mini_toml_loads directly rather than going through a payload,
# so they exercise the fallback on EVERY interpreter. Routed through the
# @file loader they would only run on 3.9/3.10, and the 3.11+ jobs — the
# ones that were green while the route was broken — would prove nothing.


def test_mini_toml_parses_the_paths_array_from_the_documented_payload() -> None:
    """The exact payload shape `git-commit:@-` documents."""
    parsed = supertool._mini_toml_loads(
        'message = """subject: here\n\nbody"""\npaths = ["src/Foo", "src/Bar"]\n'
    )
    assert parsed == {
        "message": "subject: here\n\nbody",
        "paths": ["src/Foo", "src/Bar"],
    }


def test_mini_toml_array_accepts_empty_trailing_comma_and_comments() -> None:
    assert supertool._mini_toml_loads("paths = []\n") == {"paths": []}
    assert supertool._mini_toml_loads('paths = ["a",]\n') == {"paths": ["a"]}
    assert supertool._mini_toml_loads(
        'paths = [\n  "a",  # keep\n  # skip\n  "b",\n]\n'
    ) == {"paths": ["a", "b"]}


def test_mini_toml_array_holds_every_scalar_the_parser_supports() -> None:
    """Elements reuse the scalar parser, so all its types work — and nest."""
    assert supertool._mini_toml_loads(
        "vals = [1, -2, true, false, 'lit', \"esc\\tx\", '''raw''']\n"
    ) == {"vals": [1, -2, True, False, "lit", "esc\tx", "raw"]}
    assert supertool._mini_toml_loads('n = [["a"], []]\n') == {
        "n": [["a"], []]
    }


def test_mini_toml_array_errors_are_explicit_not_a_truncated_list() -> None:
    """A malformed array must raise, never silently return fewer elements."""
    for bad in ('paths = ["a"\n', 'paths = ["a" "b"]\n', "paths = [\n"):
        try:
            supertool._mini_toml_loads(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_mini_toml_still_rejects_a_genuinely_unknown_value() -> None:
    """The array branch must not have swallowed the unknown-type error."""
    try:
        supertool._mini_toml_loads("k = @\n")
    except ValueError as exc:
        assert "unknown value type" in str(exc)
    else:
        raise AssertionError("expected ValueError")


# --- The preset's own stdout encoding --------------------------------------


def test_commit_succeeds_on_a_non_utf8_console(tmp_path: Path) -> None:
    """commit.py prints ✓ and runs as its own process.

    Forcing cp1252 reproduces the Windows failure on any OS. Two distinct
    defects sit on this one line, which is why the glyph itself is asserted
    rather than just the exit code:

      - commit.py did not reconfigure its own stdout, so writing ✓ raised
        UnicodeEncodeError *after* the commit had landed;
      - supertool.py then decoded the preset's output with the locale
        encoding, so the UTF-8 bytes E2 9C 93 came back as the three cp1252
        characters 'â\\u0153\\u201c' — a receipt reporting mojibake for an op
        that worked.

    Asserting only `code == 0` would have passed against the second bug.
    """
    work = _repo(tmp_path)
    payload = 'message = """feat(git): non-utf8 console"""\npaths = ["a.txt"]\n'
    code, out, err = _run(
        ["git-commit:@-"], cwd=work, stdin=payload,
        extra_env={"PYTHONIOENCODING": "cp1252"},
    )
    assert code == 0, f"stdout={out} stderr={err}"
    assert "UnicodeEncodeError" not in out + err
    assert "UnicodeDecodeError" not in out + err
    assert "✓" in out
    assert _message(work) == (
        "feat(git): non-utf8 console\n"
        "\n"
        f"Co-Authored-By: {COAUTHOR}\n"
    )


def test_receipt_keeps_utf8_glyphs_under_a_non_utf8_locale(tmp_path: Path) -> None:
    """supertool must decode an op's output as UTF-8, not as the locale.

    The git preset is deliberately not used here. `text=True` without an
    explicit encoding decodes with `locale.getpreferredencoding()`, so on a
    cp1252 console the UTF-8 bytes E2 9C 93 came back as three wrong
    characters and the receipt showed mojibake for an op that succeeded.

    Forcing an ASCII locale makes that decode *raise* rather than merely
    mangle, so the pin goes red on Linux and macOS too instead of relying on
    the Windows jobs alone. The config here is ASCII-only and declares its op
    inline: `presets/git.json` contains non-ASCII, and supertool reads its
    config files without an explicit encoding — an unrelated defect that
    would otherwise crash this test before it reached the code under test.
    """
    work = tmp_path / "w"
    work.mkdir()
    emitter = work / "emit.py"
    emitter.write_text(
        "import sys\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n"
        "print('done \\u2713')\n",
        encoding="ascii",
    )
    (work / ".supertool.json").write_text(
        '{"ops": {"emit": {"cmd": "%s emit.py", "syntax": "emit"}}}'
        % sys.executable.replace("\\", "/"),
        encoding="ascii",
    )
    code, out, err = _run(
        ["emit"], cwd=work,
        extra_env={
            "LC_ALL": "C", "LANG": "C",
            "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0",
        },
    )
    assert code == 0, f"stdout={out} stderr={err}"
    assert "UnicodeDecodeError" not in out + err
    assert "done ✓" in out
