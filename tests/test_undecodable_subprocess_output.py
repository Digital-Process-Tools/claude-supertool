"""#501: what happens after `errors="replace"` has already saved the op.

The sweep pins `encoding="utf-8", errors="replace"` on every decoding
subprocess call in shipped code, which is the right trade almost everywhere:
git, gh and glab write UTF-8 whatever `LANG` says, and where their output is
somebody else's bytes — a blob, a CI log, a commit message — mojibake beats the
#498 failure, a traceback that lands *after* half the answer is on screen.

It is the wrong trade at two seams, and these are the tests for those:

* **the vim shell verbs** (`:!`, `:%!`, `:r !`) push the child's stdout back
  into the user's file. `errors="replace"` there does not save anything — it
  writes U+FFFD over the user's bytes and reports success.
* **`git diff --cached -z --name-only`** hands raw path bytes to
  `validate_staged` / `format_staged`. A mangled name fails `os.path.isfile`
  and the entry disappears from the list with nothing said — a pre-commit gate
  quietly declining to check a file that is being committed.

Both refuse and say so instead. The alternative to a crash is not "proceed
anyway"; it is "proceed with what is safe and name what was not".
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import supertool


@pytest.fixture
def vim_shell_on(monkeypatch):
    monkeypatch.setenv("SUPERTOOL_ALLOW_VIM_SHELL", "1")
    yield


def _emitter(tmp_path: Path, payload: bytes) -> str:
    """A shell command printing `payload` raw — bypasses the child's own codec.

    `sys.stdout.buffer` is deliberate: supertool pins `PYTHONIOENCODING=utf-8`
    on every child it spawns, so a child printing *text* could never produce
    the bytes this test is about. Real producers (git blob content, CI logs,
    any tool piping a binary through) write to the raw stream exactly so.
    """
    script = tmp_path / "emit.py"
    script.write_bytes(
        b"import sys\nsys.stdout.buffer.write(" + repr(payload).encode("ascii") + b")\n"
    )
    return f'"{sys.executable}" "{script}"'


BAD = b"caf\xe9 \x89 latin1\n"
GOOD = b"clean output\n"


class TestUndecodableAt:
    def test_clean_text_reports_no_offset(self) -> None:
        assert supertool._undecodable_at("café — fine") == -1

    def test_reports_the_offset_of_the_first_replacement(self) -> None:
        assert supertool._undecodable_at("ab�c�") == 2


class TestVimShellRefusesMojibake:
    """The write-back seam. A refusal must leave the file byte-identical."""

    def test_bang_insert_refuses_and_leaves_the_file_alone(
        self, vim_shell_on, tmp_path: Path
    ) -> None:
        target = tmp_path / "foo.txt"
        target.write_text("one\ntwo\n", encoding="utf-8")
        before = target.read_bytes()
        out = supertool.dispatch(
            f"vim:::{target}:::G\\e:!{_emitter(tmp_path, BAD)}")
        assert "ERROR" in out and "not valid UTF-8" in out, out
        assert "file NOT modified" in out, out
        assert target.read_bytes() == before

    def test_range_filter_refuses_and_leaves_the_file_alone(
        self, vim_shell_on, tmp_path: Path
    ) -> None:
        target = tmp_path / "foo.txt"
        target.write_text("a\nb\nc\n", encoding="utf-8")
        before = target.read_bytes()
        out = supertool.dispatch(
            f"vim:::{target}:::G\\e:%!{_emitter(tmp_path, BAD)}")
        assert "ERROR" in out and "not valid UTF-8" in out, out
        assert "NOT replaced" in out, out
        assert target.read_bytes() == before

    def test_r_bang_refuses_to_read_mojibake_in_as_content(
        self, vim_shell_on, tmp_path: Path
    ) -> None:
        target = tmp_path / "foo.txt"
        target.write_text("keep me\n", encoding="utf-8")
        before = target.read_bytes()
        out = supertool.dispatch(
            f"vim:::{target}:::G\\e:r !{_emitter(tmp_path, BAD)}")
        assert "ERROR" in out and "not valid UTF-8" in out, out
        assert target.read_bytes() == before

    def test_a_clean_command_still_writes(self, vim_shell_on, tmp_path: Path) -> None:
        """The guard must not have made every shell verb refuse."""
        target = tmp_path / "foo.txt"
        target.write_text("one\n", encoding="utf-8")
        out = supertool.dispatch(
            f"vim:::{target}:::G\\e:!{_emitter(tmp_path, GOOD)}")
        assert "ERROR" not in out, out
        assert "clean output" in target.read_text(encoding="utf-8")


class TestStagedPathsThatDidNotDecode:
    """The filesystem seam. A dropped path must be named, not just dropped."""

    def test_clean_paths_produce_no_warning(self) -> None:
        assert supertool._undecodable_staged_paths("a.py\x00b.py\x00") == ""

    def test_a_mangled_path_is_named_with_a_count(self) -> None:
        warning = supertool._undecodable_staged_paths("a.py\x00caf�.py\x00")
        assert "1 staged path(s)" in warning
        assert "caf�.py" in warning
        assert "NOT" in warning

    def _stage_with(self, monkeypatch, tmp_path: Path, stdout: str) -> None:
        """Pin `git diff --cached -z` output — a latin-1 filename cannot be
        created on macOS (APFS rejects invalid UTF-8 names), so the bytes are
        injected at the seam they would arrive from."""
        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(args, 0, stdout, "")
            return real_run(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.chdir(tmp_path)

    def test_validate_staged_names_the_file_it_could_not_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "ok.json").write_text("{}\n", encoding="utf-8")
        self._stage_with(monkeypatch, tmp_path, "ok.json\x00caf�.json\x00")
        supertool._CONFIG, supertool._CONFIG_CHECKED = {"validators": {}}, True
        result = supertool.op_validate_staged()
        assert "caf�.json" in result, result
        assert "ok.json" in result, result

    def test_format_staged_says_so_even_when_nothing_else_is_staged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worst case: the only staged file is the one that did not decode.

        Before #501 this returned a bare "no staged files" — a green receipt
        for a commit nothing had looked at.
        """
        self._stage_with(monkeypatch, tmp_path, "caf�.json\x00")
        supertool._CONFIG, supertool._CONFIG_CHECKED = {"formatters": {}}, True
        result = supertool.op_format_staged()
        assert "caf�.json" in result, result
        assert result != "no staged files\n"
